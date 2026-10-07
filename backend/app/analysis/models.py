"""Data types of the relation test engine."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pandas as pd

from app.analysis.errors import InvalidSpecError
from app.analysis.transforms import TRANSFORMS

PERIOD_ALL = "all_years"
PERIOD_RECENT = "2017_2026"
PERIODS = (PERIOD_ALL, PERIOD_RECENT)

RESULT_SUPPORTED = "supported"
RESULT_UNSUPPORTED = "unsupported"
RESULT_INSUFFICIENT = "insufficient_data"

STATUS_SUPPORTED = "supported"
STATUS_UNSUPPORTED = "unsupported"
STATUS_PERIODS_DIFFER = "periods_differ"
STATUS_INSUFFICIENT = "insufficient_data"


@dataclass(frozen=True)
class SeriesData:
    """One series ready for testing: native frequency, catalog measure, values by slot."""

    key: str
    frequency: str
    step: int
    values: pd.Series  # index = int slots at ``step``, float values
    aggregation: str | None = None
    measure_type: str | None = None
    cumulative: bool = False


@dataclass(frozen=True)
class RelationSpec:
    """The locked hypothesis the engine tests (no data was seen to build it)."""

    target: str
    driver_directions: Mapping[str, str]  # driver key -> 'positive' | 'negative'
    lag_min: int
    lag_max: int
    transform: str
    nominal_tl_series: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.driver_directions:
            raise InvalidSpecError("a relation needs at least one driver")
        if self.target in self.driver_directions:
            raise InvalidSpecError("the target cannot also be a driver")
        for key, direction in self.driver_directions.items():
            if direction not in ("positive", "negative"):
                raise InvalidSpecError(f"direction of {key!r} must be positive or negative")
        if not (0 <= self.lag_min <= self.lag_max):
            raise InvalidSpecError("lags must satisfy 0 <= lag_min <= lag_max")
        if self.transform not in TRANSFORMS:
            raise InvalidSpecError(f"transform must be one of {TRANSFORMS}")
        members = {self.target, *self.driver_directions}
        unknown = set(self.nominal_tl_series) - members
        if unknown:
            raise InvalidSpecError(f"nominal_tl_series not in the relation: {sorted(unknown)}")

    @property
    def drivers(self) -> tuple[str, ...]:
        return tuple(self.driver_directions)


@dataclass(frozen=True)
class WindowResult:
    """The outcome in one test window (same fields the graph stores per period)."""

    period: str
    result: str
    n_obs: int | None = None
    best_lag: int | None = None
    statistic_name: str | None = None
    statistic_value: float | None = None
    p_value: float | None = None
    start: date | None = None
    end: date | None = None
    data_range_start: date | None = None
    data_range_end: date | None = None
    reliable: bool = True
    reliability_reason: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TestReport:
    """Both windows plus the derived relation status."""

    __test__ = False  # not a pytest class

    windows: Mapping[str, WindowResult]
    status: str
    transform: str

    @property
    def reliable(self) -> bool:
        return all(window.reliable for window in self.windows.values())


@dataclass(frozen=True)
class MissingData:
    """Series that are not loaded at all: the caller must fetch them, nothing was tested."""

    series: tuple[str, ...]


@dataclass(frozen=True)
class Unsupported:
    """Series that cannot enter a test, with the reason per series."""

    reasons: Mapping[str, str]
