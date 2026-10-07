"""Pluggable significance methods (DECISIONS §8, Task 3.2 decision 2026-10-05).

The p-value method is deliberately NOT fixed: monthly series turned into annual percent
changes overlap by 11 months, so the naive OLS p-value is far too optimistic. Candidates
are compared in the calibration study (``app.analysis.calibration``); the engine only needs
this interface. ``HacOls`` is the first candidate: OLS with Newey-West (Bartlett) standard
errors and a t reference. Until a method is calibrated, every result it gives is flagged
unreliable (user decision: a result with an explicit warning, never a silent one).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np
import statsmodels.api as sm

UNCALIBRATED_REASON = "significance method not calibrated yet (Task 3.2 calibration study pending)"


@dataclass(frozen=True)
class FitContext:
    """What a method may need to know about the data it is fitting."""

    transform: str
    step: int  # period length in months
    lags_searched: int = 1


@dataclass(frozen=True)
class FitResult:
    """Coefficients (intercept first), p-values and reliability of one fit."""

    coefficients: tuple[float, ...]
    p_values: tuple[float, ...]
    r_squared: float
    n_obs: int
    reliable: bool
    reliability_reason: str | None
    settings: dict[str, Any]


class SignificanceMethod(Protocol):
    name: str

    def fit(self, y: np.ndarray, x: np.ndarray, context: FitContext) -> FitResult: ...


def bartlett_rule_lags(n_obs: int) -> int:
    """Newey-West rule of thumb: floor(4 * (n / 100) ** (2 / 9))."""
    return int(math.floor(4.0 * (n_obs / 100.0) ** (2.0 / 9.0)))


def default_hac_lags(n_obs: int, transform: str, step: int) -> int:
    """HAC bandwidth when none is forced: the overlap horizon of an annual change on sub-annual
    data (``12 / step - 1`` periods), otherwise the rule of thumb. Shared by ``HacOls`` and the
    resampling methods so that the statistic is the same everywhere."""
    if transform == "annual_pct_change" and step < 12:
        lags = 12 // step - 1
    else:
        lags = bartlett_rule_lags(n_obs)
    return max(0, min(lags, n_obs - 2))


@dataclass(frozen=True)
class HacOls:
    """OLS + HAC (Newey-West, Bartlett kernel), small-sample correction and t reference on.

    ``maxlags``: ``None`` = the overlap horizon of the transform (an annual change on
    sub-annual data overlaps ``12/step - 1`` periods) or the rule of thumb otherwise; an
    int forces that many lags (calibration grid). ``lag_correction`` multiplies every
    p-value by the number of lags searched (Bonferroni, capped at 1).
    """

    maxlags: int | str | None = None  # int, "rule" (rule of thumb only) or None (default)
    lag_correction: bool = False
    use_t: bool = True
    calibrated: bool = False
    name: str = "hac_ols"

    def _lags(self, n_obs: int, context: FitContext) -> int:
        if self.maxlags == "rule":
            return max(0, min(bartlett_rule_lags(n_obs), n_obs - 2))
        if self.maxlags is not None:
            return max(0, min(int(self.maxlags), n_obs - 2))
        return default_hac_lags(n_obs, context.transform, context.step)

    def fit(self, y: np.ndarray, x: np.ndarray, context: FitContext) -> FitResult:
        n_obs = int(len(y))
        regressors = np.asarray(x, dtype="float64").reshape(n_obs, -1)
        design = sm.add_constant(regressors, has_constant="add")
        lags = self._lags(n_obs, context)
        model = sm.OLS(np.asarray(y, dtype="float64"), design).fit(
            cov_type="HAC",
            cov_kwds={"maxlags": lags, "use_correction": True},
            use_t=self.use_t,
        )
        p_values = np.asarray(model.pvalues, dtype="float64")
        if self.lag_correction and context.lags_searched > 1:
            p_values = np.minimum(1.0, p_values * context.lags_searched)
        return FitResult(
            coefficients=tuple(float(value) for value in model.params),
            p_values=tuple(float(value) for value in p_values),
            r_squared=float(model.rsquared),
            n_obs=n_obs,
            reliable=self.calibrated,
            reliability_reason=None if self.calibrated else UNCALIBRATED_REASON,
            settings={
                "method": self.name,
                "kernel": "bartlett",
                "maxlags": lags,
                "small_sample_correction": True,
                "reference": "t" if self.use_t else "normal",
                "lag_correction": self.lag_correction,
                "lags_searched": context.lags_searched,
            },
        )


# --------------------------------------------------------------------------- #
# Block methods: resampling tests that need the whole common block
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BlockProblem:
    """The common block of one window, in the form the resampling methods need.

    ``y`` has ``n`` values. ``x_ext[key]`` is the driver on the extended index
    ``[first - lag_max, last - lag_min]`` (``n + lag_max - lag_min`` values), so a lag column is a
    slice (:meth:`lag_column`) and a resampled driver can be re-lagged without touching the
    engine. ``directions`` maps every driver to its expected sign.
    """

    y: np.ndarray
    x_ext: dict[str, np.ndarray]
    lag_min: int
    lag_max: int
    directions: dict[str, str]
    transform: str
    step: int

    @property
    def n(self) -> int:
        return int(len(self.y))

    @property
    def lags(self) -> range:
        return range(self.lag_min, self.lag_max + 1)

    def lag_column(self, x_ext: np.ndarray, lag: int) -> np.ndarray:
        start = self.lag_max - lag
        return x_ext[start : start + self.n]


@dataclass(frozen=True)
class BlockOutcome:
    """What a block method concluded for one window."""

    supported: bool
    lags: dict[str, int]
    p_values: dict[str, float]
    coefficients: dict[str, float]
    statistic_name: str
    statistic_value: float
    reliable: bool
    reliability_reason: str | None
    settings: dict[str, Any]


class BlockMethod(Protocol):
    """A method that tests a whole window (resampling needs more than ``y`` and ``x``)."""

    name: str

    def test_block(self, problem: BlockProblem, alpha: float) -> BlockOutcome: ...
