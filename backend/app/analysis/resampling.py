"""Resampling candidates for the significance method (Task 3.2, protocol v1.1 section 6).

All three are *block methods*: they need the whole common block of a window (``BlockProblem``),
repeat the **whole lag search** on every resample, and return two-sided p-values
``(#{|T*| >= |T|} + 1) / (R + 1)`` plus the sign condition checked on the observed fit.

- :class:`CircularShift` (one driver): the driver is circularly shifted; shifts closer than
  ``guard`` periods to zero are excluded and drawn without replacement (at most ``shifts``).
- :class:`StationaryBootstrap` (one driver): the driver is resampled with stationary-bootstrap
  blocks (geometric lengths, expected length ``mean_block``); statistic = best |r| over the lags.
- :class:`WholeChainBootstrap` (any number of drivers): restricted-residual moving-block
  bootstrap. For every tested driver the model WITHOUT that driver is fitted once on the observed
  data (the other drivers at the lags the observed data chose), its centred residuals are
  resampled in fixed-length moving blocks, ``y* = restricted fit + e*``, and on every ``y*`` the
  **whole chain runs again** (every driver's best lag by |r|, one OLS with all lagged drivers,
  HAC t recomputed). Statistic = ``|t_j|`` (HAC), the same as ``HacOls``.

Every method is deterministic for a given ``seed`` and data (the generator is seeded from the
seed and a checksum of ``y``). Results stay flagged unreliable until ``calibrated=True``.
"""

from __future__ import annotations

import math
import zlib
from dataclasses import dataclass

import numpy as np

from app.analysis.errors import AnalysisError
from app.analysis.fasthac import ols_hac_t_batch
from app.analysis.significance import (
    UNCALIBRATED_REASON,
    BlockOutcome,
    BlockProblem,
    default_hac_lags,
)


def _rng(seed: int, problem: BlockProblem) -> np.random.Generator:
    checksum = zlib.crc32(np.ascontiguousarray(problem.y, dtype="float64").tobytes())
    return np.random.default_rng([seed, checksum])


def _standardise(values: np.ndarray, axis: int) -> np.ndarray:
    """Zero mean, unit (population) variance along ``axis``; a constant column becomes zeros."""
    centred = values - values.mean(axis=axis, keepdims=True)
    scale = values.std(axis=axis, keepdims=True)
    return centred / np.where(scale > 0, scale, np.inf)


def _lag_columns(problem: BlockProblem, x_ext: np.ndarray) -> np.ndarray:
    return np.stack([problem.lag_column(x_ext, lag) for lag in problem.lags], axis=1)  # n x L


def _sign_ok(value: float, direction: str) -> bool:
    return value > 0 if direction == "positive" else value < 0


def _flags(calibrated: bool) -> tuple[bool, str | None]:
    return calibrated, None if calibrated else UNCALIBRATED_REASON


def block_periods(months: int, step: int, n: int) -> int:
    """Block length in periods for a length given in months: at least 2, at most ``n``.

    The ``n / 3`` cap belongs to the Politis-White estimate only; the fixed candidates 12, 18 and
    24 months stay distinct methods (Codex review finding, 2026-10-07).
    """
    return int(max(2, min(max(months // step, 1), n)))


def politis_white_block(x: np.ndarray, step: int) -> int:
    """Politis-White (stationary) block length of ``x`` (``arch``), rounded up, clipped to
    [2, n/3]; falls back to 12 months when the estimate is not finite."""
    from arch.bootstrap import optimal_block_length

    n = len(x)
    try:
        value = float(optimal_block_length(np.asarray(x, dtype="float64")).iloc[0]["stationary"])
    except Exception:  # noqa: BLE001 - arch raises assorted errors on degenerate input
        value = float("nan")
    if not math.isfinite(value):
        return block_periods(12, step, max(n // 3, 2))
    return int(max(2, min(math.ceil(value), max(n // 3, 2))))


def stationary_indices(
    rng: np.random.Generator, length: int, replicates: int, mean_length: float
) -> np.ndarray:
    """Stationary-bootstrap index matrix ``replicates x length`` (circular)."""
    restart = rng.random((replicates, length)) < 1.0 / mean_length
    starts = rng.integers(0, length, size=(replicates, length))
    indices = np.empty((replicates, length), dtype=np.int64)
    indices[:, 0] = starts[:, 0]
    for position in range(1, length):
        following = (indices[:, position - 1] + 1) % length
        indices[:, position] = np.where(restart[:, position], starts[:, position], following)
    return indices


def moving_block_indices(
    rng: np.random.Generator, length: int, replicates: int, block: int
) -> np.ndarray:
    """Moving-block bootstrap index matrix ``replicates x length`` (blocks of fixed length)."""
    block = max(1, min(block, length))
    count = math.ceil(length / block)
    starts = rng.integers(0, length - block + 1, size=(replicates, count))
    indices = (starts[:, :, None] + np.arange(block)).reshape(replicates, -1)
    return indices[:, :length]


# --------------------------------------------------------------------------- #
# Pairwise statistic
# --------------------------------------------------------------------------- #


def _best_abs_r(problem: BlockProblem, y_std: np.ndarray, x_ext: np.ndarray) -> tuple[int, float]:
    """(lag, signed r) of the strongest |r| over the lag range."""
    columns = _standardise(_lag_columns(problem, x_ext), axis=0)
    r = columns.T @ y_std / problem.n
    position = int(np.argmax(np.abs(r)))
    return problem.lag_min + position, float(r[position])


def _single_driver(problem: BlockProblem, name: str) -> tuple[str, np.ndarray]:
    if len(problem.x_ext) != 1:
        raise AnalysisError(f"{name} tests one driver at a time (use the whole-chain bootstrap)")
    key = next(iter(problem.x_ext))
    return key, problem.x_ext[key]


@dataclass(frozen=True)
class CircularShift:
    """Circular-shift surrogate test of one driver (statistic: strongest |r| over the lags)."""

    shifts: int = 199
    seed: int = 0
    calibrated: bool = False
    name: str = "circular_shift"

    def test_block(self, problem: BlockProblem, alpha: float) -> BlockOutcome:
        key, x_ext = _single_driver(problem, self.name)
        y_std = _standardise(problem.y, axis=0)
        lag, r = _best_abs_r(problem, y_std, x_ext)
        guard = max(12 // problem.step, 1)
        offsets = np.arange(guard, len(x_ext) - guard)
        rng = _rng(self.seed, problem)
        if len(offsets) == 0:
            p_value, used = 1.0, 0
        else:
            chosen = rng.choice(offsets, size=min(self.shifts, len(offsets)), replace=False)
            exceed = sum(
                abs(_best_abs_r(problem, y_std, np.roll(x_ext, int(shift)))[1]) >= abs(r)
                for shift in chosen
            )
            used = len(chosen)
            p_value = (exceed + 1) / (used + 1)
        reliable, reason = _flags(self.calibrated)
        return BlockOutcome(
            supported=_sign_ok(r, problem.directions[key]) and p_value < alpha,
            lags={key: lag},
            p_values={key: p_value},
            coefficients={key: r},
            statistic_name="abs_r",
            statistic_value=abs(r),
            reliable=reliable,
            reliability_reason=reason,
            settings={"method": self.name, "shifts_used": used, "guard": guard},
        )


@dataclass(frozen=True)
class StationaryBootstrap:
    """Stationary-bootstrap test of one driver (statistic: strongest |r| over the lags).

    ``mean_block`` is the expected block length in months (12, 18, 24...) or ``"pw"`` for the
    Politis-White estimate of the driver series.
    """

    mean_block: int | str = 12
    replicates: int = 499
    seed: int = 0
    calibrated: bool = False
    name: str = "stationary_bootstrap"

    def test_block(self, problem: BlockProblem, alpha: float) -> BlockOutcome:
        key, x_ext = _single_driver(problem, self.name)
        n_ext = len(x_ext)
        y_std = _standardise(problem.y, axis=0)
        lag, r = _best_abs_r(problem, y_std, x_ext)
        if self.mean_block == "pw":
            mean_length = politis_white_block(x_ext, problem.step)
        else:
            mean_length = block_periods(int(self.mean_block), problem.step, n_ext)
        rng = _rng(self.seed, problem)
        resampled = x_ext[stationary_indices(rng, n_ext, self.replicates, mean_length)]
        strongest = np.zeros(self.replicates)
        for current in problem.lags:
            start = problem.lag_max - current
            columns = _standardise(resampled[:, start : start + problem.n], axis=1)
            strongest = np.maximum(strongest, np.abs(columns @ y_std) / problem.n)
        p_value = (int(np.sum(strongest >= abs(r))) + 1) / (self.replicates + 1)
        reliable, reason = _flags(self.calibrated)
        return BlockOutcome(
            supported=_sign_ok(r, problem.directions[key]) and p_value < alpha,
            lags={key: lag},
            p_values={key: p_value},
            coefficients={key: r},
            statistic_name="abs_r",
            statistic_value=abs(r),
            reliable=reliable,
            reliability_reason=reason,
            settings={
                "method": self.name,
                "mean_block_periods": mean_length,
                "replicates": self.replicates,
            },
        )


# --------------------------------------------------------------------------- #
# Whole-chain restricted-residual block bootstrap
# --------------------------------------------------------------------------- #


def _chain(
    problem: BlockProblem,
    y: np.ndarray,
    columns: dict[str, np.ndarray],
    standard: dict[str, np.ndarray],
    maxlags: int,
) -> tuple[dict[str, np.ndarray], np.ndarray, np.ndarray]:
    """The whole chain for many targets at once (``y`` is ``B x n``).

    Every driver's lag by strongest |r| (own pairwise search), then one OLS of the target on all
    lagged drivers with HAC standard errors. Returns the chosen lag positions per driver
    (``B``), the slopes and the HAC t values (``B x k``).
    """
    y_std = _standardise(y, axis=1)
    chosen: dict[str, np.ndarray] = {}
    lagged = []
    for key, table in columns.items():
        r = y_std @ standard[key] / problem.n  # B x L
        position = np.argmax(np.abs(r), axis=1)
        chosen[key] = position
        lagged.append(table[:, position].T)  # B x n
    design = np.stack(lagged, axis=2)  # B x n x k
    beta, t_values = ols_hac_t_batch(y, design, maxlags)
    return chosen, beta[:, 1:], t_values[:, 1:]


@dataclass(frozen=True)
class WholeChainBootstrap:
    """Restricted-residual moving-block bootstrap with the whole chain repeated (see module doc).

    ``block`` is the moving-block length in months; ``maxlags`` forces the HAC bandwidth (default:
    the shared rule of ``HacOls``).
    """

    block: int = 12
    replicates: int = 499
    seed: int = 0
    maxlags: int | None = None
    calibrated: bool = False
    name: str = "whole_chain_bootstrap"

    def test_block(self, problem: BlockProblem, alpha: float) -> BlockOutcome:
        keys = list(problem.x_ext)
        n = problem.n
        columns = {key: _lag_columns(problem, problem.x_ext[key]) for key in keys}
        standard = {key: _standardise(columns[key], axis=0) for key in keys}
        maxlags = (
            self.maxlags
            if self.maxlags is not None
            else default_hac_lags(n, problem.transform, problem.step)
        )
        y = np.asarray(problem.y, dtype="float64")
        chosen, beta, t_values = _chain(problem, y[None, :], columns, standard, maxlags)
        observed_lag = {key: int(chosen[key][0]) for key in keys}
        observed_t = t_values[0]
        observed_beta = beta[0]

        rng = _rng(self.seed, problem)
        block = block_periods(self.block, problem.step, n)
        p_values: dict[str, float] = {}
        for index, key in enumerate(keys):
            others = [other for other in keys if other != key]
            design = np.column_stack(
                [np.ones(n)] + [columns[other][:, observed_lag[other]] for other in others]
            )
            coefficients, *_ = np.linalg.lstsq(design, y, rcond=None)
            fitted = design @ coefficients
            residuals = y - fitted
            residuals = residuals - residuals.mean()
            picks = moving_block_indices(rng, n, self.replicates, block)
            pseudo = fitted[None, :] + residuals[picks]
            _, _, t_star = _chain(problem, pseudo, columns, standard, maxlags)
            exceed = int(np.sum(np.abs(t_star[:, index]) >= abs(observed_t[index])))
            p_values[key] = (exceed + 1) / (self.replicates + 1)

        supported = all(
            _sign_ok(float(observed_beta[i]), problem.directions[key]) and p_values[key] < alpha
            for i, key in enumerate(keys)
        )
        reliable, reason = _flags(self.calibrated)
        return BlockOutcome(
            supported=supported,
            lags={key: problem.lag_min + observed_lag[key] for key in keys},
            p_values=p_values,
            coefficients={key: float(observed_beta[i]) for i, key in enumerate(keys)},
            statistic_name="min_abs_hac_t",
            statistic_value=float(np.min(np.abs(observed_t))),
            reliable=reliable,
            reliability_reason=reason,
            settings={
                "method": self.name,
                "replicates": self.replicates,
                "block_periods": block,
                "hac_maxlags": maxlags,
            },
        )
