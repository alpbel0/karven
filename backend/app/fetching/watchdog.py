"""Stalled and undelivered on-demand jobs (Task 1.5).

Only on-demand jobs are watched: ``origin`` is ``on_demand`` or ``agent_retry``
(a retry the data-fetch agent opened is still on-demand work). Core-refresh and
calendar-sync jobs live in the scheduler process, set ``heartbeat_at`` once on
purpose, and must never be touched here.

``reap_stalled``:

- a ``fetching`` on-demand job whose ``heartbeat_at`` is older than
  ``now - stall_after`` is failed with ``heartbeat lost`` (conditional: it can
  never overwrite a job that just completed), and its waiters become ``dropped``,
- a ``requested`` on-demand job whose most recent request/dispatch
  (``max(requested_at, attributes["last_enqueued_at"])``) is older than the same
  threshold (Redis/worker was down when it was dispatched) is returned for
  re-enqueue; its status is left alone, and the runner's claim makes the retry
  idempotent. Stamping ``last_enqueued_at`` after a successful hand-off is what
  keeps a merely backed-up queue from re-publishing the same job every tick.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.data import fetch_jobs
from app.data.fetch_requests import WATCHED_ORIGINS
from app.data.models import FetchJob

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WatchdogReport:
    """Job ids the watchdog failed and the ids it wants re-enqueued."""

    reaped: list[int] = field(default_factory=list)
    requeued: list[int] = field(default_factory=list)


def _is_on_demand(job: FetchJob) -> bool:
    # Both an on-demand request and an agent-opened retry are watched: a retry is
    # still on-demand work, so a lost heartbeat must be reaped and diagnosed.
    return (job.attributes or {}).get("origin") in WATCHED_ORIGINS


def _last_enqueued_at(job: FetchJob) -> datetime | None:
    """Parse ``attributes["last_enqueued_at"]`` (ISO UTC), or None."""
    value = (job.attributes or {}).get("last_enqueued_at")
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment


def _stalled_reference(job: FetchJob) -> datetime | None:
    """The most recent moment the job was requested or (re-)enqueued."""
    moments = [m for m in (job.requested_at, _last_enqueued_at(job)) if m is not None]
    return max(moments) if moments else None


def reap_stalled(session: Session, *, now: datetime, stall_after: timedelta) -> WatchdogReport:
    """Fail stalled fetching jobs and return requested jobs to re-enqueue."""
    cutoff = now - stall_after
    reaped: list[int] = []
    requeued: list[int] = []

    fetching = session.scalars(
        sa.select(FetchJob).where(FetchJob.status == fetch_jobs.FETCHING)
    ).all()
    for job in fetching:
        if not _is_on_demand(job):
            continue
        if job.heartbeat_at is None or job.heartbeat_at >= cutoff:
            continue
        # Lock and re-check the status so a job that just completed is not
        # overwritten; a job the run already resolved is skipped.
        if (
            fetch_jobs.fail_if_fetching(
                session, job.id, error_reason=fetch_jobs.HEARTBEAT_LOST_REASON, now=now
            )
            is not None
        ):
            reaped.append(job.id)

    requested = session.scalars(
        sa.select(FetchJob).where(FetchJob.status == fetch_jobs.REQUESTED)
    ).all()
    for job in requested:
        if not _is_on_demand(job):
            continue
        reference = _stalled_reference(job)
        if reference is not None and reference < cutoff:
            requeued.append(job.id)

    session.commit()
    logger.info("fetch watchdog: reaped=%d requeued=%d", len(reaped), len(requeued))
    return WatchdogReport(reaped=reaped, requeued=requeued)


__all__ = ["WatchdogReport", "reap_stalled"]
