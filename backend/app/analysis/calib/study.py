"""Run the calibration study: replicate records for every cell and candidate method.

    python -m app.analysis.calib.study run --stage tune --reps 200 --jobs 6 --out runs/tune

Every replicate (one generated data set) is pushed through the real engine for every candidate
method; the record keeps, per method and window, the result, the largest p-value, the lags, the
sample size and the gate features, so the gate thresholds and the acceptance statistics can be
computed afterwards WITHOUT re-running. Records are written per (cell, chunk) as gzip JSON lines;
finished chunks are skipped on a re-run (resumable). Methods use calibrated=True here: reliability
is decided by the gate, applied afterwards (``accept.py``).
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
import zlib
from collections.abc import Callable, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from app.analysis.calib.dgp import (
    GATED_FAMILIES,
    HELD_OUT_FAMILIES,
    REGION_FAMILIES,
    STRUCTURES,
    TUNING_FAMILIES,
    Cell,
    make_replicate,
)
from app.analysis.engine import run_relation_test
from app.analysis.gate import GateThresholds, assess
from app.analysis.models import (
    PERIOD_ALL,
    PERIOD_RECENT,
    RESULT_INSUFFICIENT,
    TestReport,
)
from app.analysis.resampling import CircularShift, StationaryBootstrap, WholeChainBootstrap
from app.analysis.significance import HacOls

CODES = {"supported": "S", "unsupported": "U", "insufficient_data": "I"}
FALSE_SUPPORT_STRUCTURES = (
    "k1_null",
    "k1_reversed",
    "k2_null_null",
    "k2_real_null",
    "k2_real_null_r5",
    "k2_real_null_r8",
    "k2_real_reversed",
    "k3_real_null_null",
    "k3_real_real_null",
)
POWER_STRUCTURES = (
    "k1_real02",
    "k1_real04",
    "k1_real07",
    "k2_real_real04",
    "k2_real_real07",
    "k2_real_spread",
    "k2_real_nearzero",
    "k3_real_real_real",
)
POWER_FAMILIES = ("iid", "ar05", "ma11", "sar12_05")
SAMPLE_SIZES = (36, 60, 120, 250)


@dataclass(frozen=True)
class MethodDef:
    name: str
    make: Callable[[int], Any]  # seed -> method object
    max_k: int | None = None  # None = any number of drivers


def candidate_methods() -> list[MethodDef]:
    methods: list[MethodDef] = []
    # "rule" = the Newey-West rule of thumb; None = the engine default (overlap horizon of an
    # annual change, rule of thumb otherwise); 11 / 18 / 24 = forced bandwidths in months.
    for label, maxlags in (("rule", "rule"), ("default", None), ("11", 11), ("18", 18), ("24", 24)):
        for correction in (False, True):
            name = f"hac_{label}" + ("_bonf" if correction else "")
            methods.append(
                MethodDef(
                    name,
                    lambda seed, m=maxlags, c=correction: HacOls(
                        maxlags=m, lag_correction=c, calibrated=True
                    ),
                )
            )
    methods.append(MethodDef("shift", lambda seed: CircularShift(seed=seed, calibrated=True), 1))
    for block in (12, 18, 24, "pw"):
        methods.append(
            MethodDef(
                f"stationary_{block}",
                lambda seed, b=block: StationaryBootstrap(mean_block=b, seed=seed, calibrated=True),
                1,
            )
        )
    for block in (12, 18, 24):
        methods.append(
            MethodDef(
                f"chain_{block}",
                lambda seed, b=block: WholeChainBootstrap(block=b, seed=seed, calibrated=True),
            )
        )
    return methods


def tuning_cells() -> list[Cell]:
    cells: list[Cell] = []
    for family in TUNING_FAMILIES:
        for n in SAMPLE_SIZES:
            for structure in FALSE_SUPPORT_STRUCTURES:
                cells.append(Cell("stat", family, structure, n))
    for family in POWER_FAMILIES:
        for n in SAMPLE_SIZES:
            for structure in POWER_STRUCTURES:
                cells.append(Cell("stat", family, structure, n))
    annual = "annual_pct_change"
    for family in ("iid", "ar05", "ma11", "seasonal_indep", "seasonal_common"):
        for n in (60, 120, 250):
            for structure in ("k1_null", "k2_real_null", "k1_real04"):
                cells.append(Cell("e2e", family, structure, n, annual))
    for transform in ("difference", "period_pct_change"):
        for family in ("iid", "seasonal_common", "seasonal_indep"):
            for structure in ("k1_null", "k1_real04"):
                cells.append(Cell("e2e", family, structure, 120, transform))
    for structure in ("k1_null", "k1_real04"):
        cells.append(Cell("e2e", "iid", structure, 120, annual, gap=True))
        cells.append(Cell("e2e", "iid", structure, 120, annual, nominal=True))
    return cells


TUNING2_SIZES = (100, 250)
TUNING2_FAMILIES = (*REGION_FAMILIES, "seasonal_indep", "seasonal_common", "sar12_08")
TUNING2_METHODS = ("shift", "stationary_pw", "chain_12", "chain_24")


def tuning2_cells() -> list[Cell]:
    """Tuning-2 cells (protocol v1.5): region and gated families at the validation sizes."""
    cells: list[Cell] = []
    for family in TUNING2_FAMILIES:
        for n in TUNING2_SIZES:
            for structure in FALSE_SUPPORT_STRUCTURES:
                cells.append(Cell("stat", family, structure, n))
    for family in REGION_FAMILIES:
        for n in TUNING2_SIZES:
            for structure in POWER_STRUCTURES:
                cells.append(Cell("stat", family, structure, n))
    return cells


VALIDATION_SIZES = (100, 250)
#: Sample sizes that are not multiples of 12, for the controls of the circular-shift behaviour.
VALIDATION_MODULO_SIZES = (111, 245)
VALIDATION_MODULO_FAMILIES = ("iid", "ma11", "seasonal_indep")


def validation_cells() -> list[Cell]:
    """Validation cells (protocol v1.4): region, gated and held-out families, 9 structures each,
    plus the modulo-12 controls."""
    cells: list[Cell] = []
    for family in (*REGION_FAMILIES, *GATED_FAMILIES, *HELD_OUT_FAMILIES):
        for n in VALIDATION_SIZES:
            for structure in FALSE_SUPPORT_STRUCTURES:
                cells.append(Cell("stat", family, structure, n))
    for family in VALIDATION_MODULO_FAMILIES:
        for n in VALIDATION_MODULO_SIZES:
            for structure in FALSE_SUPPORT_STRUCTURES:
                cells.append(Cell("stat", family, structure, n))
    return cells


def _window_record(window: Any) -> dict[str, Any]:
    lags = window.details.get("lags")
    return {
        "r": CODES[window.result],
        "p": window.p_value,
        "n": window.n_obs,
        "l": list(lags.values()) if isinstance(lags, dict) else None,
        "f": window.details.get("features"),
    }


def _skipped_by_gate(
    series: list[Any], spec: Any, deflator: Any, gate: GateThresholds
) -> dict[str, Any] | None:
    """Validation shortcut (exact): the gate looks at data features only, so a replicate whose
    window fails it is unreliable whatever any method says. Returns the record entry shared by all
    methods (result ``I`` is the convention for "not reliable"), or ``None`` to run the methods."""
    cheap = run_relation_test(
        series[0],
        series[1:],
        spec,
        deflator=deflator,
        method=HacOls(maxlags="rule", calibrated=True),
    )
    if not isinstance(cheap, TestReport):
        return None
    windows = {w: cheap.windows[w] for w in (PERIOD_ALL, PERIOD_RECENT)}
    failed = any(w.result == RESULT_INSUFFICIENT for w in windows.values()) or any(
        "features" in w.details and not assess(w.details["features"], gate)[0]
        for w in windows.values()
    )
    if not failed:
        return None
    return {
        "status": "skipped_by_gate",
        "w": {
            name: {"r": "I", "p": None, "n": w.n_obs, "l": None, "f": w.details.get("features")}
            for name, w in windows.items()
        },
    }


def run_replicates(
    cell: Cell,
    methods: list[MethodDef],
    reps: range,
    seed_base: int,
    gate: GateThresholds | None = None,
) -> list[dict[str, Any]]:
    """Records of the given replicate numbers of one cell (``gate``: validation shortcut)."""
    k = cell.spec_structure.k
    records = []
    for rep in reps:
        rng = np.random.default_rng([seed_base, zlib.crc32(cell.id.encode("utf-8")), rep])
        series, spec, deflator = make_replicate(cell, rng)
        method_seed = int(rng.integers(0, 2**31 - 1))
        record: dict[str, Any] = {
            "cell": cell.id,
            "rep": rep,
            "k": k,
            "true_support": cell.spec_structure.true_support,
            "m": {},
        }
        skipped = _skipped_by_gate(series, spec, deflator, gate) if gate is not None else None
        for method in methods:
            if method.max_k is not None and k > method.max_k:
                continue
            if skipped is not None:
                record["m"][method.name] = skipped
                continue
            try:
                outcome = run_relation_test(
                    series[0], series[1:], spec, deflator=deflator, method=method.make(method_seed)
                )
            except Exception as error:  # noqa: BLE001 - counted as `failed`, never hidden
                record["m"][method.name] = {
                    "status": "failed",
                    "error": f"{type(error).__name__}: {error}"[:200],
                }
                continue
            if not isinstance(outcome, TestReport):
                record["m"][method.name] = {"status": "unsupported_series"}
                continue
            record["m"][method.name] = {
                "status": outcome.status,
                "w": {
                    PERIOD_ALL: _window_record(outcome.windows[PERIOD_ALL]),
                    PERIOD_RECENT: _window_record(outcome.windows[PERIOD_RECENT]),
                },
            }
        records.append(record)
    return records


PROTOCOL_VERSION = "1.5"
SEED_BASE = {"tune": 1001, "tune2": 1001, "validate": 9001}


class ManifestMismatch(RuntimeError):
    """The output directory belongs to a different run (stage, seed, methods, cells...)."""


def select_methods(only: str = "", names: Sequence[str] | None = None) -> list[MethodDef]:
    """Candidate methods by exact name (frozen validation) or by substring (development)."""
    methods = candidate_methods()
    if names is not None:
        wanted = set(names)
        unknown = wanted - {m.name for m in methods}
        if unknown:
            raise ValueError(f"unknown methods {sorted(unknown)}")
        return [m for m in methods if m.name in wanted]
    return [m for m in methods if not only or only in m.name]


def _manifest(
    stage: str,
    seed_base: int,
    reps: int,
    chunk: int,
    methods: list[MethodDef],
    cells: list[Cell],
    start_rep: int = 0,
) -> dict[str, Any]:
    return {
        "protocol": PROTOCOL_VERSION,
        "stage": stage,
        "seed_base": seed_base,
        "reps": reps,
        "start_rep": start_rep,
        "chunk": chunk,
        "methods": [m.name for m in methods],
        "cells": sorted(cell.id for cell in cells),
    }


def write_or_check_manifest(out: Path, manifest: dict[str, Any]) -> None:
    """Create ``MANIFEST.json`` or refuse to continue in a directory of a different run."""
    path = out / "MANIFEST.json"
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing != manifest:
            differing = sorted(k for k in manifest if existing.get(k) != manifest[k])
            raise ManifestMismatch(
                f"{out} holds a different run (differs in {differing}); use a new output directory"
            )
        return
    path.write_text(json.dumps(manifest, indent=1), encoding="utf-8")


def _chunk_path(out: Path, stage: str, seed_base: int, cell: Cell, start: int, stop: int) -> Path:
    safe = cell.id.replace("/", "~")
    return out / f"{stage}-s{seed_base}__{safe}.{start}-{stop}.jsonl.gz"


def _job(
    args: tuple[Cell, int, int, int, str, str, tuple[str, ...], dict[str, float] | None],
) -> tuple[str, int, float]:
    cell, start, stop, seed_base, out, stage, names, gate_values = args
    gate = GateThresholds(**gate_values) if gate_values else None
    path = _chunk_path(Path(out), stage, seed_base, cell, start, stop)
    if path.exists():
        return cell.id, 0, 0.0
    methods = select_methods(names=names)
    began = time.time()
    records = run_replicates(cell, methods, range(start, stop), seed_base, gate)
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + chr(10))
    tmp.replace(path)
    return cell.id, len(records), time.time() - began


def run_study(
    cells: list[Cell],
    reps: int,
    chunk: int,
    seed_base: int,
    out: Path,
    jobs: int,
    only: str = "",
    *,
    stage: str = "tune",
    names: Sequence[str] | None = None,
    gate: dict[str, float] | None = None,
    start_rep: int = 0,
) -> None:
    methods = select_methods(only, names)
    out.mkdir(parents=True, exist_ok=True)
    manifest = _manifest(stage, seed_base, reps, chunk, methods, cells, start_rep)
    manifest["gate"] = gate
    write_or_check_manifest(out, manifest)
    method_names = tuple(m.name for m in methods)
    tasks = [
        (cell, start, min(start + chunk, reps), seed_base, str(out), stage, method_names, gate)
        for cell in cells
        for start in range(start_rep, reps, chunk)
    ]
    print(f"{len(cells)} cells, {len(tasks)} tasks, jobs={jobs}", file=sys.stderr, flush=True)
    done = 0
    with ProcessPoolExecutor(max_workers=jobs) as pool:
        for cell_id, count, seconds in pool.map(_job, tasks):
            done += 1
            if done % 10 == 0 or done == len(tasks):
                print(
                    f"{done}/{len(tasks)} tasks (last {cell_id}: {count} reps, {seconds:.1f}s)",
                    file=sys.stderr,
                    flush=True,
                )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.analysis.calib.study", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run the study")
    run.add_argument("--stage", choices=("tune", "tune2", "validate"), required=True)
    run.add_argument("--reps", type=int, default=None)
    run.add_argument("--chunk", type=int, default=None)
    run.add_argument("--start-rep", type=int, default=0, help="first replicate (extending a run)")
    run.add_argument(
        "--methods", default=None, help="comma list of method names (tune2: default set)"
    )
    run.add_argument("--jobs", type=int, default=4)
    run.add_argument("--seed", type=int, default=None)
    run.add_argument("--out", required=True)
    run.add_argument("--only-cell", default="", help="substring filter on cell ids (tuning only)")
    run.add_argument(
        "--family",
        action="append",
        default=None,
        help="restrict the tuning cells to these families (repeatable; tuning only)",
    )
    run.add_argument("--only-method", default="", help="substring filter on method names (tuning)")
    run.add_argument(
        "--freeze", default=None, help="FREEZE.json with the frozen methods (validate)"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    names: list[str] | None = None
    gate: dict[str, float] | None = None
    if args.stage == "tune2":
        cells = [c for c in tuning2_cells() if args.only_cell in c.id]
        names = list(args.methods.split(",")) if args.methods else list(TUNING2_METHODS)
        reps = args.reps if args.reps is not None else 800
        chunk = args.chunk if args.chunk is not None else 25
    elif args.stage == "tune":
        cells = [c for c in tuning_cells() if args.only_cell in c.id]
        if args.family:
            cells = [c for c in cells if c.family in args.family]
        reps = args.reps if args.reps is not None else 200
        chunk = args.chunk if args.chunk is not None else 25
    else:
        if not args.freeze:
            raise SystemExit("validation runs only the frozen method: pass --freeze FREEZE.json")
        if args.only_cell or args.only_method or args.family:
            raise SystemExit(
                "validation takes no cell or method filters (the freeze record decides)"
            )
        frozen = json.loads(Path(args.freeze).read_text(encoding="utf-8"))
        names = list(frozen["methods"])
        gate = frozen.get("gate")
        cells = validation_cells()
        reps = args.reps if args.reps is not None else int(frozen.get("budget_reps", 6000))
        chunk = args.chunk if args.chunk is not None else 100
    seed = args.seed if args.seed is not None else SEED_BASE.get(args.stage, 1001)
    unknown = [c.structure for c in cells if c.structure not in STRUCTURES]
    if unknown:
        raise SystemExit(f"unknown structures {unknown}")
    run_study(
        cells,
        reps,
        chunk,
        seed,
        Path(args.out),
        args.jobs,
        args.only_method,
        stage=args.stage,
        names=names,
        gate=gate,
        start_rep=args.start_rep,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
