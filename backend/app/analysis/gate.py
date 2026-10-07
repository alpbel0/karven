"""Reliability gate (Task 3.2, protocol v1.1 section 7).

A window result is *reliable* only if the data of that window are in the region where the
significance method was validated. The gate looks at data features that can be computed from the
window itself: sample size, first and seasonal-lag autocorrelation, a variance break (second half
against first half) and a mean break. ``window_features`` computes them for every window (and the
engine stores them in the result details), ``assess`` applies the thresholds. The thresholds are
chosen in the tuning phase and frozen in ``FREEZE.md`` before validation; until then the gate is
off and every result carries the "not calibrated" warning instead.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class GateThresholds:
    """Upper limits (and the lower sample limit) a window must respect to be called reliable."""

    n_min: int
    rho1_max: float
    rho_seasonal_max: float
    var_ratio_max: float
    mean_shift_max: float
    #: Persistent seasonality = lag-12 AND lag-24 autocorrelation both high: a deterministic cycle
    #: keeps its autocorrelation at lag 24, an annual change (MA(11)) loses it. ``-1`` switches the
    #: lag-24 condition off (a window is flagged by the lag-12 limit alone).
    rho_seasonal24_max: float = -1.0


def autocorrelation(values: np.ndarray, lag: int) -> float:
    """Lag ``lag`` autocorrelation (0.0 for a constant or too short series)."""
    n = len(values)
    if lag < 1 or n <= lag + 1:
        return 0.0
    centred = values - values.mean()
    denominator = float(centred @ centred)
    if denominator == 0.0:
        return 0.0
    return float(centred[lag:] @ centred[:-lag]) / denominator


def _break_features(values: np.ndarray) -> tuple[float, float]:
    """(variance ratio >= 1 of the two halves, mean shift in pooled standard deviations)."""
    half = len(values) // 2
    first, second = values[:half], values[half:]
    if half < 3 or len(second) < 3:
        return 1.0, 0.0
    v1, v2 = float(first.var()), float(second.var())
    if v1 == 0.0 or v2 == 0.0:
        ratio = 1.0 if v1 == v2 else float("inf")
    else:
        ratio = max(v1 / v2, v2 / v1)
    pooled = float(values.std())
    shift = abs(float(second.mean() - first.mean())) / pooled if pooled > 0 else 0.0
    return ratio, shift


def window_features(
    y: np.ndarray, drivers: Sequence[np.ndarray], step: int
) -> dict[str, float | int]:
    """Gate features of one window: the worst case over the target and every driver series."""
    seasonal_lag = max(12 // step, 1)
    series = [np.asarray(y, dtype="float64"), *(np.asarray(d, dtype="float64") for d in drivers)]
    breaks = [_break_features(values) for values in series]
    return {
        "n": int(len(y)),
        "rho1": max(abs(autocorrelation(values, 1)) for values in series),
        "rho_seasonal": max(abs(autocorrelation(values, seasonal_lag)) for values in series),
        "rho_seasonal24": max(abs(autocorrelation(values, 2 * seasonal_lag)) for values in series),
        "var_ratio": max(ratio for ratio, _ in breaks),
        "mean_shift": max(shift for _, shift in breaks),
    }


def assess(features: dict[str, float | int], limits: GateThresholds) -> tuple[bool, str | None]:
    """Apply the thresholds; returns ``(reliable, reason)``."""
    problems = []
    if features["n"] < limits.n_min:
        problems.append(f"sample {features['n']} < {limits.n_min}")
    if features["rho1"] > limits.rho1_max:
        problems.append(f"autocorrelation {features['rho1']:.2f} > {limits.rho1_max}")
    if features["rho_seasonal"] > limits.rho_seasonal_max and (
        features.get("rho_seasonal24", 1.0) > limits.rho_seasonal24_max
    ):
        problems.append(
            f"persistent seasonality: autocorrelation {features['rho_seasonal']:.2f} at lag 12 "
            f"> {limits.rho_seasonal_max} and {features.get('rho_seasonal24', float('nan')):.2f} "
            f"at lag 24 > {limits.rho_seasonal24_max}"
        )
    if features["var_ratio"] > limits.var_ratio_max:
        problems.append(f"variance break {features['var_ratio']:.1f} > {limits.var_ratio_max}")
    if features["mean_shift"] > limits.mean_shift_max:
        problems.append(f"mean break {features['mean_shift']:.2f} > {limits.mean_shift_max}")
    if problems:
        return False, "outside the validated region: " + "; ".join(problems)
    return True, None
