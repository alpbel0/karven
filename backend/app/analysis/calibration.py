"""Calibration study for the significance method (Task 3.2, part 2).

The p-value method is chosen by measurement, not by argument (user decision 2026-10-05):
synthetic series with a KNOWN truth are pushed through the real chain
(``engine.prepare`` -> lag search -> test) and every candidate method is scored on

- false-positive rate: how often it says ``supported`` when there is no relation (null),
- power: how often it says ``supported`` when there is a relation,
- no-result rate: how often the window is ``insufficient_data``,
- correct-lag rate: how often the true lag is found (real scenarios),

with Monte Carlo standard errors. Samples are counted AFTER transform, alignment and lag.
Candidates: ``HacOls`` over a ``maxlags`` grid with the lag-search correction on and off
(pairwise and regression), and a circular-shift surrogate test (pairwise only, the whole lag
search is repeated on every shift). Tuning and validation use different seeds.

    python -m app.analysis.calibration run --reps 200 --out calibration.json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from app.analysis.engine import EngineConfig, prepare, run_window
from app.analysis.models import (
    PERIOD_ALL,
    RESULT_INSUFFICIENT,
    RESULT_SUPPORTED,
    RelationSpec,
    SeriesData,
)
from app.analysis.significance import HacOls, SignificanceMethod
from app.analysis.slots import slot_of
from app.analysis.windows import window_slots

START = slot_of(date(2005, 1, 1), 1)
SD = 0.01  # monthly growth standard deviation (1 %)


# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Scenario:
    """One simulated world."""

    name: str
    n_after: int  # usable observations after the transform (before the lag search)
    transform: str  # annual_pct_change | period_pct_change | difference
    dependence: str  # iid | ar05 | ar09 | seasonal | breaks | heavy
    beta: float = 0.0  # effect of the driver growth on the target growth (0 = null)
    lag: int = 1
    drivers: int = 1  # 1 = pairwise; 2 = regression with a second, irrelevant driver
    lag_max: int = 3


def _innovations(rng: np.random.Generator, n: int, dependence: str) -> np.ndarray:
    """Standardised growth innovations with the requested dependence structure."""
    if dependence == "heavy":
        base = rng.standard_t(3, size=n) / math.sqrt(3.0)
    else:
        base = rng.normal(size=n)
    if dependence in ("ar05", "ar09"):
        phi = 0.5 if dependence == "ar05" else 0.9
        out = np.empty(n)
        out[0] = base[0]
        for i in range(1, n):
            out[i] = phi * out[i - 1] + math.sqrt(1 - phi**2) * base[i]
        return out
    if dependence == "seasonal":
        month = np.arange(n) % 12
        return base * 0.6 + 0.8 * np.sin(2 * math.pi * month / 12.0)
    if dependence == "breaks":
        scale = np.where(np.arange(n) < n // 2, 0.5, 2.0)
        return base * scale
    return base


def _levels(growth: np.ndarray) -> np.ndarray:
    return 100.0 * np.exp(np.cumsum(growth * SD))


def simulate(scenario: Scenario, rng: np.random.Generator) -> tuple[list[SeriesData], RelationSpec]:
    """Series (target first) and the locked hypothesis for one replication."""
    extra = 12 if scenario.transform == "annual_pct_change" else 1
    n = scenario.n_after + extra + scenario.lag_max + 1
    driver_growth = [_innovations(rng, n, scenario.dependence) for _ in range(scenario.drivers)]
    own = _innovations(rng, n, scenario.dependence)
    target_growth = own.copy()
    if scenario.beta:
        shifted = np.zeros(n)
        shifted[scenario.lag :] = (
            driver_growth[0][: n - scenario.lag] if scenario.lag else driver_growth[0]
        )
        target_growth = target_growth + scenario.beta * shifted / max(np.std(shifted), 1e-9)
    index = range(START, START + n)

    def series(key: str, growth: np.ndarray) -> SeriesData:
        return SeriesData(
            key=key,
            frequency="monthly",
            step=1,
            values=pd.Series(_levels(growth), index=list(index)),
            aggregation="ortalama",
            measure_type="endeks",
        )

    target = series("T", target_growth)
    drivers = [series(f"D{i}", growth) for i, growth in enumerate(driver_growth)]
    spec = RelationSpec(
        "T",
        {item.key: "positive" for item in drivers},
        0,
        scenario.lag_max,
        scenario.transform,
    )
    return [target, *drivers], spec


# --------------------------------------------------------------------------- #
# Methods
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Outcome:
    """What one method concluded in one replication."""

    supported: bool
    insufficient: bool
    best_lag: int | None = None


def hac_method(method: SignificanceMethod) -> Callable[..., Outcome]:
    def run(prepared, spec, step, scenario, rng, config) -> Outcome:
        window = run_window(prepared, spec, step, PERIOD_ALL, method, config)
        return Outcome(
            supported=window.result == RESULT_SUPPORTED,
            insufficient=window.result == RESULT_INSUFFICIENT,
            best_lag=window.best_lag,
        )

    return run


def circular_shift_method(shifts: int = 199, guard: int = 12) -> Callable[..., Outcome]:
    """Pairwise only: p = share of circular shifts of the driver whose best |r| (over the same
    lag range) reaches the observed best |r|. Shifts closer than ``guard`` periods to zero
    are excluded (the overlap would leave them related)."""

    def run(prepared, spec, step, scenario, rng, config) -> Outcome:
        key = spec.drivers[0]
        joined = pd.concat([prepared[spec.target].rename("y"), prepared[key].rename("x")], axis=1)
        joined = joined.dropna()
        lo, hi = window_slots(PERIOD_ALL, step)
        y = joined["y"].to_numpy()
        x = joined["x"].to_numpy()
        n = len(y)
        lags = range(spec.lag_min, spec.lag_max + 1)
        if n - spec.lag_max < config.min_obs[step]:
            return Outcome(False, True)

        def best_abs_r(driver: np.ndarray) -> tuple[float, int, float]:
            best = (-1.0, 0, 0.0)
            for lag in lags:
                a = driver[: n - lag] if lag else driver
                b = y[lag:]
                if np.std(a) == 0 or np.std(b) == 0:
                    continue
                r = float(np.corrcoef(a, b)[0, 1])
                if abs(r) > best[0]:
                    best = (abs(r), lag, r)
            return best

        observed_abs, lag, r = best_abs_r(x)
        offsets = [s for s in range(guard, n - guard)]
        if not offsets:
            return Outcome(False, True)
        chosen = rng.choice(offsets, size=min(shifts, len(offsets)), replace=False)
        exceed = sum(1 for s in chosen if best_abs_r(np.roll(x, int(s)))[0] >= observed_abs)
        p_value = (exceed + 1) / (len(chosen) + 1)
        supported = r > 0 and p_value < config.alpha
        return Outcome(supported, False, lag)

    return run


def default_methods() -> dict[str, Callable[..., Outcome]]:
    methods: dict[str, Callable[..., Outcome]] = {}
    for label, maxlags in (("rule", None), ("11", 11), ("18", 18), ("24", 24)):
        for correction in (False, True):
            name = f"hac_{label}" + ("_bonf" if correction else "")
            methods[name] = hac_method(HacOls(maxlags=maxlags, lag_correction=correction))
    methods["shift"] = circular_shift_method()
    return methods


# --------------------------------------------------------------------------- #
# Study
# --------------------------------------------------------------------------- #


@dataclass
class CellResult:
    scenario: str
    method: str
    reps: int
    supported: int = 0
    insufficient: int = 0
    lag_correct: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def rate(self) -> float:
        return self.supported / self.reps

    @property
    def se(self) -> float:
        p = self.rate
        return math.sqrt(p * (1 - p) / self.reps)


def run_cell(
    scenario: Scenario,
    methods: dict[str, Callable[..., Outcome]],
    reps: int,
    seed: int,
) -> list[CellResult]:
    """Run ``reps`` replications of one scenario through every method."""
    rng = np.random.default_rng(seed)
    config = EngineConfig()
    cells = {name: CellResult(scenario.name, name, reps) for name in methods}
    for _ in range(reps):
        series, spec = simulate(scenario, rng)
        prepared, step = prepare(series, spec, deflator=None)
        for name, method in methods.items():
            if scenario.drivers > 1 and name == "shift":
                continue  # pairwise only
            outcome = method(prepared, spec, step, scenario, rng, config)
            cells[name].supported += outcome.supported
            cells[name].insufficient += outcome.insufficient
            if scenario.beta and outcome.best_lag == scenario.lag:
                cells[name].lag_correct += 1
    return [cell for name, cell in cells.items() if not (scenario.drivers > 1 and name == "shift")]


def default_scenarios() -> list[Scenario]:
    scenarios: list[Scenario] = []
    for transform in ("annual_pct_change", "difference"):
        for dependence in ("iid", "ar05", "ar09", "seasonal", "breaks", "heavy"):
            for n_after in (36, 60, 120, 250):
                tag = f"{transform}/{dependence}/n{n_after}"
                scenarios.append(Scenario(f"null/{tag}", n_after, transform, dependence))
                scenarios.append(Scenario(f"real/{tag}", n_after, transform, dependence, beta=0.5))
    for n_after in (60, 120, 250):
        for transform in ("annual_pct_change", "difference"):
            tag = f"{transform}/iid/n{n_after}"
            scenarios.append(
                Scenario(f"regression-null/{tag}", n_after, transform, "iid", drivers=2)
            )
            scenarios.append(
                Scenario(f"regression-real/{tag}", n_after, transform, "iid", beta=0.5, drivers=2)
            )
    return scenarios


def summarise(results: Sequence[CellResult]) -> list[dict[str, Any]]:
    rows = []
    for cell in results:
        rows.append(
            {
                "scenario": cell.scenario,
                "method": cell.method,
                "reps": cell.reps,
                "supported_rate": round(cell.rate, 4),
                "se": round(cell.se, 4),
                "insufficient_rate": round(cell.insufficient / cell.reps, 4),
                "lag_correct_rate": round(cell.lag_correct / cell.reps, 4),
            }
        )
    return rows


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.analysis.calibration", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run the study and write JSON")
    run.add_argument("--reps", type=int, default=200)
    run.add_argument("--seed", type=int, default=20261006)
    run.add_argument("--only", default=None, help="substring filter on scenario names")
    run.add_argument("--out", default="calibration.json")
    run.add_argument("--jobs", type=int, default=1)
    return parser


def _run_cell_job(args: tuple[Scenario, int, int]) -> list[dict[str, Any]]:
    scenario, reps, seed = args
    return summarise(run_cell(scenario, default_methods(), reps, seed))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    scenarios = [s for s in default_scenarios() if not args.only or args.only in s.name]
    jobs = [(scenario, args.reps, args.seed + index) for index, scenario in enumerate(scenarios)]
    rows: list[dict[str, Any]] = []
    if args.jobs > 1:
        from concurrent.futures import ProcessPoolExecutor

        with ProcessPoolExecutor(max_workers=args.jobs) as pool:
            for done, part in enumerate(pool.map(_run_cell_job, jobs), start=1):
                rows.extend(part)
                print(f"{done}/{len(jobs)} scenarios", file=sys.stderr, flush=True)
    else:
        for done, job in enumerate(jobs, start=1):
            rows.extend(_run_cell_job(job))
            print(f"{done}/{len(jobs)} scenarios", file=sys.stderr, flush=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "reps": args.reps,
                "seed": args.seed,
                "rows": rows,
                "scenarios": [asdict(s) for s in scenarios],
            },
            handle,
            indent=1,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
