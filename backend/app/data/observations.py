"""Writer/reader for the append-only observation log.

``record_observations`` is the only writer: it compares each incoming value with
the latest stored value for that (series, period) and appends a row only when the
value changed. ``get_latest`` reads the ``latest_observations`` view.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import NamedTuple

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.data.errors import SeriesNotFoundError
from app.data.models import Observation, Series
from app.data.periods import normalize_period

# Lightweight table construct over the read-only view; columns match the view's
# SELECT order so rows can be unpacked into :class:`LatestObservation`.
latest_observations = sa.table(
    "latest_observations",
    sa.column("id", sa.BigInteger),
    sa.column("series_id", sa.Integer),
    sa.column("period", sa.Date),
    sa.column("value", sa.Numeric),
    sa.column("fetched_at", sa.DateTime(timezone=True)),
    sa.column("source_updated_at", sa.DateTime(timezone=True)),
    sa.column("raw_object_key", sa.Text),
    sa.column("fetch_job_id", sa.Integer),
    sa.column("created_at", sa.DateTime(timezone=True)),
)


class RecordResult(NamedTuple):
    """How many rows a :func:`record_observations` call inserted or left alone."""

    inserted: int
    unchanged: int


class LatestObservation(NamedTuple):
    """The winning row for one (series, period) as returned by ``get_latest``."""

    id: int
    series_id: int
    period: date
    value: Decimal | None
    fetched_at: datetime
    source_updated_at: datetime | None
    raw_object_key: str | None
    fetch_job_id: int | None
    created_at: datetime


def ensure_timezone_aware(value: datetime) -> datetime:
    """Return ``value`` if it carries a timezone, else raise ``ValueError``."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("fetched_at must be timezone-aware")
    return value


def to_decimal(value: object) -> Decimal | None:
    """Coerce an incoming observation value to ``Decimal`` (or ``None``)."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError("boolean is not a valid observation value")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation as exc:
            raise ValueError(f"invalid numeric value {value!r}") from exc
    raise TypeError(f"unsupported observation value type: {type(value).__name__}")


def values_equal(current: object, incoming: object) -> bool:
    """Compare two observation values numerically (``None`` equals only ``None``)."""
    return to_decimal(current) == to_decimal(incoming)


def record_observations(
    session: Session,
    series_id: int,
    points: Iterable[tuple[date, object]],
    *,
    fetched_at: datetime,
    raw_object_key: str | None = None,
    fetch_job_id: int | None = None,
    source_updated_at: datetime | None = None,
) -> RecordResult:
    """Append changed observations for ``series_id``; return inserted/unchanged.

    ``fetched_at`` is required and must be timezone-aware. Each period is checked
    against the series frequency; duplicates in ``points`` keep the last value.
    """
    ensure_timezone_aware(fetched_at)
    series = session.get(Series, series_id)
    if series is None:
        raise SeriesNotFoundError(f"no series with id {series_id}")

    incoming: dict[date, Decimal | None] = {}
    for period, value in points:
        incoming[normalize_period(series.frequency, period)] = to_decimal(value)

    current = _latest_values(session, series_id, incoming.keys())
    inserted = 0
    unchanged = 0
    for period, value in incoming.items():
        if period in current and values_equal(current[period], value):
            unchanged += 1
            continue
        session.add(
            Observation(
                series_id=series_id,
                period=period,
                value=value,
                fetched_at=fetched_at,
                source_updated_at=source_updated_at,
                raw_object_key=raw_object_key,
                fetch_job_id=fetch_job_id,
            )
        )
        inserted += 1

    session.flush()
    return RecordResult(inserted=inserted, unchanged=unchanged)


def _latest_values(
    session: Session, series_id: int, periods: Iterable[date]
) -> dict[date, Decimal | None]:
    period_list = list(periods)
    if not period_list:
        return {}
    rows = session.execute(
        sa.select(latest_observations.c.period, latest_observations.c.value).where(
            latest_observations.c.series_id == series_id,
            latest_observations.c.period.in_(period_list),
        )
    ).all()
    return {row.period: row.value for row in rows}


def get_latest(
    session: Session,
    series_id: int,
    start: date | None = None,
    end: date | None = None,
) -> list[LatestObservation]:
    """Return the latest value per period for a series, ascending by period."""
    statement = sa.select(latest_observations).where(latest_observations.c.series_id == series_id)
    if start is not None:
        statement = statement.where(latest_observations.c.period >= start)
    if end is not None:
        statement = statement.where(latest_observations.c.period <= end)
    statement = statement.order_by(latest_observations.c.period)
    return [LatestObservation(*row) for row in session.execute(statement).all()]
