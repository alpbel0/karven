"""The core scheduler: one continuous loop over the core refresh (Task 1.4d).

``python -m app.core.scheduler [--once] [--tick-minutes N]``

``run_tick`` is one fully injectable pass:

1. Re-sync the release calendar when due. The outcome is RECORDED, never just
   logged: a ``fetch_jobs`` row (institution ``tuik``, ``external_code``
   ``calendar:sync``) moves ``requested -> fetching -> completed/failed``; a
   ``FORMAT_CHANGED`` error opens a ``format_changed`` alert immediately, the
   ``core_failure_threshold``-th consecutive failed calendar job opens a
   ``repeated_failure`` alert, and a success resolves both. A calendar failure
   never stops step 2.
2. Run :func:`app.core.refresh.refresh_core`.

``run_forever`` loops ``run_tick`` every ``core_tick_minutes``, writing a
heartbeat file after each tick; five consecutive crashed ticks exit non-zero (the
container restart policy takes over) and SIGINT/SIGTERM finish the current step
and exit 0. Celery (worker/beat) is Tasks 1.5/1.7 and is deliberately untouched.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.config import Settings
from app.config import settings as global_settings
from app.connectors.base import FORMAT_CHANGED, ConnectorError, MinioObjectStore
from app.connectors.tcmb.connector import TcmbConnector
from app.core.alerts import (
    FORMAT_CHANGED as ALERT_FORMAT_CHANGED,
)
from app.core.alerts import (
    REPEATED_FAILURE,
    open_alert,
    resolve_alerts,
)
from app.core.calendar import CalendarSyncResult, sync_calendar
from app.core.clients import (
    build_connector,
    build_evds_calendar_client,
    build_tuik_calendar_client,
)
from app.core.loader import find_institution
from app.core.refresh import Clock, RefreshReport, refresh_core
from app.data import fetch_jobs
from app.data.models import FetchJob, ReleaseCalendar

logger = logging.getLogger(__name__)

#: The calendar sync is recorded as a job for this (fixed) external code.
CALENDAR_CODE = "calendar:sync"
#: Alert scope for source-level calendar failures.
CALENDAR_SCOPE = "calendar:sync"
#: Attribute value that marks a fetch_jobs row as a core calendar sync.
CALENDAR_ORIGIN = "core_calendar"
#: The institution the calendar job/alerts are recorded under.
CALENDAR_INSTITUTION = "tuik"

#: How many consecutive crashed ticks end the process (restart policy handles it).
CRASH_LIMIT = 5

#: A calendar sync callable: run the sync inside ``session``; it commits nothing.
CalendarSync = Callable[..., CalendarSyncResult]


def _real_clock() -> datetime:
    return datetime.now(UTC)


def _error_text(exc: BaseException) -> str:
    kind = getattr(exc, "kind", None)
    if kind is not None:
        return f"{kind}: {getattr(exc, 'message', exc)}"
    return f"{type(exc).__name__}: {exc}"


# --- reports ----------------------------------------------------------------


@dataclass(frozen=True)
class CalendarState:
    """What the "is a calendar sync due?" rule needs, with no session around."""

    latest_fetched_at: datetime | None = None
    last_attempt_at: datetime | None = None


@dataclass(frozen=True)
class CalendarOutcome:
    """What the scheduler did with the release calendar in one tick."""

    due: bool = False
    outcome: str = "skipped"  # skipped | completed | failed
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    error: str | None = None
    alerts_opened: list[str] = field(default_factory=list)
    alerts_resolved: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return self.outcome == "failed"


@dataclass(frozen=True)
class TickReport:
    """The whole tick: calendar outcome, the refresh report, any escaping error."""

    calendar: CalendarOutcome
    refresh: RefreshReport | None = None
    error: str | None = None

    @property
    def failed(self) -> bool:
        return (
            self.error is not None
            or self.calendar.failed
            or (self.refresh is not None and self.refresh.failed)
        )


# --- pure due rule ----------------------------------------------------------


def calendar_sync_due(
    state: CalendarState,
    now: datetime,
    *,
    config: Settings | None = None,
) -> bool:
    """Due when the calendar is missing/stale and the last attempt is old enough.

    "Stale" is older than ``core_calendar_refresh_hours``; the retry spacing
    (``core_retry_hours``) stops a failing source from being hammered every tick.
    """
    resolved = config or global_settings
    if state.latest_fetched_at is not None:
        if now - state.latest_fetched_at < timedelta(hours=resolved.core_calendar_refresh_hours):
            return False
    if state.last_attempt_at is not None:
        if now - state.last_attempt_at < timedelta(hours=resolved.core_retry_hours):
            return False
    return True


# --- database reads ---------------------------------------------------------


def _calendar_jobs(session: Session) -> list[FetchJob]:
    """Calendar sync jobs, newest first (origin is filtered in Python)."""
    jobs = session.scalars(
        sa.select(FetchJob).where(FetchJob.external_code == CALENDAR_CODE)
    ).all()
    tagged = [job for job in jobs if (job.attributes or {}).get("origin") == CALENDAR_ORIGIN]
    tagged.sort(key=lambda job: job.id or 0, reverse=True)
    return tagged


def _load_calendar_state(session: Session) -> CalendarState:
    rows = session.scalars(sa.select(ReleaseCalendar)).all()
    latest = max(
        (row.fetched_at for row in rows if row.fetched_at is not None),
        default=None,
    )
    jobs = _calendar_jobs(session)
    last = jobs[0] if jobs else None
    return CalendarState(
        latest_fetched_at=latest,
        last_attempt_at=(last.started_at or last.requested_at) if last is not None else None,
    )


def _calendar_jobs_all_failed(session: Session, threshold: int) -> bool:
    jobs = _calendar_jobs(session)[:threshold]
    return len(jobs) >= threshold and all(job.status == fetch_jobs.FAILED for job in jobs)


# --- calendar step ----------------------------------------------------------


def _open_calendar_failure_alert(
    session: Session,
    *,
    institution_id: int,
    exc: BaseException,
    now: datetime,
    config: Settings,
) -> list[str]:
    opened: list[str] = []
    if isinstance(exc, ConnectorError) and exc.kind == FORMAT_CHANGED:
        open_alert(
            session,
            institution_id=institution_id,
            scope=CALENDAR_SCOPE,
            kind=ALERT_FORMAT_CHANGED,
            message="Calendar source answer shape changed",
            detail={"kind": exc.kind, "scope": CALENDAR_SCOPE},
            now=now,
        )
        opened.append(ALERT_FORMAT_CHANGED)
    if _calendar_jobs_all_failed(session, config.core_failure_threshold):
        open_alert(
            session,
            institution_id=institution_id,
            scope=CALENDAR_SCOPE,
            kind=REPEATED_FAILURE,
            message=f"{config.core_failure_threshold} consecutive calendar syncs failed",
            detail={"scope": CALENDAR_SCOPE, "attempts": config.core_failure_threshold},
            now=now,
        )
        opened.append(REPEATED_FAILURE)
    return opened


def _run_calendar(
    session_factory: Callable[[], Session],
    calendar_sync: CalendarSync,
    *,
    now: datetime,
    clock: Clock,
    config: Settings,
) -> CalendarOutcome:
    with session_factory() as session:
        state = _load_calendar_state(session)
        if not calendar_sync_due(state, now, config=config):
            return CalendarOutcome(due=False, outcome="skipped")

        institution = find_institution(session, CALENDAR_INSTITUTION)
        if institution is None:
            return CalendarOutcome(
                due=True,
                outcome="failed",
                error=f"institution {CALENDAR_INSTITUTION!r} is not catalogued",
            )

        job = fetch_jobs.create_job(
            session,
            institution_id=institution.id,
            external_code=CALENDAR_CODE,
            attributes={"origin": CALENDAR_ORIGIN},
        )
        fetch_jobs.mark_fetching(session, job, now=clock())
        session.commit()
        job_id = job.id
        institution_id = institution.id

        # The fetch and the finalize share one failure path: whatever raises, the
        # job is moved off the active status (the partial unique index would
        # otherwise block every later calendar job) and the report says why.
        try:
            result = calendar_sync(session, now=now)
            fetch_jobs.mark_completed(session, job, now=clock())
            resolved: list[str] = []
            for kind in (REPEATED_FAILURE, ALERT_FORMAT_CHANGED):
                if resolve_alerts(
                    session,
                    institution_id=institution_id,
                    scope=CALENDAR_SCOPE,
                    kinds=[kind],
                    now=now,
                ):
                    resolved.append(kind)
            session.commit()
        except Exception as exc:  # noqa: BLE001 - every failure becomes a failed job/alert
            session.rollback()
            failed_job = session.get(FetchJob, job_id)
            fetch_jobs.mark_failed(session, failed_job, error_reason=_error_text(exc), now=clock())
            opened = _open_calendar_failure_alert(
                session,
                institution_id=institution_id,
                exc=exc,
                now=now,
                config=config,
            )
            session.commit()
            if isinstance(exc, ConnectorError):
                logger.warning("scheduler: calendar sync failed: %s", _error_text(exc))
            else:
                logger.exception("scheduler: calendar sync crashed")
            return CalendarOutcome(
                due=True, outcome="failed", error=_error_text(exc), alerts_opened=opened
            )

        logger.info(
            "scheduler: calendar sync inserted=%d updated=%d unchanged=%d",
            result.inserted,
            result.updated,
            result.unchanged,
        )
        return CalendarOutcome(
            due=True,
            outcome="completed",
            inserted=result.inserted,
            updated=result.updated,
            unchanged=result.unchanged,
            alerts_resolved=resolved,
        )


# --- tick -------------------------------------------------------------------


def run_tick(
    session_factory: Callable[[], Session],
    *,
    connector_for: Callable[[str], TcmbConnector],
    calendar_sync: CalendarSync,
    now: datetime | None = None,
    clock: Clock | None = None,
    config: Settings | None = None,
) -> TickReport:
    """One pass: calendar sync when due, then ``refresh_core``; never raises.

    The same ``now`` drives the decisions; ``clock`` stamps the ``fetch_jobs``
    rows (default: the real clock, so a live job's times actually advance).
    """
    resolved = config or global_settings
    moment = now or _real_clock()
    real_clock = clock or _real_clock

    calendar = CalendarOutcome()
    error: str | None = None
    try:
        calendar = _run_calendar(
            session_factory, calendar_sync, now=moment, clock=real_clock, config=resolved
        )
    except Exception as exc:  # noqa: BLE001 - recorded in the report, refresh still runs
        error = _error_text(exc)
        logger.exception("scheduler: calendar step crashed")

    refresh_report: RefreshReport | None = None
    try:
        refresh_report = refresh_core(
            session_factory,
            connector_for,
            now=moment,
            clock=real_clock,
            config=resolved,
        )
    except Exception as exc:  # noqa: BLE001 - recorded in the report as the tick error
        error = _error_text(exc)
        logger.exception("scheduler: refresh step crashed")

    return TickReport(calendar=calendar, refresh=refresh_report, error=error)


# --- continuous loop --------------------------------------------------------


def write_heartbeat(path: str | Path, now: datetime) -> None:
    """Write ``now`` as an ISO timestamp; create the parent directory."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(now.isoformat(), encoding="utf-8")


def _install_signal_handlers(stop: threading.Event) -> None:
    def handler(signum: int, _frame: object) -> None:
        logger.info("scheduler: signal %s; finishing the current step", signum)
        stop.set()

    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):  # not the main thread / unsupported
            continue


def _log_summary(report: TickReport) -> None:
    refresh = report.refresh
    fetched = refresh.fetched if refresh is not None else 0
    failed = refresh.failed_count if refresh is not None else 0
    opened = (refresh.alerts_opened if refresh is not None else 0) + len(
        report.calendar.alerts_opened
    )
    resolved = (refresh.alerts_resolved if refresh is not None else 0) + len(
        report.calendar.alerts_resolved
    )
    logger.info(
        "scheduler: tick calendar=%s fetched=%d failed=%d alerts_opened=%d "
        "alerts_resolved=%d error=%s",
        report.calendar.outcome,
        fetched,
        failed,
        opened,
        resolved,
        report.error or "-",
    )


def run_forever(
    session_factory: Callable[[], Session],
    *,
    connector_for: Callable[[str], TcmbConnector],
    calendar_sync: CalendarSync,
    tick_minutes: int | None = None,
    heartbeat_path: str | Path | None = None,
    config: Settings | None = None,
    stop_event: threading.Event | None = None,
) -> int:
    """Loop ``run_tick`` every tick; return the process exit code.

    Each tick opens its own session (nothing is held across ticks). The heartbeat
    is written once at startup and after every tick. Five consecutive crashed
    ticks return 1; a stop signal returns 0 after the current step finishes.
    """
    resolved = config or global_settings
    minutes = tick_minutes if tick_minutes is not None else resolved.core_tick_minutes
    path = heartbeat_path if heartbeat_path is not None else resolved.core_heartbeat_path
    stop = stop_event if stop_event is not None else threading.Event()
    if stop_event is None:
        _install_signal_handlers(stop)

    write_heartbeat(path, _real_clock())
    consecutive_crashes = 0
    while not stop.is_set():
        crashed = False
        try:
            report = run_tick(
                session_factory,
                connector_for=connector_for,
                calendar_sync=calendar_sync,
                config=resolved,
            )
            _log_summary(report)
            crashed = report.error is not None
        except Exception:  # noqa: BLE001 - the loop survives; 5 in a row end it
            logger.exception("scheduler: tick crashed")
            crashed = True
        write_heartbeat(path, _real_clock())

        if crashed:
            consecutive_crashes += 1
            if consecutive_crashes >= CRASH_LIMIT:
                logger.error("scheduler: %d consecutive crashed ticks; exiting", CRASH_LIMIT)
                return 1
        else:
            consecutive_crashes = 0

        stop.wait(timeout=max(minutes, 0) * 60)

    logger.info("scheduler: stop requested; exiting")
    return 0


# --- CLI --------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """The scheduler argument parser (pure, importable without a database)."""
    parser = argparse.ArgumentParser(
        prog="app.core.scheduler",
        description="Core-series scheduler: calendar sync plus the release refresh.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run a single tick and exit (exit 1 when the tick failed)",
    )
    parser.add_argument(
        "--tick-minutes",
        type=int,
        default=None,
        metavar="N",
        help="minutes between ticks (default: CORE_TICK_MINUTES)",
    )
    return parser


def _print_report(report: TickReport) -> None:
    calendar = report.calendar
    print(
        f"scheduler: calendar due={calendar.due} outcome={calendar.outcome} "
        f"inserted={calendar.inserted} updated={calendar.updated} "
        f"unchanged={calendar.unchanged}"
    )
    refresh = report.refresh
    if refresh is not None:
        for item in refresh.series:
            opened = ",".join(item.alerts_opened) or "-"
            resolved = ",".join(item.alerts_resolved) or "-"
            print(
                f"scheduler: {item.external_code} action={item.action} "
                f"outcome={item.outcome} due={item.due} reason={item.reason} "
                f"inserted={item.inserted} opened={opened} resolved={resolved}"
            )
    if report.error:
        print(f"scheduler: error: {report.error}", file=sys.stderr)


def _once(
    session_factory: Callable[[], Session],
    *,
    connector_for: Callable[[str], TcmbConnector],
    calendar_sync: CalendarSync,
    now: datetime | None = None,
    clock: Clock | None = None,
    config: Settings | None = None,
) -> int:
    """Run one tick, print it, and return the exit code (0 ok / 1 failed)."""
    report = run_tick(
        session_factory,
        connector_for=connector_for,
        calendar_sync=calendar_sync,
        now=now,
        clock=clock,
        config=config,
    )
    _print_report(report)
    return 1 if report.failed else 0


def main(argv: list[str] | None = None) -> int:
    """Run the scheduler CLI. Returns a process exit code."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    from app.db.session import SessionLocal

    store = MinioObjectStore()
    evds = build_evds_calendar_client(store=store)
    tuik = build_tuik_calendar_client(store=store)
    connectors: dict[str, TcmbConnector] = {}

    def connector_for(source: str) -> TcmbConnector:
        connector = connectors.get(source)
        if connector is None:
            connector = build_connector(source, store=store)
            connectors[source] = connector
        return connector

    def calendar_sync(session: Session, *, now: datetime) -> CalendarSyncResult:
        return sync_calendar(session, now=now, evds_client=evds, tuik_fetch=tuik.fetch)

    try:
        if args.once:
            return _once(SessionLocal, connector_for=connector_for, calendar_sync=calendar_sync)
        return run_forever(
            SessionLocal,
            connector_for=connector_for,
            calendar_sync=calendar_sync,
            tick_minutes=args.tick_minutes,
        )
    finally:
        evds.close()
        tuik.close()
        for connector in connectors.values():
            connector.close()


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CALENDAR_CODE",
    "CALENDAR_INSTITUTION",
    "CALENDAR_ORIGIN",
    "CALENDAR_SCOPE",
    "CRASH_LIMIT",
    "CalendarOutcome",
    "CalendarState",
    "TickReport",
    "build_parser",
    "calendar_sync_due",
    "main",
    "run_forever",
    "run_tick",
    "write_heartbeat",
]
