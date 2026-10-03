"""On-demand fetch requests: open or attach to a job and park the waiter (Task 1.5).

A request names one series (institution, dataset, one code per non-time
dimension) plus an optional parked waiter (an idea or visual). The rules:

- request the same series again while a job is active -> no new job; the request
  is attached to the active job (whatever its origin, e.g. the core scheduler's)
  and the waiter is parked on it. Re-attaching the same waiter is idempotent.
- if the active job finished at that exact moment -> a NEW job is opened, so a
  waiter is never left waiting on a terminal job.
- creating the job is racy with concurrent callers: a savepoint + the partial
  unique index turn the loser into an attach.

``request_fetch`` only writes the database rows (no broker), so it is unit
testable. ``request_and_dispatch`` commits, then calls ``dispatch`` only when a
new job was opened; if Redis is down the job stays ``requested`` (the watchdog
re-enqueues it) and the error surfaces to the caller after the commit.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.connectors.base import build_series_definition
from app.core.loader import find_dataset, find_institution, find_series
from app.data import fetch_jobs
from app.data.models import FetchJob

#: Job origin for a request made through this module.
ON_DEMAND = "on_demand"

#: Job origin set by the data-fetch agent when it opens a retry (Task 1.6).
AGENT_RETRY = "agent_retry"

#: Origins the watchful code (watchdog, diagnose) treats as on-demand work.
WATCHED_ORIGINS = (ON_DEMAND, AGENT_RETRY)

#: The history window every on-demand fetch uses (DECISIONS §5).
DEFAULT_START = date(2000, 1, 1)

Waiter = tuple[str, str]
Dispatch = Callable[[int], None]


@dataclass(frozen=True)
class FetchRequest:
    """Outcome of one :func:`request_fetch` call."""

    job_id: int
    created: bool
    status: str


def _active_job(session: Session, *, institution_id: int, external_code: str) -> FetchJob | None:
    """The active job for the series, locked, or None when there is none.

    ``FOR UPDATE`` blocks on a concurrent terminal transition; Postgres then
    re-checks the predicate, so a job that finished meanwhile is not returned.
    """
    return session.scalar(
        sa.select(FetchJob)
        .where(
            FetchJob.institution_id == institution_id,
            FetchJob.external_code == external_code,
            FetchJob.status.in_(fetch_jobs.ACTIVE_STATUSES),
        )
        .with_for_update()
        # Refresh any identity-map copy so the locked row's real status decides.
        .execution_options(populate_existing=True)
    )


def _attach_waiter(session: Session, job: FetchJob, waiter: Waiter | None) -> None:
    if waiter is None:
        return
    waiter_type, waiter_id = waiter
    fetch_jobs.attach_waiter(session, job, waiter_type=waiter_type, waiter_id=waiter_id)


def request_fetch(
    session: Session,
    *,
    institution_code: str,
    dataset_code: str,
    codes: dict[str, str],
    waiter: Waiter | None = None,
    start: date = DEFAULT_START,
) -> FetchRequest:
    """Open a fetch job for the series, or attach to its active job.

    ``codes`` is validated at request time through
    :func:`app.connectors.base.build_series_definition`, so a bad breakdown
    raises ``SeriesDefinitionError`` before any row is written. Raises
    ``LookupError`` for an unknown institution or dataset.
    """
    institution = find_institution(session, institution_code)
    if institution is None:
        raise LookupError(f"institution {institution_code!r} is not catalogued")
    dataset = find_dataset(session, institution_code, dataset_code)
    if dataset is None:
        raise LookupError(
            f"dataset {dataset_code!r} is not catalogued for institution {institution_code!r}"
        )
    definition = build_series_definition(dataset, codes)
    external_code = definition.external_code

    series = find_series(session, institution_code, external_code)
    series_id = series.id if series is not None else None
    attributes = {
        "origin": ON_DEMAND,
        "dataset_code": dataset_code,
        "codes": dict(codes),
        "start": start.isoformat(),
    }

    existing = _active_job(session, institution_id=institution.id, external_code=external_code)
    if existing is not None and existing.status in fetch_jobs.ACTIVE_STATUSES:
        _attach_waiter(session, existing, waiter)
        session.flush()
        return FetchRequest(existing.id, created=False, status=existing.status)

    try:
        with session.begin_nested():
            job = fetch_jobs.create_job(
                session,
                institution_id=institution.id,
                external_code=external_code,
                series_id=series_id,
                attributes=attributes,
            )
            _attach_waiter(session, job, waiter)
        return FetchRequest(job.id, created=True, status=job.status)
    except IntegrityError:
        # A concurrent caller opened the active job first; attach to it instead.
        job = _active_job(session, institution_id=institution.id, external_code=external_code)
        if job is None:
            raise
        _attach_waiter(session, job, waiter)
        session.flush()
        return FetchRequest(job.id, created=False, status=job.status)


def request_and_dispatch(
    session: Session,
    *,
    institution_code: str,
    dataset_code: str,
    codes: dict[str, str],
    waiter: Waiter | None = None,
    start: date = DEFAULT_START,
    dispatch: Dispatch | None = None,
) -> FetchRequest:
    """``request_fetch`` plus commit and enqueue, only for a newly opened job.

    The commit happens before the dispatch: a broker failure must not lose the
    request (the watchdog re-enqueues a stuck ``requested`` job), and the error
    is re-raised to the caller after the job is durable.
    """
    result = request_fetch(
        session,
        institution_code=institution_code,
        dataset_code=dataset_code,
        codes=codes,
        waiter=waiter,
        start=start,
    )
    session.commit()
    if result.created:
        if dispatch is None:
            from app.fetching.tasks import enqueue_job

            dispatch = enqueue_job
        dispatch(result.job_id)
        # Only after a successful hand-off: the watchdog uses this to avoid
        # re-publishing the same undelivered job on every tick.
        fetch_jobs.stamp_enqueued(session, result.job_id, now=datetime.now(UTC))
        session.commit()
    return result


__all__ = [
    "AGENT_RETRY",
    "DEFAULT_START",
    "ON_DEMAND",
    "WATCHED_ORIGINS",
    "FetchRequest",
    "Waiter",
    "request_and_dispatch",
    "request_fetch",
]
