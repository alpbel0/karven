"""The relation test (DECISIONS §8): prepare the series, search the lag, test two windows.

``run_relation_test`` is pure (no database): it takes loaded ``SeriesData`` and the locked
``RelationSpec`` and returns a ``TestReport`` (or ``Unsupported``). The decisions it
implements:

- series are reduced to the lowest frequency of the relation (rule from the catalog
  measure), an incomplete period is dropped, nominal TL series are divided by the CPI,
  then the hypothesis transform is applied (never a raw-level test);
- two windows: all years and 2017-2026, each using the longest contiguous block of
  periods (a calendar gap is never filled or squeezed);
- 2 series: the lag in the hypothesis range with the strongest |r|; supported only if the
  sign matches the expected direction and p < alpha (a reversed sign is unsupported);
- 3+ series: every driver gets its lag from its own pairwise search, then one OLS with
  all lagged drivers; every coefficient must match its direction and be significant;
- minimum observations: 36 monthly / 12 quarterly / 10 annual (18 semiannual is our own
  default, the decision text does not name it), times the number of drivers for a
  regression; below that the window is ``insufficient_data``;
- status: both windows supported -> supported, both unsupported -> unsupported, one of each
  -> periods_differ, any insufficient window -> insufficient_data (no verdict on partial
  evidence).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np
import pandas as pd

from app.analysis.errors import SingularDesignError, UnsupportedSeriesError
from app.analysis.frequency import aggregate
from app.analysis.gate import GateThresholds, assess, window_features
from app.analysis.models import (
    PERIODS,
    RESULT_INSUFFICIENT,
    RESULT_SUPPORTED,
    RESULT_UNSUPPORTED,
    STATUS_INSUFFICIENT,
    STATUS_PERIODS_DIFFER,
    STATUS_SUPPORTED,
    STATUS_UNSUPPORTED,
    RelationSpec,
    SeriesData,
    TestReport,
    Unsupported,
    WindowResult,
)
from app.analysis.significance import (
    UNCALIBRATED_REASON,
    BlockProblem,
    FitContext,
    FitResult,
    HacOls,
    SignificanceMethod,
)
from app.analysis.slots import date_of
from app.analysis.transforms import apply_transform, deflate
from app.analysis.windows import longest_block, window_slots

DEFAULT_MIN_OBS: dict[int, int] = {1: 36, 3: 12, 6: 18, 12: 10}


@dataclass(frozen=True)
class EngineConfig:
    """Fixed thresholds of the engine (the p-value method is passed separately)."""

    min_obs: Mapping[int, int] = field(default_factory=lambda: dict(DEFAULT_MIN_OBS))
    alpha: float = 0.05
    gate: GateThresholds | None = None  # off until the calibration freeze


# --------------------------------------------------------------------------- #
# Preparation
# --------------------------------------------------------------------------- #


def prepare(
    series: Sequence[SeriesData],
    spec: RelationSpec,
    *,
    deflator: SeriesData | None,
) -> tuple[dict[str, pd.Series], int]:
    """Deflate, reduce to the lowest frequency and transform every series.

    Returns the transformed series by key and the common period length in months.
    Raises ``UnsupportedSeriesError`` for the first series that cannot be used.
    """
    for item in series:
        if item.aggregation == "test_disi":
            raise UnsupportedSeriesError(
                item.key, "measure type is not testable (weight/contribution/distribution)"
            )
    target_step = max(item.step for item in series)
    prepared: dict[str, pd.Series] = {}
    for item in series:
        values = item.values.astype("float64")
        if item.key in spec.nominal_tl_series:
            if deflator is None:
                raise UnsupportedSeriesError(
                    item.key, "nominal TL series but no CPI deflator was given"
                )
            values = deflate(
                values,
                item.step,
                deflator.values,
                deflator.step,
                key=item.key,
                cpi_key=deflator.key,
            )
        values = aggregate(
            values,
            item.step,
            target_step,
            item.aggregation,
            cumulative=item.cumulative,
            key=item.key,
        )
        if item.aggregation == "yeniden_hesapla":
            # Already a percent change at this frequency: used as it is.
            transformed = values.dropna()
        else:
            transformed = apply_transform(values, spec.transform, target_step, key=item.key)
        prepared[item.key] = transformed
    return prepared, target_step


# --------------------------------------------------------------------------- #
# One window
# --------------------------------------------------------------------------- #


def _col(key: str, lag: int) -> str:
    return f"{key}@{lag}"


def _common_block(
    y: pd.Series,
    prepared: Mapping[str, pd.Series],
    spec: RelationSpec,
    lo: int | None,
    hi: int | None,
) -> pd.DataFrame:
    """The longest contiguous block of target slots where y and EVERY lagged driver column exist.

    One common set of dates for all candidate lags and all drivers (Task 3.2 decision after the
    second outside review): lags are compared on the same sample, the edge loss is applied once
    (``lag_max`` periods) and the observed test and any resampling test use the same dates.
    """
    if y.empty:
        return pd.DataFrame()
    grid = range(int(y.index.min()), int(y.index.max()) + 1)
    columns: dict[str, pd.Series] = {"y": y.reindex(grid)}
    for key in spec.drivers:
        for lag in range(spec.lag_min, spec.lag_max + 1):
            columns[_col(key, lag)] = _lagged(prepared[key], lag, grid)
    frame = pd.DataFrame(columns)
    if lo is not None:
        frame = frame.loc[frame.index >= lo]
    if hi is not None:
        frame = frame.loc[frame.index <= hi]
    return longest_block(frame)


def _lagged(series: pd.Series, lag: int, grid: range) -> pd.Series:
    """``series`` moved ``lag`` periods forward on the full grid (driver leads the target)."""
    low = min(int(series.index.min()), grid.start)
    high = max(int(series.index.max()), grid.stop - 1)
    full = series.reindex(range(low, high + 1))
    return full.shift(lag).reindex(grid)


@dataclass(frozen=True)
class _PairFit:
    lag: int
    r: float
    n_obs: int
    fit: FitResult
    block: pd.DataFrame


def _constant(values: np.ndarray) -> bool:
    return bool(np.ptp(values) == 0.0)


def _search_lag(
    block: pd.DataFrame,
    key: str,
    spec: RelationSpec,
    step: int,
    method: SignificanceMethod,
) -> _PairFit | None:
    """Best lag (strongest |r|) for one driver on the common block; ``None`` if all are constant."""
    lags = range(spec.lag_min, spec.lag_max + 1)
    yv = block["y"].to_numpy()
    if _constant(yv):
        return None
    best: tuple[int, float] | None = None
    for lag in lags:
        xv = block[_col(key, lag)].to_numpy()
        if _constant(xv):
            continue
        r = float(np.corrcoef(xv, yv)[0, 1])
        if best is None or abs(r) > abs(best[1]):
            best = (lag, r)
    if best is None:
        return None
    lag, r = best
    xv = block[_col(key, lag)].to_numpy()
    fit = method.fit(yv, xv, FitContext(spec.transform, step, lags_searched=len(lags)))
    return _PairFit(lag, r, len(block), fit, block)


def _sign_ok(coefficient: float, direction: str) -> bool:
    return coefficient > 0 if direction == "positive" else coefficient < 0


def _data_range(prepared: Mapping[str, pd.Series], step: int) -> tuple[Any, Any]:
    first = max(int(item.index.min()) for item in prepared.values() if not item.empty)
    last = min(int(item.index.max()) for item in prepared.values() if not item.empty)
    if first > last:
        return None, None
    return date_of(first, step), date_of(last, step)


def _run_window_inner(
    prepared: Mapping[str, pd.Series],
    spec: RelationSpec,
    step: int,
    period: str,
    method: SignificanceMethod,
    config: EngineConfig,
) -> WindowResult:
    """Test the relation in one window."""
    lo, hi = window_slots(period, step)
    range_start, range_end = _data_range(prepared, step)
    common = dict(period=period, data_range_start=range_start, data_range_end=range_end)
    min_n = config.min_obs[step]
    y = prepared[spec.target]
    drivers = spec.drivers

    block = _common_block(y, prepared, spec, lo, hi)
    needed = min_n * len(drivers)
    if len(block) < needed:
        return WindowResult(
            result=RESULT_INSUFFICIENT,
            n_obs=len(block),
            details={"reason": "too few common observations", "needed": needed},
            **common,
        )

    features = window_features(
        block["y"].to_numpy(),
        [block[_col(key, spec.lag_min)].to_numpy() for key in drivers],
        step,
    )
    result = _decide(block, prepared, spec, step, method, config, common)
    return _finish(result, features, config, method)


def run_window(
    prepared: Mapping[str, pd.Series],
    spec: RelationSpec,
    step: int,
    period: str,
    method: SignificanceMethod,
    config: EngineConfig,
) -> WindowResult:
    """Test the relation in one window. A window that cannot be decided (too few observations, a
    constant or singular series) is never reliable."""
    result = _run_window_inner(prepared, spec, step, period, method, config)
    if result.result == RESULT_INSUFFICIENT:
        reason = str(result.details.get("reason", "too few observations"))
        details = {
            "method_validated": bool(getattr(method, "calibrated", False)),
            "gate_passed": None,
            "structurally_eligible": False,
            **result.details,
        }
        return replace(
            result,
            reliable=False,
            reliability_reason=f"insufficient data: {reason}",
            details=details,
        )
    return result


def _finish(
    result: WindowResult, features: dict, config: EngineConfig, method: SignificanceMethod
) -> WindowResult:
    """Attach the gate features and the validation facts; combine the reliability reasons.

    A window is reliable only if the significance method is validated (``calibrated``), the gate
    (when one is configured) passes and the window is structurally decidable. The three facts are
    stored separately (``method_validated``, ``gate_passed``, ``structurally_eligible``) so that
    a gate pass can never be read as a validation (Codex review, 2026-10-07).
    """
    validated = bool(getattr(method, "calibrated", False))
    reasons: list[str] = []
    if result.reliability_reason:
        reasons.append(result.reliability_reason)
    gate_passed: bool | None = None
    if config.gate is not None and result.result != RESULT_INSUFFICIENT:
        gate_passed, gate_reason = assess(features, config.gate)
        if not gate_passed and gate_reason:
            reasons.append(gate_reason)
    reliable = result.reliable and validated and gate_passed is not False
    if not validated and not any(reason == UNCALIBRATED_REASON for reason in reasons):
        reasons.insert(0, UNCALIBRATED_REASON)
    facts = {
        "method_validated": validated,
        "gate_passed": gate_passed,
        "structurally_eligible": result.result != RESULT_INSUFFICIENT,
    }
    return replace(
        result,
        reliable=reliable,
        reliability_reason=None if reliable else "; ".join(reasons) or None,
        details={**result.details, "features": features, **facts},
    )


def _decide(
    block: pd.DataFrame,
    prepared: Mapping[str, pd.Series],
    spec: RelationSpec,
    step: int,
    method: SignificanceMethod,
    config: EngineConfig,
    common: dict[str, Any],
) -> WindowResult:
    """The window decision on the common block (HAC path or a block method)."""
    drivers = spec.drivers
    if hasattr(method, "test_block"):
        return _run_block_method(block, prepared, spec, step, method, config, common)

    searched: dict[str, _PairFit] = {}
    for key in drivers:
        best = _search_lag(block, key, spec, step, method)
        if best is None:
            return WindowResult(
                result=RESULT_INSUFFICIENT,
                n_obs=len(block),
                details={"reason": "a constant series", "driver": key},
                **common,
            )
        searched[key] = best

    if len(drivers) == 1:
        key = drivers[0]
        found = searched[key]
        slope = found.fit.coefficients[1]
        p_value = found.fit.p_values[1]
        supported = _sign_ok(slope, spec.driver_directions[key]) and p_value < config.alpha
        return _window_result(
            common,
            supported,
            block,
            found.fit,
            step,
            best_lag=found.lag,
            statistic_name="pearson_r",
            statistic_value=found.r,
            p_value=p_value,
            details={"lags": {key: found.lag}, "slope": slope, "r": found.r},
        )

    # Regression: every driver at its own lag, on the same common block.
    regression = pd.DataFrame(
        {key: block[_col(key, searched[key].lag)] for key in drivers} | {"y": block["y"]}
    )
    block = regression
    x_values = block[list(drivers)].to_numpy()
    y_values = block["y"].to_numpy()
    if any(_constant(x_values[:, i]) for i in range(x_values.shape[1])) or _constant(y_values):
        return WindowResult(
            result=RESULT_INSUFFICIENT,
            n_obs=len(block),
            details={"reason": "a constant series in the regression block"},
            **common,
        )
    lags_searched = spec.lag_max - spec.lag_min + 1
    fit = method.fit(y_values, x_values, FitContext(spec.transform, step, lags_searched))
    coefficients = {key: fit.coefficients[i + 1] for i, key in enumerate(drivers)}
    p_values = {key: fit.p_values[i + 1] for i, key in enumerate(drivers)}
    supported = all(
        _sign_ok(coefficients[key], spec.driver_directions[key]) and p_values[key] < config.alpha
        for key in drivers
    )
    return _window_result(
        common,
        supported,
        block,
        fit,
        step,
        best_lag=None,
        statistic_name="r_squared",
        statistic_value=fit.r_squared,
        p_value=max(p_values.values()),
        details={
            "lags": {key: searched[key].lag for key in drivers},
            "coefficients": coefficients,
            "p_values": p_values,
        },
    )


def _run_block_method(
    block: pd.DataFrame,
    prepared: Mapping[str, pd.Series],
    spec: RelationSpec,
    step: int,
    method: Any,
    config: EngineConfig,
    common: dict[str, Any],
) -> WindowResult:
    """Run a resampling method on the common block (it repeats the lag search itself)."""
    first, last = int(block.index.min()), int(block.index.max())
    y_values = block["y"].to_numpy()
    x_ext = {
        key: prepared[key]
        .reindex(range(first - spec.lag_max, last - spec.lag_min + 1))
        .to_numpy(dtype="float64")
        for key in spec.drivers
    }
    if _constant(y_values) or any(_constant(values) for values in x_ext.values()):
        return WindowResult(
            result=RESULT_INSUFFICIENT,
            n_obs=len(block),
            details={"reason": "a constant series"},
            **common,
        )
    problem = BlockProblem(
        y=y_values,
        x_ext=x_ext,
        lag_min=spec.lag_min,
        lag_max=spec.lag_max,
        directions=dict(spec.driver_directions),
        transform=spec.transform,
        step=step,
    )
    try:
        outcome = method.test_block(problem, config.alpha)
    except SingularDesignError:
        return WindowResult(
            result=RESULT_INSUFFICIENT,
            n_obs=len(block),
            details={"reason": "singular regression design"},
            **common,
        )
    return WindowResult(
        result=RESULT_SUPPORTED if outcome.supported else RESULT_UNSUPPORTED,
        n_obs=len(block),
        best_lag=next(iter(outcome.lags.values())) if len(outcome.lags) == 1 else None,
        statistic_name=outcome.statistic_name,
        statistic_value=outcome.statistic_value,
        p_value=max(outcome.p_values.values()),
        start=date_of(first, step),
        end=date_of(last, step),
        reliable=outcome.reliable,
        reliability_reason=outcome.reliability_reason,
        details={
            "lags": outcome.lags,
            "p_values": outcome.p_values,
            "coefficients": outcome.coefficients,
            "settings": outcome.settings,
        },
        **common,
    )


def _window_result(
    common: dict[str, Any],
    supported: bool,
    block: pd.DataFrame,
    fit: FitResult,
    step: int,
    *,
    best_lag: int | None,
    statistic_name: str,
    statistic_value: float,
    p_value: float,
    details: dict[str, Any],
) -> WindowResult:
    details = {**details, "settings": fit.settings}
    return WindowResult(
        result=RESULT_SUPPORTED if supported else RESULT_UNSUPPORTED,
        n_obs=int(len(block)),
        best_lag=best_lag,
        statistic_name=statistic_name,
        statistic_value=float(statistic_value),
        p_value=float(p_value),
        start=date_of(int(block.index.min()), step),
        end=date_of(int(block.index.max()), step),
        reliable=fit.reliable,
        reliability_reason=fit.reliability_reason,
        details=details,
        **common,
    )


# --------------------------------------------------------------------------- #
# Status and the whole test
# --------------------------------------------------------------------------- #


def derive_status(windows: Mapping[str, WindowResult]) -> str:
    """Relation status from the two windows (DECISIONS §8; mixed = periods_differ)."""
    results = {window.result for window in windows.values()}
    if RESULT_INSUFFICIENT in results:
        return STATUS_INSUFFICIENT
    if results == {RESULT_SUPPORTED}:
        return STATUS_SUPPORTED
    if results == {RESULT_UNSUPPORTED}:
        return STATUS_UNSUPPORTED
    return STATUS_PERIODS_DIFFER


def run_relation_test(
    target: SeriesData,
    drivers: Sequence[SeriesData],
    spec: RelationSpec,
    *,
    deflator: SeriesData | None = None,
    method: SignificanceMethod | None = None,
    config: EngineConfig | None = None,
) -> TestReport | Unsupported:
    """Test one relation in both windows. Pure: no database, no LLM."""
    method = method or HacOls()
    config = config or EngineConfig()
    series = [target, *drivers]
    try:
        prepared, step = prepare(series, spec, deflator=deflator)
    except UnsupportedSeriesError as error:
        return Unsupported({error.key: error.reason})
    windows = {
        period: run_window(prepared, spec, step, period, method, config) for period in PERIODS
    }
    return TestReport(windows=windows, status=derive_status(windows), transform=spec.transform)
