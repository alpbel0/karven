"""Synthetic series with known relations for the relation test engine tests."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from app.analysis.models import SeriesData
from app.analysis.slots import slot_of

START_2005 = slot_of(date(2005, 1, 1), 1)  # first monthly slot used by the helpers


def series_from_levels(
    key: str,
    levels: np.ndarray,
    *,
    step: int = 1,
    start_slot: int | None = None,
    aggregation: str = "ortalama",
    cumulative: bool = False,
) -> SeriesData:
    first = START_2005 if start_slot is None else start_slot
    index = range(first, first + len(levels))
    return SeriesData(
        key=key,
        frequency={1: "monthly", 3: "quarterly", 6: "semiannual", 12: "annual"}[step],
        step=step,
        values=pd.Series(np.asarray(levels, dtype="float64"), index=list(index)),
        aggregation=aggregation,
        measure_type="endeks",
        cumulative=cumulative,
    )


def random_walk(
    rng: np.random.Generator, increments: np.ndarray, start: float = 100.0
) -> np.ndarray:
    return start + np.cumsum(increments)


def linked_pair(
    n: int = 252,
    *,
    lag: int = 1,
    beta: float = 0.8,
    noise: float = 0.5,
    seed: int = 1,
) -> tuple[SeriesData, SeriesData]:
    """Target whose first difference follows the driver's first difference ``lag`` months ago."""
    rng = np.random.default_rng(seed)
    driver_inc = rng.normal(size=n)
    target_inc = np.zeros(n)
    target_inc[lag:] = beta * driver_inc[:-lag] if lag else beta * driver_inc
    if lag == 0:
        target_inc = beta * driver_inc
    target_inc = target_inc + rng.normal(scale=noise, size=n)
    driver = series_from_levels("tcmb|DRV", random_walk(rng, driver_inc))
    target = series_from_levels("tcmb|TGT", random_walk(rng, target_inc))
    return target, driver


def independent_pair(n: int = 252, seed: int = 7) -> tuple[SeriesData, SeriesData]:
    rng = np.random.default_rng(seed)
    driver = series_from_levels("tcmb|DRV", random_walk(rng, rng.normal(size=n)))
    target = series_from_levels("tcmb|TGT", random_walk(rng, rng.normal(size=n)))
    return target, driver
