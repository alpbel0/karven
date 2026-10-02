"""Fetch-job status constants and small helpers.

The full scheduling, heartbeat and stalling logic is Task 1.5; this module only
holds the vocabulary frozen by the schema and the couple of natural helpers used
by callers and tests.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.data.models import FetchJob

REQUESTED = "requested"
FETCHING = "fetching"
COMPLETED = "completed"
FAILED = "failed"

STATUSES = (REQUESTED, FETCHING, COMPLETED, FAILED)
ACTIVE_STATUSES = (REQUESTED, FETCHING)
TERMINAL_STATUSES = (COMPLETED, FAILED)

# A job whose heartbeat was lost is marked failed with this reason.
HEARTBEAT_LOST_REASON = "heartbeat lost"


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


def mark_status(
    session: Session,
    job: FetchJob,
    status: str,
    *,
    error_reason: str | None = None,
) -> FetchJob:
    """Move ``job`` to ``status`` and stamp the matching timestamps."""
    if status not in STATUSES:
        raise ValueError(f"unknown fetch job status {status!r}")
    now = datetime.now(UTC)
    job.status = status
    job.error_reason = error_reason
    if status == FETCHING and job.started_at is None:
        job.started_at = now
    if status in TERMINAL_STATUSES:
        job.finished_at = now
    session.flush()
    return job
