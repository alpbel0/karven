"""Analysis of the calibration study: gate tuning, method selection, acceptance (protocol §7-8).

Works on the replicate records written by ``study.py`` (nothing is re-run). Definitions:

* a replicate is **reliable** when the final decision exists (no window is ``insufficient_data``)
  and BOTH windows pass the gate;
* **false support** = reliable and supported (both windows ``S``) while the truth is not
  "every driver has a positive effect"; **power** = reliable, supported and the truth holds;
* the usage region **R** is fixed by the protocol (stat layer, n in {120, 250}, families
  iid / ar05 / seasonal_indep / ma11).

Tuning (protocol 8.1, v1.2): every candidate that covers k = 1, 2 and 3 in R is eligible. A method
that only handles one driver enters as a **composite** ``<pairwise>+<chain>`` (the pairwise
method for k = 1, the whole-chain bootstrap for k >= 2). Per candidate the gate keeps the point
false-support rate <= 5 % in at least 90 % of the R false-support cells (each with >=
``MIN_RELIABLE_TUNING`` reliable replicates), reliable rate >= 60 % and strong-effect power >= 50 %
in R; among those the gate with the highest **mean per-cell power** over the R power cells wins.
The method with the highest mean power is chosen; powers within ``TIE`` of each other are ties and
go to the simplest family (HAC, shift, stationary, chain).

Validation (protocol 8.3-8.4) uses only the frozen method and gate: every acceptance cell needs
>= ``MIN_RELIABLE_VALIDATION`` reliable replicates and a one-sided 95 % Clopper-Pearson upper
bound of the false-support rate below the ceiling (intersection-union: all cells must pass, no
cell selection afterwards). The Bonferroni bound is reported as a sensitivity check.
"""

from __future__ import annotations

import argparse
import gzip
import itertools
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy.stats import beta as beta_distribution

from app.analysis.calib.dgp import GATED_FAMILIES, HELD_OUT_FAMILIES, REGION_FAMILIES
from app.analysis.gate import GateThresholds

CEILING = 0.05
MIN_RELIABLE_TUNING = 200  # protocol v1.5: a pilot cell needs >= 200 reliable replicates
FS_MARGIN = 0.04  # v1.5 eligibility: region false-support point rate <= ceiling - 1 point
LEAK_MARGIN = 0.015  # v1.5 eligibility: gated cells, leak
GATED_SHARE_MARGIN = 0.08  # v1.5 eligibility: gated cells, reliable share
MIN_RELIABLE_VALIDATION = 1500
TIE = 0.01
FAILURE_ALERT = 0.001
LEAK_CEILING = 0.025  # must-be-gated cells: P(reliable and false support) over all replicates
GATED_RELIABLE_CAP = 0.10  # must-be-gated cells: P(reliable) over all replicates
REGION_SIZES = (100, 250)
STRONG_STRUCTURES = ("k1_real07", "k2_real_real07", "k3_real_real_real")
SIMPLICITY = ("hac", "shift", "stationary", "chain")  # tie-break order

#: Composites: a pairwise method for k = 1 with the whole-chain bootstrap for k >= 2.
COMPOSITES = {
    "shift+chain_12": ("shift", "chain_12"),
    "shift+chain_18": ("shift", "chain_18"),
    "shift+chain_24": ("shift", "chain_24"),
    "stationary_12+chain_12": ("stationary_12", "chain_12"),
    "stationary_18+chain_18": ("stationary_18", "chain_18"),
    "stationary_24+chain_24": ("stationary_24", "chain_24"),
    "stationary_pw+chain_12": ("stationary_pw", "chain_12"),
}

GRID = {
    "n_min": (100,),  # fixed by the protocol (v1.5): windows below 100 are not reliable
    "rho1_max": (0.6, 0.8, 0.9, 1.01),
    "rho_seasonal_max": (0.3, 0.4, 0.6, 1.01),
    "var_ratio_max": (2.0, 4.0, float("inf")),
    "mean_shift_max": (0.5, 1.0, float("inf")),
    "rho_seasonal24_max": (-1.0, 0.1, 0.15, 0.2, 0.3),
}
FEATURES = ("n", "rho1", "rho_seasonal", "rho_seasonal24", "var_ratio", "mean_shift")


def clopper_pearson_upper(failures: int, total: int, confidence: float = 0.95) -> float:
    """One-sided Clopper-Pearson upper bound of a binomial proportion."""
    if total <= 0 or failures >= total:
        return 1.0
    return float(beta_distribution.ppf(confidence, failures + 1, total - failures))


def parse_cell(cell_id: str) -> dict[str, Any]:
    parts = cell_id.split("/")
    layer, family, structure, size = parts[:4]
    rest = parts[4:]
    return {
        "layer": layer,
        "family": family,
        "structure": structure,
        "n": int(size[1:]),
        "transform": next((p for p in rest if p.endswith("change") or p == "difference"), ""),
        "gap": "gap" in rest,
        "nominal": "nominal" in rest,
    }


def load_records(directory: Path | list[Path] | tuple[Path, ...]) -> pd.DataFrame:
    """One row per (cell, replicate, method). Failed calls are counted in ``frame.attrs``.

    Accepts one directory or several (a study split over runs). Raises ``ValueError`` when a
    (cell, replicate, method) appears twice (overlapping chunks or mixed runs).
    """
    directories = [directory] if isinstance(directory, Path) else list(directory)
    paths = sorted(path for folder in directories for path in folder.glob("*.jsonl.gz"))
    rows: list[dict[str, Any]] = []
    failed: dict[tuple[str, str], int] = {}
    for path in paths:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                record = json.loads(line)
                head = {
                    "cell": record["cell"],
                    "rep": record["rep"],
                    "k": record["k"],
                    "true_support": record["true_support"],
                }
                for method, outcome in record["m"].items():
                    if outcome["status"] == "failed":
                        failed[(record["cell"], method)] = (
                            failed.get((record["cell"], method), 0) + 1
                        )
                        continue
                    if "w" not in outcome:
                        continue
                    row = dict(head, method=method, status=outcome["status"])
                    for index, window in enumerate(("all_years", "2017_2026"), start=1):
                        data = outcome["w"][window]
                        row[f"r{index}"] = data["r"]
                        row[f"p{index}"] = data["p"] if data["p"] is not None else np.nan
                        features = data["f"] or {}
                        for name in FEATURES:
                            row[f"{name}{index}"] = features.get(name, np.nan)
                    rows.append(row)
    frame = pd.DataFrame(rows)
    if frame.empty:
        frame.attrs["failed"] = failed
        return frame
    if frame.duplicated(["cell", "rep", "method"]).any():
        raise ValueError("duplicate (cell, rep, method) records: chunks overlap or runs were mixed")
    cells = frame["cell"].unique()
    parsed = pd.DataFrame([parse_cell(c) for c in cells], index=cells)
    frame = frame.join(parsed, on="cell")
    frame.attrs["failed"] = failed
    return frame


def attach_features(frame: pd.DataFrame, features_dir: Path) -> pd.DataFrame:
    """Fill the lag-24 seasonal autocorrelation of records written before that feature existed.

    ``features_dir`` holds a cheap re-run (one method) of the SAME cells and seeds: the data are
    deterministic, so the features of every (cell, replicate) are recomputed and joined to all
    methods. Records that already carry the feature (newer runs) keep their own value; only missing
    values are filled. The overlapping base features (n, lag-1 and lag-12 autocorrelation) of both
    runs must be identical (checked): this proves that the regenerated data are the data the
    original records were made from.
    """
    extra = load_records(features_dir)
    columns = ["rho_seasonal241", "rho_seasonal242"]
    one = extra.drop_duplicates(["cell", "rep"])[
        ["cell", "rep", "n1", "rho11", "rho_seasonal1", *columns]
    ]
    merged = frame.merge(one, on=["cell", "rep"], how="left", suffixes=("", "_re"))
    covered = merged["n1_re"].notna()
    for name in ("n1", "rho11", "rho_seasonal1"):
        old = merged.loc[covered, name].to_numpy(dtype=float)
        new = merged.loc[covered, f"{name}_re"].to_numpy(dtype=float)
        if not np.allclose(old, new, rtol=1e-9, atol=1e-12, equal_nan=True):
            raise ValueError(f"regenerated data differ from the original records ({name})")
    for name in columns:
        merged[name] = merged[name].where(merged[name].notna(), merged[f"{name}_re"])
    out = merged.drop(columns=[c for c in merged.columns if c.endswith("_re")])
    out.attrs = dict(frame.attrs)
    return out


def build_composites(frame: pd.DataFrame, names: list[str] | None = None) -> pd.DataFrame:
    """Rows of the composite candidates (pairwise for k = 1, chain for k >= 2)."""
    present = set(frame["method"].unique())
    parts = []
    for name, (pairwise, chain) in COMPOSITES.items():
        if names is not None and name not in names:
            continue
        if pairwise not in present or chain not in present:
            continue
        one = frame[(frame["method"] == pairwise) & (frame["k"] == 1)]
        many = frame[(frame["method"] == chain) & (frame["k"] >= 2)]
        parts.append(pd.concat([one, many]).assign(method=name))
    return pd.concat(parts, ignore_index=True) if parts else frame.iloc[0:0]


def with_composites(frame: pd.DataFrame, names: list[str] | None = None) -> pd.DataFrame:
    composites = build_composites(frame, names)
    out = pd.concat([frame, composites], ignore_index=True) if len(composites) else frame
    out.attrs["failed"] = frame.attrs.get("failed", {})
    return out


def in_region(
    frame: pd.DataFrame, families: tuple[str, ...] | list[str] = REGION_FAMILIES
) -> pd.Series:
    return (
        (frame["layer"] == "stat") & frame["n"].isin(REGION_SIZES) & frame["family"].isin(families)
    )


def in_gated(
    frame: pd.DataFrame, families: tuple[str, ...] | list[str] = GATED_FAMILIES
) -> pd.Series:
    """Cells of the families the gate must reject (persistent seasonality)."""
    return (
        (frame["layer"] == "stat") & frame["n"].isin(REGION_SIZES) & frame["family"].isin(families)
    )


def reliable_mask(frame: pd.DataFrame, gate: GateThresholds | None) -> pd.Series:
    """Both windows decided and inside the gate (no gate: only 'decided')."""
    decided = (frame["r1"] != "I") & (frame["r2"] != "I")
    if gate is None:
        return decided
    ok = decided
    for index in (1, 2):
        ok = (
            ok
            & (frame[f"n{index}"] >= gate.n_min)
            & (frame[f"rho1{index}"] <= gate.rho1_max)
            & ~(
                (frame[f"rho_seasonal{index}"] > gate.rho_seasonal_max)
                & (frame[f"rho_seasonal24{index}"].fillna(1.0) > gate.rho_seasonal24_max)
            )
            & (frame[f"var_ratio{index}"] <= gate.var_ratio_max)
            & (frame[f"mean_shift{index}"] <= gate.mean_shift_max)
        )
    return ok


def cell_table(frame: pd.DataFrame, gate: GateThresholds | None) -> pd.DataFrame:
    """Per cell: reps, reliable, false-support and power counts under a gate."""
    reliable = reliable_mask(frame, gate)
    supported = (frame["r1"] == "S") & (frame["r2"] == "S")
    work = frame.assign(
        reliable=reliable,
        false_support=reliable & supported & ~frame["true_support"],
        power=reliable & supported & frame["true_support"],
        decided=(frame["r1"] != "I") & (frame["r2"] != "I"),
    )
    return work.groupby("cell").agg(
        reps=("rep", "size"),
        reliable=("reliable", "sum"),
        false_support=("false_support", "sum"),
        power=("power", "sum"),
        decided=("decided", "sum"),
        true_support=("true_support", "first"),
        structure=("structure", "first"),
        family=("family", "first"),
        n=("n", "first"),
        k=("k", "first"),
        layer=("layer", "first"),
    )


def covers_all_driver_counts(method_frame: pd.DataFrame) -> bool:
    """Eligible only if the method has region cells for k = 1, 2 and 3."""
    return {1, 2, 3} <= set(method_frame[in_region(method_frame)]["k"].unique())


def evaluate_gate(method_frame: pd.DataFrame, gate: GateThresholds) -> dict[str, Any]:
    """The tuning statistics of one method under one gate (region R only)."""
    region = method_frame[in_region(method_frame)]
    table = cell_table(region, gate)
    fs_cells = table[~table["true_support"]]
    power_cells = table[table["true_support"]]
    strong = table[table["structure"].isin(STRONG_STRUCTURES)]
    enough = fs_cells["reliable"] >= MIN_RELIABLE_TUNING
    cell_ok = enough & (fs_cells["false_support"] <= FS_MARGIN * fs_cells["reliable"].clip(lower=1))
    strong_reliable = int(strong["reliable"].sum())
    usable = power_cells[power_cells["reliable"] >= MIN_RELIABLE_TUNING]
    mean_power = float((usable["power"] / usable["reliable"]).mean()) if len(usable) else 0.0
    gated = cell_table(method_frame[in_gated(method_frame)], gate)
    gated = gated[~gated["true_support"]]
    gated_ok = (gated["false_support"] / gated["reps"] <= LEAK_MARGIN) & (
        gated["reliable"] / gated["reps"] <= GATED_SHARE_MARGIN
    )
    return {
        "gated_ok_fraction": float(gated_ok.mean()) if len(gated_ok) else 0.0,
        "cells_ok_fraction": float(cell_ok.mean()) if len(cell_ok) else 0.0,
        "reliable_rate": float(table["reliable"].sum() / max(table["reps"].sum(), 1)),
        "strong_power": float(strong["power"].sum() / strong_reliable) if strong_reliable else 0.0,
        "mean_power": mean_power,
        "worst_fs": float(
            (fs_cells["false_support"] / fs_cells["reliable"].clip(lower=1))[enough].max()
        )
        if enough.any()
        else 1.0,
    }


class PreparedMethod:
    """Row arrays of one method for a fast gate scan (numpy only, equal to ``evaluate_gate``).

    A row is reliable under a gate iff it was decided and, in BOTH windows, every feature is inside
    its limit; with the worst case over the two windows precomputed per row, a gate is five vector
    comparisons and the per-cell counts are ``np.bincount`` sums.
    """

    def __init__(
        self,
        method_frame: pd.DataFrame,
        region: tuple[str, ...] | list[str] = REGION_FAMILIES,
        gated: tuple[str, ...] | list[str] = GATED_FAMILIES,
    ) -> None:
        frame = method_frame[in_region(method_frame, region) | in_gated(method_frame, gated)]
        self.is_region = in_region(frame, region).to_numpy()
        codes, cells = pd.factorize(frame["cell"])
        self.codes = codes
        self.cell_count = len(cells)
        info = frame.groupby("cell").agg(
            true_support=("true_support", "first"),
            structure=("structure", "first"),
            is_region=("layer", "size"),
        )
        info = info.loc[cells]
        self.cell_true = info["true_support"].to_numpy(dtype=bool)
        self.cell_strong = info["structure"].isin(STRONG_STRUCTURES).to_numpy()
        self.cell_region = (
            pd.Series(self.is_region).groupby(codes).first().reindex(range(len(cells))).to_numpy()
        )
        self.decided = ((frame["r1"] != "I") & (frame["r2"] != "I")).to_numpy()
        self.supported = ((frame["r1"] == "S") & (frame["r2"] == "S")).to_numpy()
        self.truth = frame["true_support"].to_numpy(dtype=bool)
        self.n_min = np.minimum(frame["n1"], frame["n2"]).to_numpy()
        self.rho1 = np.maximum(frame["rho11"], frame["rho12"]).to_numpy()
        self.rho_s = np.maximum(frame["rho_seasonal1"], frame["rho_seasonal2"]).to_numpy()
        self.rho_s24 = np.nan_to_num(
            np.maximum(frame["rho_seasonal241"], frame["rho_seasonal242"]).to_numpy(), nan=1.0
        )
        self.var = np.maximum(frame["var_ratio1"], frame["var_ratio2"]).to_numpy()
        self.shift = np.maximum(frame["mean_shift1"], frame["mean_shift2"]).to_numpy()
        self.reps = np.bincount(codes, minlength=self.cell_count).astype(float)

    def reliable(self, gate: GateThresholds) -> np.ndarray:
        return (
            self.decided
            & (self.n_min >= gate.n_min)
            & (self.rho1 <= gate.rho1_max)
            & ~((self.rho_s > gate.rho_seasonal_max) & (self.rho_s24 > gate.rho_seasonal24_max))
            & (self.var <= gate.var_ratio_max)
            & (self.shift <= gate.mean_shift_max)
        )

    def stats(self, gate: GateThresholds) -> dict[str, Any]:
        reliable = self.reliable(gate)
        supported = reliable & self.supported
        count = lambda mask: np.bincount(self.codes, weights=mask, minlength=self.cell_count)  # noqa: E731
        rel = count(reliable)
        false_support = count(supported & ~self.truth)
        power = count(supported & self.truth)
        region_fs = self.cell_region & ~self.cell_true
        enough = region_fs & (rel >= MIN_RELIABLE_TUNING)
        rate = false_support / np.maximum(rel, 1)
        cell_ok = enough & (false_support <= FS_MARGIN * np.maximum(rel, 1))
        usable = self.cell_region & self.cell_true & (rel >= MIN_RELIABLE_TUNING)
        strong = self.cell_region & self.cell_strong
        strong_reliable = rel[strong].sum()
        gated_fs = ~self.cell_region & ~self.cell_true
        gated_ok = (false_support / self.reps <= LEAK_MARGIN) & (
            rel / self.reps <= GATED_SHARE_MARGIN
        )
        region_reps = self.reps[self.cell_region].sum()
        return {
            "gated_ok_fraction": float(gated_ok[gated_fs].mean()) if gated_fs.any() else 0.0,
            "cells_ok_fraction": float(cell_ok[region_fs].mean()) if region_fs.any() else 0.0,
            "reliable_rate": float(rel[self.cell_region].sum() / max(region_reps, 1)),
            "strong_power": float(power[strong].sum() / strong_reliable)
            if strong_reliable
            else 0.0,
            "mean_power": float((power[usable] / rel[usable]).mean()) if usable.any() else 0.0,
            "worst_fs": float(rate[enough].max()) if enough.any() else 1.0,
        }


def qualifies(stats: dict[str, Any]) -> bool:
    return (
        stats["cells_ok_fraction"] >= 1.0
        and stats["gated_ok_fraction"] >= 1.0
        and stats["reliable_rate"] >= 0.6
        and stats["strong_power"] >= 0.5
    )


def tune_gate(
    method_frame: pd.DataFrame,
    region: tuple[str, ...] | list[str] = REGION_FAMILIES,
    gated: tuple[str, ...] | list[str] = GATED_FAMILIES,
) -> tuple[GateThresholds | None, dict[str, Any] | None]:
    """The qualifying gate with the highest mean R power (``None`` if no gate qualifies)."""
    prepared = PreparedMethod(method_frame, region, gated)
    best: tuple[float, float, GateThresholds, dict[str, Any]] | None = None
    for values in itertools.product(*GRID.values()):
        gate = GateThresholds(**dict(zip(GRID, values, strict=True)))
        stats = prepared.stats(gate)
        if not qualifies(stats):
            continue
        key = (stats["mean_power"], stats["reliable_rate"])
        if best is None or key > best[:2]:
            best = (stats["mean_power"], stats["reliable_rate"], gate, stats)
    if best is None:
        return None, None
    return best[2], best[3]


def family_rank(method: str) -> int:
    for rank, prefix in enumerate(SIMPLICITY):
        if method.startswith(prefix):
            return rank
    return len(SIMPLICITY)


def pick_method(candidates: dict[str, dict[str, Any]]) -> str | None:
    """Highest mean power; powers within ``TIE`` count as equal and go to the simplest family."""
    if not candidates:
        return None
    top = max(item["stats"]["mean_power"] for item in candidates.values())
    tied = [m for m, item in candidates.items() if top - item["stats"]["mean_power"] <= TIE]
    return sorted(tied, key=lambda m: (family_rank(m), -candidates[m]["stats"]["mean_power"], m))[0]


def _tune_pass(
    frame: pd.DataFrame, region: tuple[str, ...] | list[str], gated: tuple[str, ...] | list[str]
) -> dict[str, Any]:
    methods: dict[str, Any] = {}
    for method, part in frame.groupby("method"):
        entry: dict[str, Any] = {"eligible": covers_all_driver_counts(part)}
        if entry["eligible"]:
            gate, stats = tune_gate(part, region, gated)
            entry["gate"] = asdict(gate) if gate else None
            entry["stats"] = stats
        else:
            entry["gate"] = None
            entry["stats"] = None
        methods[method] = entry
    qualifying = {m: v for m, v in methods.items() if v["eligible"] and v["gate"] is not None}
    return {
        "region_families": list(region),
        "gated_families": list(gated),
        "methods": methods,
        "selected": pick_method(qualifying),
    }


def tune(
    directory: Path | list[Path],
    features_dir: Path | None = None,
    only_methods: list[str] | None = None,
) -> dict[str, Any]:
    """Tuning analysis (protocol v1.5): eligibility filter, then ranking, with the single
    pre-registered fallback (``sar12_05`` moves to the gated class if nothing is eligible)."""
    raw = load_records(directory)
    if only_methods is not None:
        failed = raw.attrs.get("failed", {})
        raw = raw[raw["method"].isin(only_methods)].copy()
        raw.attrs["failed"] = failed
    if features_dir is not None:
        raw = attach_features(raw, features_dir)
    frame = with_composites(raw)
    report: dict[str, Any] = {
        "records": int(len(frame)),
        "failed": {f"{cell}|{method}": n for (cell, method), n in frame.attrs["failed"].items()},
    }
    first = _tune_pass(frame, REGION_FAMILIES, GATED_FAMILIES)
    report["pass1"] = first
    if first["selected"] is not None:
        report.update(selected=first["selected"], sar12_05_moved=False, final=first)
        return report
    moved_region = tuple(f for f in REGION_FAMILIES if f != "sar12_05")
    second = _tune_pass(frame, moved_region, (*GATED_FAMILIES, "sar12_05"))
    report["pass2_sar12_05_gated"] = second
    report.update(selected=second["selected"], sar12_05_moved=second["selected"] is not None)
    report["final"] = second if second["selected"] is not None else first
    return report


def cell_kind(cell_id: str) -> str:
    """``gated`` (the gate must reject it), ``acceptance`` (region and held-out families) or
    ``report`` (everything else: reported, never decisive)."""
    family = parse_cell(cell_id)["family"]
    if family in GATED_FAMILIES:
        return "gated"
    if family in REGION_FAMILIES or family in HELD_OUT_FAMILIES:
        return "acceptance"
    return "report"


def validate(directory: Path | list[Path], freeze: dict[str, Any]) -> dict[str, Any]:
    """Acceptance of the frozen method on the untouched validation records (protocol v1.3).

    ``acceptance`` cells: >= ``MIN_RELIABLE_VALIDATION`` reliable replicates and a one-sided 95 %
    Clopper-Pearson upper bound of P(false support | reliable) below ``CEILING``. ``gated`` cells:
    upper bound of P(reliable and false support) over ALL replicates below ``LEAK_CEILING`` and
    upper bound of P(reliable) below ``GATED_RELIABLE_CAP``. Intersection-union: every cell of
    those two kinds must pass; ``report`` cells never decide.
    """
    gate = GateThresholds(**freeze["gate"]) if freeze.get("gate") else None
    frame = with_composites(load_records(directory), [freeze["method"]])
    method_frame = frame[frame["method"] == freeze["method"]]
    expected_cells = set(freeze["cells"])
    budget = int(freeze["budget_reps"])
    table = cell_table(method_frame, gate)
    missing = sorted(expected_cells - set(table.index))
    incomplete = sorted(c for c in table.index if int(table.loc[c, "reps"]) != budget)
    failed = frame.attrs["failed"]
    cells_report: dict[str, Any] = {}
    deciding = [c for c in expected_cells if cell_kind(c) != "report"]
    confidence_adjusted = 1.0 - CEILING / max(len(deciding), 1)
    for cell, row in table.iterrows():
        reps, reliable, errors = int(row["reps"]), int(row["reliable"]), int(row["false_support"])
        kind = cell_kind(cell)
        failures = sum(n for (c, _), n in failed.items() if c == cell)
        entry: dict[str, Any] = {
            "kind": kind,
            "reps": reps,
            "reliable": reliable,
            "false_support": errors,
            "failed_calls": failures,
            "failure_alert": failures / max(reps, 1) > FAILURE_ALERT,
        }
        if kind == "gated":
            leak_upper = clopper_pearson_upper(errors, reps)
            reliable_upper = clopper_pearson_upper(reliable, reps)
            entry.update(
                leak_upper95=leak_upper,
                reliable_share_upper95=reliable_upper,
                enough_reliable=True,
                passes=leak_upper < LEAK_CEILING and reliable_upper < GATED_RELIABLE_CAP,
            )
        else:
            upper = clopper_pearson_upper(errors, reliable)
            entry.update(
                rate=errors / reliable if reliable else None,
                upper95=upper,
                upper_bonferroni=clopper_pearson_upper(errors, reliable, confidence_adjusted),
                enough_reliable=reliable >= MIN_RELIABLE_VALIDATION,
                passes=reliable >= MIN_RELIABLE_VALIDATION and upper < CEILING,
            )
        cells_report[cell] = entry
    decisive = {c: v for c, v in cells_report.items() if v["kind"] != "report"}
    all_pass = (
        not missing
        and not incomplete
        and bool(decisive)
        and all(item["passes"] for item in decisive.values())
    )
    return {
        "method": freeze["method"],
        "verdict": "ACCEPTED" if all_pass else "REJECTED",
        "missing_cells": missing,
        "incomplete_cells": incomplete,
        "cells_failing": sorted(c for c, v in decisive.items() if not v["passes"]),
        "cells_without_enough_reliable": sorted(
            c for c, v in decisive.items() if not v["enough_reliable"]
        ),
        "failure_alerts": sorted(c for c, v in cells_report.items() if v["failure_alert"]),
        "bonferroni_all_pass": all(
            v["upper_bonferroni"] < CEILING and v["enough_reliable"]
            for v in decisive.values()
            if v["kind"] == "acceptance"
        ),
        "cells": cells_report,
    }


def _clean(value: Any) -> Any:
    if isinstance(value, float) and not np.isfinite(value):
        return "inf" if value > 0 else "-inf"
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.analysis.calib.accept", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    tune_parser = sub.add_parser("tune", help="tune the gates and select the method")
    tune_parser.add_argument("--dir", action="append", required=True)
    tune_parser.add_argument("--features", default=None, help="dir of the lag-24 feature re-run")
    tune_parser.add_argument(
        "--methods", default=None, help="comma list: analyse only these methods"
    )
    tune_parser.add_argument("--out", required=True)
    validate_parser = sub.add_parser("validate", help="accept or reject the frozen method")
    validate_parser.add_argument("--dir", required=True)
    validate_parser.add_argument("--freeze", required=True)
    validate_parser.add_argument("--out", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "tune":
        report = tune(
            [Path(d) for d in args.dir],
            Path(args.features) if args.features else None,
            args.methods.split(",") if args.methods else None,
        )
        Path(args.out).write_text(json.dumps(_clean(report), indent=1), encoding="utf-8")
        print(
            "selected:", report["selected"], "| sar12_05 moved to gated:", report["sar12_05_moved"]
        )
        for label in ("pass1", "pass2_sar12_05_gated"):
            if label not in report:
                continue
            print("--", label, "region:", report[label]["region_families"])
            for method, data in report[label]["methods"].items():
                if not data["eligible"]:
                    print("  ", method, "not eligible (does not cover k = 1, 2, 3)")
                elif data["gate"] is None:
                    print("  ", method, "NOT ELIGIBLE under the v1.5 filter")
                else:
                    print("  ", method, "eligible", _clean(data["stats"]))
        return 0
    freeze = json.loads(Path(args.freeze).read_text(encoding="utf-8"))
    report = validate(Path(args.dir), freeze)
    Path(args.out).write_text(json.dumps(_clean(report), indent=1), encoding="utf-8")
    print(report["verdict"], "failing:", report["cells_failing"][:10])
    return 0 if report["verdict"] == "ACCEPTED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
