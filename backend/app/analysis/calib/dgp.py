"""Simulators of the calibration study (protocol v1.1 sections 4 and 5).

Two layers:

* ``stat``: stationary processes whose coefficients are known; fed to the engine as series that
  are already transformed (aggregation ``yeniden_hesapla`` = used as they are), so the test is
  exercised without a transform.
* ``e2e``: economic levels (``100 * exp(cumsum(growth * 0.01))``) through the real chain:
  reel hale getirme -> frekans -> dönüşüm -> blok -> test -> iki pencere.

The series always end in the month of the real data (2026-09) and go backwards, so the 2017-2026
window is nested in the full one. ``Cell.true_support`` is the truth the methods are scored
against: the relation may be supported only if EVERY driver has a positive true effect.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from app.analysis.models import RelationSpec, SeriesData
from app.analysis.slots import slot_of

END_SLOT = slot_of(date(2026, 9, 1), 1)
LAG_MIN, LAG_MAX = 0, 3
BURN_IN = 200

TUNING_FAMILIES = (
    "iid",
    "ar05",
    "ar09",
    "seasonal_common",
    "seasonal_indep",
    "var_break",
    "mean_break",
    "ma11",
    "heavy",
    "seasonal_weak",
    "sar12_05",
    "sar12_08",
)
#: Families of the protocol v1.4 usage region R (acceptance cells).
REGION_FAMILIES = ("iid", "ar05", "ma11", "sar12_05", "seasonal_weak")
#: Families the reliability gate MUST reject (persistent seasonality; leak criterion). The last
#: two are unseen at tuning (validation-only controls near the gate boundary).
GATED_FAMILIES = ("seasonal_indep", "seasonal_common", "sar12_08", "seasonal_mid", "sar12_065")
HELD_OUT_FAMILIES = ("arma", "ar_neg", "hetero", "semisynth")
TARGET_KEY = "T"
CPI_KEY = "tuik|CPI"


@dataclass(frozen=True)
class Structure:
    """Which drivers have a real effect on the target."""

    name: str
    betas: tuple[float, ...]  # true effect per driver (expected direction: positive)
    lags: tuple[int, ...]
    rho: float = 0.0  # correlation between the drivers' innovations
    spread: bool = False  # the last driver's effect is split over lag and lag + 1

    @property
    def k(self) -> int:
        return len(self.betas)

    @property
    def true_support(self) -> bool:
        return all(beta > 0 for beta in self.betas)


STRUCTURES: dict[str, Structure] = {
    s.name: s
    for s in (
        Structure("k1_null", (0.0,), (1,)),
        Structure("k1_reversed", (-0.4,), (1,)),
        Structure("k1_real02", (0.2,), (1,)),
        Structure("k1_real04", (0.4,), (1,)),
        Structure("k1_real07", (0.7,), (1,)),
        Structure("k2_null_null", (0.0, 0.0), (1, 2)),
        Structure("k2_real_null", (0.4, 0.0), (1, 2)),
        Structure("k2_real_null_r5", (0.4, 0.0), (1, 2), rho=0.5),
        Structure("k2_real_null_r8", (0.4, 0.0), (1, 2), rho=0.8),
        Structure("k2_real_reversed", (0.4, -0.4), (1, 2)),
        Structure("k2_real_real04", (0.4, 0.4), (1, 2)),
        Structure("k2_real_real07", (0.7, 0.7), (1, 2)),
        Structure("k2_real_spread", (0.4, 0.4), (1, 1), spread=True),
        Structure("k2_real_nearzero", (0.4, 0.1), (1, 2)),
        Structure("k3_real_null_null", (0.4, 0.0, 0.0), (1, 2, 0)),
        Structure("k3_real_real_null", (0.4, 0.4, 0.0), (1, 2, 0)),
        Structure("k3_real_real_real", (0.7, 0.7, 0.7), (1, 2, 0)),
    )
}


@dataclass(frozen=True)
class Cell:
    """One scenario cell of the study."""

    layer: str  # stat | e2e
    family: str
    structure: str
    n: int  # observations after transform, alignment and lag (the common block length)
    transform: str = "difference"  # e2e only: annual_pct_change | period_pct_change | difference
    gap: bool = False  # e2e: a contiguous 10 % of the target months is missing
    nominal: bool = False  # e2e: nominal TL series made real with a common CPI

    @property
    def id(self) -> str:
        extra = "/".join(
            part
            for part in (
                self.transform if self.layer == "e2e" else "",
                "gap" if self.gap else "",
                "nominal" if self.nominal else "",
            )
            if part
        )
        base = f"{self.layer}/{self.family}/{self.structure}/n{self.n}"
        return f"{base}/{extra}" if extra else base

    @property
    def spec_structure(self) -> Structure:
        return STRUCTURES[self.structure]


# --------------------------------------------------------------------------- #
# Unit-variance noise processes
# --------------------------------------------------------------------------- #

_POOL: np.ndarray | None = None


def residual_pool() -> np.ndarray:
    """Standardised residuals of real monthly series (semi-synthetic family)."""
    global _POOL
    if _POOL is None:
        path = Path(__file__).resolve().parents[4] / "docs" / "calibration" / "residual-pool.json"
        if not path.exists():
            raise FileNotFoundError(f"{path} is missing (build it with calib.dgp.build_pool)")
        _POOL = np.asarray(json.loads(path.read_text(encoding="utf-8"))["values"], dtype="float64")
    return _POOL


def _ar1(rng: np.random.Generator, length: int, phi: float) -> np.ndarray:
    shocks = rng.normal(size=length + BURN_IN) * math.sqrt(1.0 - phi**2)
    out = np.empty(length + BURN_IN)
    out[0] = shocks[0] / math.sqrt(1.0 - phi**2)
    for i in range(1, length + BURN_IN):
        out[i] = phi * out[i - 1] + shocks[i]
    return out[BURN_IN:]


def _sar12(rng: np.random.Generator, length: int, phi: float) -> np.ndarray:
    """Stochastic seasonality: x_t = phi * x_{t-12} + e_t, unit variance, burn-in discarded."""
    shocks = rng.normal(size=length + BURN_IN) * math.sqrt(1.0 - phi**2)
    out = np.empty(length + BURN_IN)
    out[:12] = shocks[:12] / math.sqrt(1.0 - phi**2)
    for i in range(12, length + BURN_IN):
        out[i] = phi * out[i - 12] + shocks[i]
    return out[BURN_IN:]


def _arma11(rng: np.random.Generator, length: int, phi: float, theta: float) -> np.ndarray:
    shocks = rng.normal(size=length + BURN_IN + 1)
    out = np.zeros(length + BURN_IN)
    for i in range(1, length + BURN_IN):
        out[i] = phi * out[i - 1] + shocks[i] + theta * shocks[i - 1]
    variance = (1.0 + 2.0 * phi * theta + theta**2) / (1.0 - phi**2)
    return out[BURN_IN:] / math.sqrt(variance)


def noise(
    rng: np.random.Generator, length: int, family: str, keep: int | None = None
) -> np.ndarray:
    """One unit-variance series of the given family (a driver's or the target's own noise).

    ``keep`` is the length of the tail that survives the burn-in; breaks are placed inside that
    tail (the variance break exactly in its middle, the mean break uniformly in its middle half),
    never inside the discarded burn-in (Codex review finding, 2026-10-07).
    """
    keep = length if keep is None else keep
    tail = length - keep  # index where the surviving part starts
    if family == "iid":
        return rng.normal(size=length)
    if family == "ar05":
        return _ar1(rng, length, 0.5)
    if family == "ar09":
        return _ar1(rng, length, 0.9)
    if family == "ar_neg":
        return _ar1(rng, length, -0.5)
    if family == "arma":
        return _arma11(rng, length, 0.6, -0.5)
    if family == "seasonal_common":
        month = np.arange(length) % 12
        return (
            0.6 * rng.normal(size=length) + 0.8 * np.sin(2 * math.pi * month / 12.0)
        ) / math.sqrt(0.68)
    if family == "seasonal_indep":
        month = np.arange(length) % 12
        phase = rng.uniform(0.0, 2 * math.pi)
        return (
            0.6 * rng.normal(size=length) + 0.8 * np.sin(2 * math.pi * month / 12.0 + phase)
        ) / math.sqrt(0.68)
    if family == "seasonal_weak":
        month = np.arange(length) % 12
        phase = rng.uniform(0.0, 2 * math.pi)
        return (
            0.9 * rng.normal(size=length) + 0.4 * np.sin(2 * math.pi * month / 12.0 + phase)
        ) / math.sqrt(0.89)
    if family == "seasonal_mid":
        month = np.arange(length) % 12
        phase = rng.uniform(0.0, 2 * math.pi)
        return (
            0.7 * rng.normal(size=length) + 0.6 * np.sin(2 * math.pi * month / 12.0 + phase)
        ) / math.sqrt(0.67)
    if family in ("sar12_05", "sar12_08", "sar12_065"):
        phi = {"sar12_05": 0.5, "sar12_08": 0.8, "sar12_065": 0.65}[family]
        return _sar12(rng, length, phi)
    if family == "var_break":
        scale = np.where(np.arange(length) < tail + keep // 2, 1.0, 2.0)
        return rng.normal(size=length) * scale
    if family == "mean_break":
        point = tail + int(rng.integers(keep // 4, 3 * keep // 4 + 1))
        return rng.normal(size=length) + np.where(np.arange(length) >= point, 1.0, 0.0)
    if family == "heavy":
        return rng.standard_t(3, size=length) / math.sqrt(3.0)
    if family == "ma11":
        raw = rng.normal(size=length + 11)
        return np.convolve(raw, np.ones(12), mode="valid") / math.sqrt(12.0)
    if family == "hetero":
        return rng.normal(size=length)  # the heteroskedasticity is applied to the target below
    if family == "semisynth":
        pool = residual_pool()
        return rng.choice(pool, size=length, replace=True)
    raise ValueError(f"unknown family {family!r}")


def _driver_noises(
    rng: np.random.Generator, length: int, family: str, k: int, rho: float, keep: int
) -> list[np.ndarray]:
    own = [noise(rng, length, family, keep) for _ in range(k)]
    if rho <= 0.0 or k < 2:
        return own
    common = noise(rng, length, family, keep)
    return [math.sqrt(rho) * common + math.sqrt(1.0 - rho) * item for item in own]


def build_pool(series_growth: list[np.ndarray], out: Path) -> int:
    """Pool of standardised AR(1)-residuals of real series (run once, stored in docs/)."""
    values: list[float] = []
    for growth in series_growth:
        x = np.asarray(growth, dtype="float64")
        x = (x - x.mean()) / x.std()
        phi = float(x[1:] @ x[:-1] / (x[:-1] @ x[:-1]))
        residual = x[1:] - phi * x[:-1]
        residual = (residual - residual.mean()) / residual.std()
        values.extend(float(v) for v in residual)
    out.write_text(json.dumps({"values": values}), encoding="utf-8")
    return len(values)


# --------------------------------------------------------------------------- #
# Series construction
# --------------------------------------------------------------------------- #


def _processes(
    rng: np.random.Generator, length: int, cell: Cell
) -> tuple[np.ndarray, list[np.ndarray]]:
    """Target growth/values and driver series of the length ``length`` (burn-in included)."""
    structure = cell.spec_structure
    total = length + BURN_IN
    drivers = _driver_noises(rng, total, cell.family, structure.k, structure.rho, length)
    target = noise(rng, total, cell.family, length)
    if cell.family == "hetero":
        lagged = np.roll(drivers[0], structure.lags[0])
        target = target * np.exp(0.3 * lagged) / math.exp(0.09)
    for index, (beta, lag) in enumerate(zip(structure.betas, structure.lags, strict=True)):
        if beta == 0.0:
            continue
        pieces = [(beta, lag)]
        if structure.spread and index == structure.k - 1:
            pieces = [(beta / 2.0, lag), (beta / 2.0, lag + 1)]
        for weight, shift in pieces:
            target = target + weight * np.roll(drivers[index], shift)
    return target[BURN_IN:], [item[BURN_IN:] for item in drivers]


def _series(key: str, values: np.ndarray, aggregation: str) -> SeriesData:
    first = END_SLOT - len(values) + 1
    return SeriesData(
        key=key,
        frequency="monthly",
        step=1,
        values=pd.Series(np.asarray(values, dtype="float64"), index=range(first, END_SLOT + 1)),
        aggregation=aggregation,
        measure_type="endeks",
    )


def make_replicate(
    cell: Cell, rng: np.random.Generator
) -> tuple[list[SeriesData], RelationSpec, SeriesData | None]:
    """The series (target first), the locked hypothesis and the CPI deflator (or ``None``)."""
    structure = cell.spec_structure
    keys = [f"D{i}" for i in range(structure.k)]
    directions = {key: "positive" for key in keys}
    if cell.layer == "stat":
        length = cell.n + LAG_MAX
        target, drivers = _processes(rng, length, cell)
        series = [_series(TARGET_KEY, target, "yeniden_hesapla")]
        series += [
            _series(key, values, "yeniden_hesapla")
            for key, values in zip(keys, drivers, strict=True)
        ]
        spec = RelationSpec(TARGET_KEY, directions, LAG_MIN, LAG_MAX, "difference")
        return series, spec, None

    loss = 12 if cell.transform == "annual_pct_change" else 1
    length = cell.n + loss + LAG_MAX
    target, drivers = _processes(rng, length, cell)
    levels = [100.0 * np.exp(np.cumsum(item * 0.01)) for item in [target, *drivers]]
    deflator = None
    nominal: tuple[str, ...] = ()
    if cell.nominal:
        cpi = 100.0 * np.exp(np.cumsum(0.02 + 0.01 * rng.normal(size=length)))
        deflator = _series(CPI_KEY, cpi, "ortalama")
        levels = [level * cpi for level in levels]
        nominal = (TARGET_KEY, *keys)
    series = [_series(TARGET_KEY, levels[0], "ortalama")]
    series += [_series(key, level, "ortalama") for key, level in zip(keys, levels[1:], strict=True)]
    if cell.gap:
        target_series = series[0]
        hole = max(int(0.1 * len(target_series.values)), 1)
        start = len(target_series.values) // 2
        drop = target_series.values.index[start : start + hole]
        series[0] = SeriesData(
            key=target_series.key,
            frequency=target_series.frequency,
            step=target_series.step,
            values=target_series.values.drop(index=drop),
            aggregation=target_series.aggregation,
            measure_type=target_series.measure_type,
        )
    spec = RelationSpec(TARGET_KEY, directions, LAG_MIN, LAG_MAX, cell.transform, nominal)
    return series, spec, deflator
