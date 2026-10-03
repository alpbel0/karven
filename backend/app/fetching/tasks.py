"""Celery tasks for on-demand fetching, its watchdog and its agent (1.5, 1.6).

Every task builds its own database engine/session factory, so the worker holds
no connection across tasks. ``fetch.run_job`` delegates to
:func:`app.fetching.runner.run_job`; ``fetch.watchdog`` reaps stalled jobs and
re-enqueues undelivered ones; ``fetch.diagnose_job`` hands a failed on-demand job
to the data-fetch agent (:func:`app.fetching.agent.service.diagnose_job`).
``enqueue_job``/``enqueue_diagnose`` are the dispatch entry points used by
``request_and_dispatch``, the watchdog and the runner.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.data import fetch_jobs
from app.db.session import create_db_engine
from app.fetching.agent.service import default_chat_client_factory, diagnose_job
from app.fetching.celery_app import app
from app.fetching.connectors import connector_for
from app.fetching.runner import run_job
from app.fetching.watchdog import WatchdogReport, reap_stalled

logger = logging.getLogger(__name__)


def _real_clock() -> datetime:
    return datetime.now(UTC)


def _session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


def _safe_enqueue_diagnose(job_id: int) -> None:
    """Enqueue a diagnosis; a broker failure is logged, never masking the result."""
    try:
        enqueue_diagnose(job_id)
    except Exception:  # noqa: BLE001 - must not mask the runner's outcome
        logger.exception("fetch diagnose job %s could not be enqueued", job_id)


@app.task(name="fetch.run_job", acks_late=True, ignore_result=True)
def run_job_task(job_id: int) -> None:
    """Run one fetch job to a terminal status; diagnose it when it fails."""
    engine = create_db_engine()
    try:
        result = run_job(
            _session_factory(engine),
            job_id,
            connector_for=connector_for,
            clock=_real_clock,
            heartbeat_interval=settings.fetch_heartbeat_interval_seconds,
        )
        if result.outcome == "failed":
            _safe_enqueue_diagnose(job_id)
    except Exception:
        # ``run_job`` records the failure before re-raising, so the job is failed
        # and diagnosable; enqueue, then re-raise the unexpected error.
        _safe_enqueue_diagnose(job_id)
        raise
    finally:
        engine.dispose()


@app.task(name="fetch.watchdog", ignore_result=True)
def watchdog_task() -> None:
    """Fail stalled on-demand jobs, re-enqueue undelivered ones, diagnose reaped."""
    engine = create_db_engine()
    try:
        with _session_factory(engine)() as session:
            report: WatchdogReport = reap_stalled(
                session,
                now=_real_clock(),
                stall_after=timedelta(minutes=settings.fetch_heartbeat_stall_minutes),
            )
        for job_id in report.requeued:
            enqueue_job(job_id)
            # Stamp only after a successful hand-off; a failed enqueue leaves the
            # job immediately re-eligible on the next tick.
            with _session_factory(engine)() as session:
                fetch_jobs.stamp_enqueued(session, job_id, now=_real_clock())
                session.commit()
        for job_id in report.reaped:
            _safe_enqueue_diagnose(job_id)
    finally:
        engine.dispose()
    logger.info("fetch watchdog: reaped=%d requeued=%d", len(report.reaped), len(report.requeued))


@app.task(name="fetch.diagnose_job", acks_late=True, ignore_result=True)
def diagnose_job_task(job_id: int) -> None:
    """Diagnose one failed on-demand job with the data-fetch agent.

    The agent records a failure row per ``(job, round)``, so a re-delivered task
    is a no-op instead of a crash loop. An agent error is recorded, not raised.
    """
    engine = create_db_engine()
    try:
        diagnose_job(
            _session_factory(engine),
            job_id,
            chat_client_factory=default_chat_client_factory,
            max_rounds=settings.fetch_agent_max_rounds,
        )
    finally:
        engine.dispose()


def enqueue_job(job_id: int) -> None:
    """Put one job on the ``fetch`` queue; the runner's claim makes it idempotent."""
    run_job_task.apply_async(args=[job_id], queue="fetch")


def enqueue_diagnose(job_id: int) -> None:
    """Put one job on the ``fetch`` queue for the diagnosis task."""
    diagnose_job_task.apply_async(args=[job_id], queue="fetch")


__all__ = [
    "diagnose_job_task",
    "enqueue_diagnose",
    "enqueue_job",
    "run_job_task",
    "watchdog_task",
]
