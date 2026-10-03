"""Run one on-demand fetch job with an independent heartbeat (Task 1.5).

Plain functions, no Celery import, so the whole run is unit-testable with a fake
session factory and connector.

``run_job``:

1. atomically claims the job (``UPDATE ... WHERE status='requested'``); a second
   delivery of the same job affects zero rows and is a no-op,
2. starts a heartbeat thread that beats ``heartbeat_at`` on its own short session
   while the blocking network fetch runs; a heartbeat that affects zero rows
   stops quietly (the watchdog already failed the job), a database error is
   recorded and surfaced at the end,
3. resolves the dataset/codes from ``job.attributes`` and ingests the series,
4. locks the job row and, only if it is still ``fetching``, commits observations
   and completion together; a job the watchdog declared dead is rolled back, so
   no observation from a dead job is ever written,
5. records any failure as ``failed`` with a reason on a fresh session (conditional
   on the job still being ``fetching``) and re-raises only truly unexpected ones.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.connectors.base import ConnectorError, SourceConnector, ingest_series
from app.core.loader import find_dataset
from app.data import fetch_jobs
from app.data.errors import SeriesDefinitionError
from app.data.models import FetchJob, Institution

logger = logging.getLogger(__name__)

Clock = Callable[[], datetime]
ConnectorFor = Callable[[Institution | str, object], tuple[SourceConnector, dict[str, object]]]
SessionFactory = Callable[[], Session]

#: A job carries its start date as an ISO string in ``attributes["start"]``.
_FALLBACK_START = date(2000, 1, 1)


@dataclass(frozen=True)
class RunJobResult:
    """What ``run_job`` did with one job."""

    job_id: int
    outcome: str  # skipped | completed | failed | reaped
    inserted: int = 0
    unchanged: int = 0
    error: str | None = None
    heartbeat_error: str | None = None


def _error_text(exc: BaseException) -> str:
    kind = getattr(exc, "kind", None)
    if kind is not None:
        return f"{kind}: {getattr(exc, 'message', exc)}"
    return f"{type(exc).__name__}: {exc}"


def _parse_start(value: object) -> date:
    if not value:
        return _FALLBACK_START
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return _FALLBACK_START


def _close(connector: object | None) -> None:
    close = getattr(connector, "close", None)
    if callable(close):
        close()


class _Heartbeat:
    """A daemon thread beating ``heartbeat_at`` on its own session each interval."""

    def __init__(
        self,
        session_factory: SessionFactory,
        job_id: int,
        interval: float,
        clock: Clock,
    ) -> None:
        self._session_factory = session_factory
        self._job_id = job_id
        self._interval = max(float(interval), 0.001)
        self._clock = clock
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name=f"fetch-heartbeat-{job_id}", daemon=True
        )
        self.error: str | None = None

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                with self._session_factory() as session:
                    result = session.execute(
                        sa.update(FetchJob)
                        .where(
                            FetchJob.id == self._job_id,
                            FetchJob.status == fetch_jobs.FETCHING,
                        )
                        .values(heartbeat_at=self._clock())
                    )
                    session.commit()
                if result.rowcount == 0:
                    # The watchdog already failed the job; stop quietly.
                    return
            except Exception as exc:  # noqa: BLE001 - recorded for the runner
                self.error = _error_text(exc)
                return


def _record_failure(session_factory: SessionFactory, job_id: int, error: str, clock: Clock) -> None:
    with session_factory() as session:
        fetch_jobs.fail_if_fetching(session, job_id, error_reason=error, now=clock())
        session.commit()


def run_job(
    session_factory: SessionFactory,
    job_id: int,
    *,
    connector_for: ConnectorFor,
    clock: Clock,
    heartbeat_interval: float,
) -> RunJobResult:
    """Claim and run one fetch job; never leave it ``fetching``."""
    moment = clock()
    with session_factory() as session:
        claimed = session.execute(
            sa.update(FetchJob)
            .where(FetchJob.id == job_id, FetchJob.status == fetch_jobs.REQUESTED)
            .values(
                status=fetch_jobs.FETCHING,
                started_at=moment,
                heartbeat_at=moment,
            )
        )
        session.commit()
        if claimed.rowcount == 0:
            # Already claimed or terminal: a duplicate delivery must not re-run.
            return RunJobResult(job_id, "skipped")
        job = session.get(FetchJob, job_id)
        if job is None or job.status != fetch_jobs.FETCHING:
            return RunJobResult(job_id, "skipped")
        attributes = dict(job.attributes or {})

        heartbeat = _Heartbeat(session_factory, job_id, heartbeat_interval, clock)
        heartbeat.start()
        connector: SourceConnector | None = None
        try:
            institution = session.get(Institution, job.institution_id)
            dataset_code = attributes.get("dataset_code")
            codes = attributes.get("codes")
            if institution is None or not dataset_code or not codes:
                raise LookupError(
                    f"job {job_id} has no resolvable dataset (attributes={attributes!r})"
                )
            dataset = find_dataset(session, institution.code, str(dataset_code))
            if dataset is None:
                raise LookupError(
                    f"dataset {dataset_code!r} is not catalogued for institution "
                    f"{institution.code!r}"
                )
            connector, channel_kwargs = connector_for(institution, dataset)
            ingest = ingest_series(
                session,
                connector,
                dataset=dataset,
                codes=dict(codes),
                start=_parse_start(attributes.get("start")),
                **channel_kwargs,
            )
            _close(connector)
            connector = None

            locked = session.scalar(
                sa.select(FetchJob)
                .where(FetchJob.id == job_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if locked is None or locked.status != fetch_jobs.FETCHING:
                # The watchdog declared the job dead while we fetched: drop the
                # observations with the job (no data from a dead job).
                session.rollback()
                return RunJobResult(job_id, "reaped", heartbeat_error=heartbeat.error)
            fetch_jobs.mark_completed(session, locked, now=clock())
            session.commit()
            if heartbeat.error is not None:
                logger.warning(
                    "fetch job %d completed but its heartbeat errored: %s",
                    job_id,
                    heartbeat.error,
                )
            return RunJobResult(
                job_id,
                "completed",
                inserted=ingest.inserted,
                unchanged=ingest.unchanged,
                heartbeat_error=heartbeat.error,
            )
        except (ConnectorError, SeriesDefinitionError, LookupError) as exc:
            session.rollback()
            error = _error_text(exc)
            _record_failure(session_factory, job_id, error, clock)
            return RunJobResult(job_id, "failed", error=error, heartbeat_error=heartbeat.error)
        except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
            session.rollback()
            error = _error_text(exc)
            _record_failure(session_factory, job_id, error, clock)
            raise
        finally:
            _close(connector)
            heartbeat.stop()
            heartbeat.join()


__all__ = ["Clock", "ConnectorFor", "RunJobResult", "run_job"]
