"""Frozen records and input validation for the relation graph.

Nothing here decides anything (no statistics, no LLM): the models carry what a
writer supplies, validate the closed enums and structural rules documented in
``docs/DECISIONS.md`` §9, and are returned by the repository.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType
from typing import Any

from app.graph.errors import InvalidRelationError
from app.graph.keys import series_key

EXPECTED_DIRECTIONS: tuple[str, ...] = ("positive", "negative")
TRANSFORMS: tuple[str, ...] = ("annual_pct_change", "period_pct_change", "difference")
RELATION_STATUSES: tuple[str, ...] = (
    "hypothesis",
    "supported",
    "unsupported",
    "periods_differ",
    "insufficient_data",
)
PERIODS: tuple[str, ...] = ("all_years", "2017_2026")
PERIOD_RESULTS: tuple[str, ...] = ("supported", "unsupported", "insufficient_data")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class SeriesRef:
    """A series identity in the graph, independent of the PostgreSQL row.

    ``key`` is derived (not stored as a field) so it can never disagree with
    ``institution``/``code``.
    """

    institution: str
    code: str

    def __post_init__(self) -> None:
        series_key(self.institution, self.code)

    @property
    def key(self) -> str:
        return series_key(self.institution, self.code)


@dataclass(frozen=True)
class Hypothesis:
    """The first hypothesis for a relation; also reused as a news proposal.

    ``driver_directions`` maps each driver's series key to ``'positive'`` or
    ``'negative'``; every driver has exactly one direction (a 2-series relation
    has exactly one entry). It is validated against the relation's drivers by
    :func:`validate_driver_directions` at write time. Lag values are integers in
    periods of the target frequency and must satisfy ``0 <= lag_min <= lag_max``.
    ``nominal_tl_series`` holds series keys that are subset-checked against the
    relation members by :func:`validate_nominal_members`.
    """

    mechanism: str
    driver_directions: Mapping[str, str]
    lag_min: int
    lag_max: int
    transform: str
    nominal_tl_series: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.mechanism, str) or not self.mechanism.strip():
            raise InvalidRelationError("mechanism must be a non-empty string")
        if not isinstance(self.driver_directions, Mapping) or not self.driver_directions:
            raise InvalidRelationError(
                "driver_directions must be a non-empty mapping of series key -> direction"
            )
        for key, direction in self.driver_directions.items():
            if not isinstance(key, str) or not key:
                raise InvalidRelationError("driver_directions keys must be non-empty strings")
            if direction not in EXPECTED_DIRECTIONS:
                raise InvalidRelationError(
                    f"driver_directions values must be one of {EXPECTED_DIRECTIONS}, "
                    f"got {direction!r} for {key!r}"
                )
        if self.transform not in TRANSFORMS:
            raise InvalidRelationError(
                f"transform must be one of {TRANSFORMS}, got {self.transform!r}"
            )
        if not _is_int(self.lag_min) or not _is_int(self.lag_max):
            raise InvalidRelationError("lag_min and lag_max must be integers")
        if self.lag_min < 0 or self.lag_max < 0:
            raise InvalidRelationError("lag_min and lag_max must be >= 0")
        if self.lag_min > self.lag_max:
            raise InvalidRelationError("lag_min must not be greater than lag_max")
        if isinstance(self.nominal_tl_series, str):
            raise InvalidRelationError("nominal_tl_series must be a collection of series keys")
        for key in self.nominal_tl_series:
            if not isinstance(key, str) or not key:
                raise InvalidRelationError("nominal_tl_series entries must be non-empty strings")
        # Sort for deterministic equality/repr; wrap so the frozen record stays
        # read-only. A mapping proxy is not hashable, but hypotheses are never
        # hashed (only SeriesRef is, when building member sets).
        ordered = dict(sorted(self.driver_directions.items()))
        object.__setattr__(self, "driver_directions", MappingProxyType(ordered))
        object.__setattr__(self, "nominal_tl_series", tuple(self.nominal_tl_series))


@dataclass(frozen=True)
class PeriodResult:
    """One period's test result. Every numeric/boundary field may be null.

    A result may be accompanied by an explicit reliability warning (e.g. strong
    dependence / small effective sample). ``reliable=False`` requires a non-empty
    ``reliability_reason``; ``reliable=True`` must carry no reason, so the warning
    is never buried in free text.
    """

    period: str
    result: str
    n_obs: int | None = None
    best_lag: int | None = None
    statistic_name: str | None = None
    statistic_value: float | None = None
    p_value: float | None = None
    start: date | None = None
    end: date | None = None
    transform: str | None = None
    data_range_start: date | None = None
    data_range_end: date | None = None
    details: str | None = None
    reliable: bool = True
    reliability_reason: str | None = None

    def __post_init__(self) -> None:
        if self.period not in PERIODS:
            raise InvalidRelationError(f"period must be one of {PERIODS}, got {self.period!r}")
        if self.result not in PERIOD_RESULTS:
            raise InvalidRelationError(
                f"result must be one of {PERIOD_RESULTS}, got {self.result!r}"
            )
        if self.details is not None and not isinstance(self.details, str):
            raise InvalidRelationError("details must be a JSON string or None")
        if not isinstance(self.reliable, bool):
            raise InvalidRelationError("reliable must be a boolean")
        if self.reliable:
            if self.reliability_reason is not None:
                raise InvalidRelationError("reliability_reason must be None when reliable is True")
        elif not isinstance(self.reliability_reason, str) or not self.reliability_reason.strip():
            raise InvalidRelationError(
                "reliability_reason must be a non-empty string when reliable is False"
            )


@dataclass(frozen=True)
class TestRecord:
    """One immutable ``RelationTest`` node with its per-period results."""

    id: str
    relation_key: str
    tested_at: datetime
    period_results: tuple[PeriodResult, ...] = ()


@dataclass(frozen=True)
class NewsLink:
    """A ``(:News)-[:SOURCE_OF]->(:Relation)`` edge with the news' proposal."""

    news_id: int
    proposal: Hypothesis
    linked_at: datetime


@dataclass(frozen=True)
class RelationRecord:
    """A relation with its members, hypothesis, status, news and tests.

    ``reliable`` is ``None`` for a fresh hypothesis (no test yet); after
    ``add_test`` it is true only if both period results of that test are reliable.
    """

    key: str
    target: SeriesRef
    drivers: tuple[SeriesRef, ...]
    hypothesis: Hypothesis
    status: str
    reliable: bool | None = None
    created_at: datetime | None = None
    news_links: tuple[NewsLink, ...] = ()
    tests: tuple[TestRecord, ...] = ()


def normalize_relation(target: SeriesRef, drivers: Any) -> tuple[SeriesRef, tuple[SeriesRef, ...]]:
    """Validate the relation shape and return deduplicated, sorted drivers.

    Rules: one target, at least one driver, at least two distinct series in
    total, the target is never a driver, duplicate drivers collapse. The role
    (target vs driver) is part of the identity: swapping them is another relation.
    """
    if not isinstance(target, SeriesRef):
        raise InvalidRelationError("target must be a SeriesRef")
    try:
        driver_list = list(drivers)
    except TypeError as exc:
        raise InvalidRelationError("drivers must be a collection of SeriesRef") from exc
    if not driver_list:
        raise InvalidRelationError("a relation needs at least one driver series")
    for driver in driver_list:
        if not isinstance(driver, SeriesRef):
            raise InvalidRelationError("every driver must be a SeriesRef")
    unique = {driver.key: driver for driver in driver_list}
    if target.key in unique:
        raise InvalidRelationError("the target series must not also be a driver")
    if len(unique) + 1 < 2:
        raise InvalidRelationError("a relation needs at least two distinct series")
    ordered = tuple(unique[key] for key in sorted(unique))
    return target, ordered


def validate_nominal_members(
    hypothesis: Hypothesis, target: SeriesRef, drivers: tuple[SeriesRef, ...]
) -> None:
    """Reject nominal TL keys that are not members of the relation."""
    member_keys = {target.key, *(driver.key for driver in drivers)}
    unknown = [key for key in hypothesis.nominal_tl_series if key not in member_keys]
    if unknown:
        raise InvalidRelationError(
            f"nominal_tl_series must be a subset of the relation's series, unknown: {unknown}"
        )


def validate_driver_directions(hypothesis: Hypothesis, drivers: tuple[SeriesRef, ...]) -> None:
    """Require ``driver_directions`` to cover exactly the relation's drivers."""
    driver_keys = {driver.key for driver in drivers}
    provided = set(hypothesis.driver_directions)
    if provided != driver_keys:
        missing = sorted(driver_keys - provided)
        extra = sorted(provided - driver_keys)
        raise InvalidRelationError(
            "driver_directions must cover exactly the driver series "
            f"(missing={missing}, extra={extra})"
        )


def validate_status(status: str) -> None:
    """Reject a relation status outside the closed set from ``DECISIONS`` §9."""
    if status not in RELATION_STATUSES:
        raise InvalidRelationError(f"status must be one of {RELATION_STATUSES}, got {status!r}")
