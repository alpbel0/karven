"""Query-time "is this series loaded?" helper (Task 2.2 decision 3).

Whether a series has observations is derived from the ``observations`` table,
never stored on the dataset. This keeps the answer exact when values are fetched
or (in tests) deleted, with no denormalised column to drift.

The helper runs one grouped query for all requested ids (no N+1). The query is
exposed separately so its SQL shape is unit-testable without a database.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.data.models import Observation


def loaded_series_query(series_ids: Iterable[int]) -> Select[tuple[int]]:
    """The single grouped query behind :func:`series_loaded`."""
    return (
        select(Observation.series_id)
        .where(Observation.series_id.in_(list(series_ids)))
        .group_by(Observation.series_id)
    )


def series_loaded(session: Session, series_ids: Iterable[int]) -> dict[int, bool]:
    """Map each series id to whether it has at least one observation.

    Returns an entry for every requested id (``False`` when it has none), so a
    caller can zip ids and results without a second lookup.
    """
    ids = list(series_ids)
    if not ids:
        return {}
    loaded = set(session.execute(loaded_series_query(ids)).scalars().all())
    return {series_id: series_id in loaded for series_id in ids}


__all__ = ["loaded_series_query", "series_loaded"]
