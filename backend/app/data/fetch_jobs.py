"""Fetch-job status constants and helpers.

The full scheduling, heartbeat and stalling logic is Task 1.5; this module holds
the vocabulary frozen by the schema plus the natural helpers used by callers:

- status transitions (``create_job``/``mark_status`` and its wrappers) and the
  background-condition helper ``fail_if_fetching``,
- parking waiters: ``attach_waiter`` records a future idea/visual that waits for
  the job, and every terminal transition resolves the job's ``waiting`` waiters
  (``ready`` on completion, ``dropped`` on failure).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.data.models import FetchJob, FetchJobWaiter

REQUESTED = "requested"
FETCHING = "fetching"
COMPLETED = "completed"
FAILED = "failed"

STATUSES = (REQUESTED, FETCHING, COMPLETED, FAILED)
ACTIVE_STATUSES = (REQUESTED, FETCHING)
TERMINAL_STATUSES = (COMPLETED, FAILED)

#: Waiter statuses (frozen by the schema check constraint).
WAITING = "waiting"
READY = "ready"
DROPPED = "dropped"
WAITER_STATUSES = (WAITING, READY, DROPPED)

# A job whose heartbeat was lost is marked failed with this reason.
HEARTBEAT_LOST_REASON = "heartbeat lost"

#: Terminal job status -> the status of its parked waiters.
_WAITER_ON_TERMINAL = {COMPLETED: READY, FAILED: DROPPED}


def create_job(
    session: Session,
    *,
    institution_id: int,
    external_code: str,
    series_id: int | None = None,
    attributes: dict[str, Any] | None = None,
) -> FetchJob:
    """Open a ``requested`` job. A second active job for the series is rejected."""
    job = FetchJob(
        institution_id=institution_id,
        external_code=external_code,
        series_id=series_id,
        status=REQUESTED,
        attributes=attributes if attributes is not None else {},
    )
    session.add(job)
    session.flush()
    return job


def _resolve_waiters(job: FetchJob, status: str, moment: datetime) -> None:
    """Move every ``waiting`` waiter of ``job`` to a terminal waiter status."""
    for waiter in job.waiters:
        if waiter.status == WAITING:
            waiter.status = status
            waiter.resolved_at = moment


def mark_status(
    session: Session,
    job: FetchJob,
    status: str,
    *,
    error_reason: str | None = None,
    now: datetime | None = None,
) -> FetchJob:
    """Move ``job`` to ``status`` and stamp the matching timestamps.

    A terminal status also resolves the job's parked waiters in the same flush
    (``ready`` on completion, ``dropped`` on failure), whatever the job's origin.
    """
    if status not in STATUSES:
        raise ValueError(f"unknown fetch job status {status!r}")
    moment = now or datetime.now(UTC)
    job.status = status
    job.error_reason = error_reason
    if status == FETCHING:
        if job.started_at is None:
            job.started_at = moment
        job.heartbeat_at = moment
    if status in TERMINAL_STATUSES:
        job.finished_at = moment
        _resolve_waiters(job, _WAITER_ON_TERMINAL[status], moment)
    session.flush()
    return job


def mark_fetching(session: Session, job: FetchJob, *, now: datetime | None = None) -> FetchJob:
    """Start fetching ``job`` and stamp ``started_at``/``heartbeat_at``."""
    return mark_status(session, job, FETCHING, now=now)


def mark_completed(session: Session, job: FetchJob, *, now: datetime | None = None) -> FetchJob:
    """Finish ``job`` successfully and stamp ``finished_at``."""
    return mark_status(session, job, COMPLETED, now=now)


def mark_failed(
    session: Session,
    job: FetchJob,
    *,
    error_reason: str | None = None,
    now: datetime | None = None,
) -> FetchJob:
    """Fail ``job`` with ``error_reason`` and stamp ``finished_at``."""
    return mark_status(session, job, FAILED, error_reason=error_reason, now=now)


def fail_if_fetching(
    session: Session,
    job_id: int,
    *,
    error_reason: str | None = None,
    now: datetime | None = None,
) -> FetchJob | None:
    """Fail ``job_id`` only while it is still ``fetching``; None if it is not.

    The row is locked ``FOR UPDATE`` and its status re-checked, so a job that the
    runner or watchdog already moved to a terminal status is never overwritten.
    """
    job = session.scalar(
        sa.select(FetchJob)
        .where(FetchJob.id == job_id)
        .with_for_update()
        # The session keeps objects in its identity map after commit
        # (``expire_on_commit=False``); refresh the locked row so the status is
        # the one the lock actually observed, not a stale in-memory copy.
        .execution_options(populate_existing=True)
    )
    if job is None or job.status != FETCHING:
        return None
    return mark_failed(session, job, error_reason=error_reason, now=now)


def stamp_enqueued(
    session: Session,
    job_id: int,
    *,
    now: datetime | None = None,
) -> FetchJob | None:
    """Record ``attributes["last_enqueued_at"]`` (ISO UTC) on ``job_id``.

    The row is re-read fresh (and locked), so the merge never clobbers another
    writer's attributes; only the one key changes. Returns None for a missing job.
    """
    moment = (now or datetime.now(UTC)).isoformat()
    job = session.scalar(
        sa.select(FetchJob)
        .where(FetchJob.id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if job is None:
        return None
    attributes = dict(job.attributes or {})
    attributes["last_enqueued_at"] = moment
    job.attributes = attributes
    session.flush()
    return job


def attach_waiter(
    session: Session,
    job: FetchJob,
    *,
    waiter_type: str,
    waiter_id: str,
    now: datetime | None = None,
) -> FetchJobWaiter:
    """Park ``(waiter_type, waiter_id)`` on ``job``; re-attaching is idempotent."""
    existing = session.scalar(
        sa.select(FetchJobWaiter).where(
            FetchJobWaiter.fetch_job_id == job.id,
            FetchJobWaiter.waiter_type == waiter_type,
            FetchJobWaiter.waiter_id == waiter_id,
        )
    )
    if existing is not None:
        return existing
    waiter = FetchJobWaiter(
        fetch_job_id=job.id,
        waiter_type=waiter_type,
        waiter_id=waiter_id,
        status=WAITING,
    )
    if now is not None:
        waiter.created_at = now
    job.waiters.append(waiter)
    # Appending through the relationship cascades to the session; add explicitly
    # so a session double that does not run the unit of work still stores it.
    session.add(waiter)
    session.flush()
    return waiter


def waiters_for(session: Session, waiter_type: str, waiter_id: str) -> list[FetchJobWaiter]:
    """Every waiter row for one parked idea/visual, newest job first."""
    return list(
        session.scalars(
            sa.select(FetchJobWaiter)
            .where(
                FetchJobWaiter.waiter_type == waiter_type,
                FetchJobWaiter.waiter_id == waiter_id,
            )
            .order_by(FetchJobWaiter.id.desc())
        ).all()
    )


def ready_waiters(session: Session, waiter_type: str) -> list[FetchJobWaiter]:
    """Every ``ready`` waiter of ``waiter_type`` (for the future consumer)."""
    return list(
        session.scalars(
            sa.select(FetchJobWaiter)
            .where(
                FetchJobWaiter.waiter_type == waiter_type,
                FetchJobWaiter.status == READY,
            )
            .order_by(FetchJobWaiter.id)
        ).all()
    )
