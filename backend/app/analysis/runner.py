"""Test a relation end to end: load from the database, run the engine, prepare for the graph.

``run_relation`` returns ``MissingData`` (some series are not loaded: nothing is tested),
``Unsupported`` (a series cannot enter a test) or a ``TestReport``.
``to_graph_period_results`` turns a report into the two ``PeriodResult`` records that
``app.graph.repository.add_test`` stores together with ``report.status``; the reliability
warning travels in the explicit ``reliable`` / ``reliability_reason`` fields (Task 3.1).
"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.analysis.engine import EngineConfig, run_relation_test
from app.analysis.errors import UnsupportedSeriesError
from app.analysis.gate import GateThresholds
from app.analysis.loader import DEFLATOR_KEY, load_series
from app.analysis.models import (
    PERIODS,
    MissingData,
    RelationSpec,
    TestReport,
    Unsupported,
    WindowResult,
)
from app.analysis.resampling import WholeChainBootstrap
from app.analysis.significance import SignificanceMethod
from app.graph.models import PeriodResult

#: EXPERIMENTAL defaults (Task 3.2). The calibration (docs/calibration/RESULT.md) found NO candidate
#: that met the pre-registered acceptance criteria, so this method is not validated: it was
#: picked on the tuning results (lowest false-support profile we measured) and every result
#: stays ``reliable=False`` (``calibrated=False``). The gate only records where the data leave
#: the region in which the tuning data were collected (``details["gate_passed"]``).
EXPERIMENTAL_METHOD = WholeChainBootstrap(block=24, replicates=499, seed=0, calibrated=False)
EXPERIMENTAL_GATE = GateThresholds(
    n_min=100,
    rho1_max=1.01,
    rho_seasonal_max=0.3,
    var_ratio_max=float("inf"),
    mean_shift_max=float("inf"),
    rho_seasonal24_max=0.2,
)


def experimental_config() -> EngineConfig:
    return EngineConfig(gate=EXPERIMENTAL_GATE)


def run_relation(
    session: Session,
    spec: RelationSpec,
    *,
    method: SignificanceMethod | None = None,
    config: EngineConfig | None = None,
) -> TestReport | MissingData | Unsupported:
    """Load every series of ``spec`` (and the CPI deflator when needed) and test it.

    Without ``method`` / ``config`` the experimental, NOT validated defaults are used.
    """
    method = method or EXPERIMENTAL_METHOD
    config = config or experimental_config()
    keys = [spec.target, *spec.drivers]
    if spec.nominal_tl_series:
        keys.append(DEFLATOR_KEY)
    loaded = {}
    unusable: dict[str, str] = {}
    for key in dict.fromkeys(keys):
        try:
            loaded[key] = load_series(session, key)
        except UnsupportedSeriesError as error:
            unusable[error.key] = error.reason
    if unusable:
        return Unsupported(unusable)
    missing = tuple(key for key, data in loaded.items() if data is None)
    if missing:
        return MissingData(missing)
    deflator = loaded[DEFLATOR_KEY] if spec.nominal_tl_series else None
    return run_relation_test(
        loaded[spec.target],
        [loaded[key] for key in spec.drivers],
        spec,
        deflator=deflator,
        method=method,
        config=config,
    )


def _period_result(window: WindowResult, transform: str) -> PeriodResult:
    return PeriodResult(
        period=window.period,
        result=window.result,
        n_obs=window.n_obs,
        best_lag=window.best_lag,
        statistic_name=window.statistic_name,
        statistic_value=window.statistic_value,
        p_value=window.p_value,
        start=window.start,
        end=window.end,
        transform=transform,
        data_range_start=window.data_range_start,
        data_range_end=window.data_range_end,
        details=json.dumps(window.details, sort_keys=True, default=str),
        reliable=window.reliable,
        reliability_reason=window.reliability_reason,
    )


def to_graph_period_results(report: TestReport) -> list[PeriodResult]:
    """The two graph period results of a report, in the graph's period order."""
    return [_period_result(report.windows[period], report.transform) for period in PERIODS]
