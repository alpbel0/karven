"""Core refresh: the pure release-date decision plus the writing engine (1.4c).

``decide`` is pure: it takes a :class:`SeriesState`, the relevant calendar rows
and an injected ``now`` (timezone-aware) and returns a :class:`Decision`. It is
unit-tested with fixed clocks and never touches the database.

``refresh_core`` is the engine: for every ``is_core`` registry series it builds
the state, asks ``decide``, opens a ``fetch_jobs`` row and calls
``ingest_series`` (full history each time) when due, then records the outcome and
opens/resolves the matching ``data_alerts`` rows. It commits once per series and
keeps going after a failure.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.config import Settings
from app.config import settings as global_settings
from app.connectors.base import (
    FORMAT_CHANGED,
    ConnectorError,
    SourceConnector,
    ingest_series,
)
from app.connectors.tcmb.connector import SERIE_DIMENSION, SOURCES
from app.core.alerts import (
    FORMAT_CHANGED as ALERT_FORMAT_CHANGED,
)
from app.core.alerts import (
    NO_NEW_PERIOD,
    REPEATED_FAILURE,
    open_alert,
    resolve_alerts,
)
from app.core.calendar import CALENDAR_KEYS
from app.core.loader import find_dataset, find_institution
from app.core.registry import CORE_SERIES, CoreSeries
from app.data import fetch_jobs
from app.data.models import Observation, ReleaseCalendar, Series

logger = logging.getLogger(__name__)

ISTANBUL = ZoneInfo("Europe/Istanbul")
ORIGIN = "core_refresh"
CORE_START = date(2000, 1, 1)

#: The job-time clock: decisions use the injected ``now`` (a fixed instant in
#: tests), but ``mark_*`` must advance in real time, otherwise a live job shows
#: ``started_at == finished_at == heartbeat_at``.
Clock = Callable[[], datetime]


def _real_clock() -> datetime:
    return datetime.now(UTC)

#: Nominal length of one period, used only for the "calendar looked stale" rule.
FREQUENCY_DAYS = {
    "daily": 1,
    "weekly": 7,
    "monthly": 31,
    "quarterly": 92,
    "semiannual": 183,
    "annual": 366,
    "biennial": 731,
    "irregular": 366,
}


@dataclass(frozen=True)
class CalendarRow:
    """The decision-relevant slice of one ``release_calendar`` row."""

    expected_on: date
    expected_at: str | None = None
    period_label: str | None = None


@dataclass(frozen=True)
class SeriesState:
    """Everything ``decide`` needs about one series, with no DB access.

    ``baselines`` maps an ``expected_on`` to the series' max stored period among
    observations fetched strictly before that date's 00:00 Istanbul. It answers
    "what did we already have when the release was due".
    """

    external_code: str
    frequency: str
    has_calendar: bool
    max_period: date | None
    baselines: tuple[tuple[date, date | None], ...] = ()
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None

    def baseline_for(self, expected_on: date) -> date | None:
        for day, baseline in self.baselines:
            if day == expected_on:
                return baseline
        return None


@dataclass(frozen=True)
class Decision:
    """The pure outcome for one series at one ``now``."""

    external_code: str
    due: bool
    reason: str
    alert_no_new_period: bool


@dataclass(frozen=True)
class SeriesReport:
    """What the engine did for one series."""

    external_code: str
    action: str
    outcome: str
    due: bool
    reason: str
    inserted: int = 0
    alerts_opened: list[str] = field(default_factory=list)
    alerts_resolved: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass(frozen=True)
class RefreshReport:
    """The whole run's outcome, one entry per series."""

    series: list[SeriesReport] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return any(item.outcome == "failed" for item in self.series)

    @property
    def failed_count(self) -> int:
        return sum(1 for item in self.series if item.outcome == "failed")

    @property
    def fetched(self) -> int:
        return sum(1 for item in self.series if item.action == "fetch")

    @property
    def alerts_opened(self) -> int:
        return sum(len(item.alerts_opened) for item in self.series)

    @property
    def alerts_resolved(self) -> int:
        return sum(len(item.alerts_resolved) for item in self.series)


# --- pure decision ----------------------------------------------------------


def _istanbul_date(now: datetime) -> date:
    return now.astimezone(ISTANBUL).date()


def _after_daily_cutoff(now: datetime, after: str) -> bool:
    local = now.astimezone(ISTANBUL)
    hour, _, minute = after.partition(":")
    cutoff = time(int(hour), int(minute or 0))
    return local.timetz().replace(tzinfo=None) >= cutoff


def _is_weekday(day: date) -> bool:
    return day.weekday() < 5


def _satisfied(state: SeriesState, expected_on: date) -> bool:
    baseline = state.baseline_for(expected_on)
    if state.max_period is None:
        return False
    if baseline is None:
        return True
    return state.max_period > baseline


def _calendar_stale(state: SeriesState, rows: list[CalendarRow], today: date) -> bool:
    """True when no future row exists and the last row is older than the frequency."""
    has_future = any(row.expected_on > today for row in rows)
    if has_future:
        return False
    if not rows:
        return True
    last = max(row.expected_on for row in rows)
    length = FREQUENCY_DAYS.get(state.frequency, 31)
    return (today - last) > timedelta(days=length)


def _retry_ready(state: SeriesState, now: datetime, retry_hours: int) -> bool:
    if state.last_attempt_at is None:
        return True
    return now - state.last_attempt_at >= timedelta(hours=retry_hours)


def decide(
    state: SeriesState,
    calendar_rows: list[CalendarRow],
    now: datetime,
    *,
    config: Settings | None = None,
) -> Decision:
    """Decide whether ``state`` is due and whether to alert; pure and clock-injected."""
    resolved = config or global_settings
    today = _istanbul_date(now)

    if not state.has_calendar:
        return _decide_daily(state, today, now, resolved)

    if _calendar_stale(state, calendar_rows, today):
        # Missing or stale calendar: one poll per day, and never an alert on the
        # date (a calendar we cannot trust must not accuse the source).
        return _decide_fallback(state, today, now, resolved)

    relevant = [row for row in calendar_rows if row.expected_on <= today]
    if not relevant:
        return Decision(state.external_code, False, "not_due", False)

    row = max(relevant, key=lambda item: item.expected_on)
    if _satisfied(state, row.expected_on):
        return Decision(state.external_code, False, "satisfied", False)

    deadline = datetime.combine(
        row.expected_on + timedelta(days=resolved.core_grace_days), time.min, tzinfo=ISTANBUL
    )
    alert = now >= deadline
    if now < datetime.combine(row.expected_on, time.min, tzinfo=ISTANBUL):
        return Decision(state.external_code, False, "not_due", alert)
    if not _retry_ready(state, now, resolved.core_retry_hours):
        return Decision(state.external_code, False, "waiting_retry", alert)
    return Decision(state.external_code, True, "release_due", alert)


def _decide_daily(
    state: SeriesState, today: date, now: datetime, config: Settings
) -> Decision:
    stale = (
        state.max_period is not None
        and (today - state.max_period) > timedelta(days=config.core_daily_stale_days)
    )
    if not _is_weekday(today):
        return Decision(state.external_code, False, "weekend", stale)
    if not _after_daily_cutoff(now, config.core_daily_after):
        return Decision(state.external_code, False, "before_cutoff", stale)
    if _refreshed_today(state, today):
        return Decision(state.external_code, False, "already_refreshed", stale)
    if not _retry_ready(state, now, config.core_retry_hours):
        return Decision(state.external_code, False, "waiting_retry", stale)
    return Decision(state.external_code, True, "daily_due", stale)


def _refreshed_today(state: SeriesState, today: date) -> bool:
    return (
        state.last_success_at is not None
        and state.last_success_at.astimezone(ISTANBUL).date() == today
    )


def _decide_fallback(
    state: SeriesState, today: date, now: datetime, config: Settings
) -> Decision:
    """Missing/stale calendar: one poll per day, never an alert on the date."""
    if _refreshed_today(state, today):
        return Decision(state.external_code, False, "calendar_fallback", False)
    if not _retry_ready(state, now, config.core_retry_hours):
        return Decision(state.external_code, False, "waiting_retry", False)
    return Decision(state.external_code, True, "calendar_fallback", False)


# --- state building ---------------------------------------------------------


def _institution_code(core: CoreSeries) -> str:
    return SOURCES[core.source].institution_code


def _calendar_pair(core: CoreSeries) -> tuple[str, str] | None:
    return CALENDAR_KEYS.get(core.external_code)


def _load_calendar_rows(
    session: Session, core: CoreSeries
) -> list[CalendarRow]:
    pair = _calendar_pair(core)
    if pair is None:
        return []
    source, key = pair
    rows = session.scalars(
        sa.select(ReleaseCalendar)
        .where(ReleaseCalendar.source == source, ReleaseCalendar.key == key)
        .order_by(ReleaseCalendar.expected_on)
    ).all()
    return [
        CalendarRow(row.expected_on, row.expected_at, row.period_label) for row in rows
    ]


def _baseline(session: Session, series_id: int, expected_on: date) -> date | None:
    threshold = datetime.combine(expected_on, time.min, tzinfo=ISTANBUL).astimezone(UTC)
    return session.scalar(
        sa.select(sa.func.max(Observation.period)).where(
            Observation.series_id == series_id,
            Observation.fetched_at < threshold,
        )
    )


def build_state(
    session: Session, core: CoreSeries, rows: list[CalendarRow], *, institution_id: int | None
) -> SeriesState:
    """Read one series' current state (max period, baselines, last attempts)."""
    series = None
    if institution_id is not None:
        series = session.scalar(
            sa.select(Series).where(
                Series.institution_id == institution_id,
                Series.external_code == core.external_code,
            )
        )
    if series is None:
        return SeriesState(
            external_code=core.external_code,
            frequency="monthly",
            has_calendar=_calendar_pair(core) is not None,
        )

    max_period = session.scalar(
        sa.select(sa.func.max(Observation.period)).where(Observation.series_id == series.id)
    )
    baselines = tuple(
        (row.expected_on, _baseline(session, series.id, row.expected_on)) for row in rows
    )
    last_attempt, last_success = _last_job_times(session, core.external_code)
    return SeriesState(
        external_code=core.external_code,
        frequency=series.frequency,
        has_calendar=_calendar_pair(core) is not None,
        max_period=max_period,
        baselines=baselines,
        last_attempt_at=last_attempt,
        last_success_at=last_success,
    )


def _last_job_times(
    session: Session, external_code: str
) -> tuple[datetime | None, datetime | None]:
    """(last core_refresh attempt, last successful one) for the series."""
    statement = sa.select(fetch_jobs.FetchJob).where(
        fetch_jobs.FetchJob.external_code == external_code,
        fetch_jobs.FetchJob.attributes["origin"].astext == ORIGIN,
    )
    jobs = session.scalars(statement.order_by(fetch_jobs.FetchJob.id.desc())).all()
    last_attempt = (jobs[0].started_at or jobs[0].requested_at) if jobs else None
    last_success = next(
        (job.finished_at or job.requested_at for job in jobs if job.status == fetch_jobs.COMPLETED),
        None,
    )
    return last_attempt, last_success


def _recent_jobs_all_failed(
    session: Session, external_code: str, threshold: int
) -> bool:
    jobs = session.scalars(
        sa.select(fetch_jobs.FetchJob)
        .where(
            fetch_jobs.FetchJob.external_code == external_code,
            fetch_jobs.FetchJob.attributes["origin"].astext == ORIGIN,
        )
        .order_by(fetch_jobs.FetchJob.id.desc())
        .limit(threshold)
    ).all()
    return len(jobs) >= threshold and all(job.status == fetch_jobs.FAILED for job in jobs)


# --- engine -----------------------------------------------------------------


ConnectorFor = Callable[[str], SourceConnector]


def _handle_alerts(
    session: Session,
    core: CoreSeries,
    decision: Decision,
    *,
    institution_id: int,
    series_id: int | None,
    now: datetime,
) -> tuple[list[str], list[str]]:
    """Open/resolve the date-based ``no_new_period`` alert for one decision."""
    if decision.alert_no_new_period:
        open_alert(
            session,
            institution_id=institution_id,
            scope=core.external_code,
            kind=NO_NEW_PERIOD,
            message="Release date passed and no new period was stored",
            detail={"external_code": core.external_code, "reason": decision.reason},
            series_id=series_id,
            now=now,
        )
        return [NO_NEW_PERIOD], []
    resolved = resolve_alerts(
        session,
        institution_id=institution_id,
        scope=core.external_code,
        kinds=[NO_NEW_PERIOD],
        now=now,
    )
    return [], ([NO_NEW_PERIOD] if resolved else [])


def _process_series(
    session: Session,
    core: CoreSeries,
    connector: SourceConnector,
    decision: Decision,
    *,
    force: bool,
    dry_run: bool,
    now: datetime,
    clock: Clock,
    config: Settings,
    institution_id: int | None,
    series_id: int | None,
) -> SeriesReport:
    should_fetch = decision.due or force
    opened: list[str] = []
    resolved: list[str] = []

    if dry_run:
        if not should_fetch:
            return SeriesReport(
                core.external_code, "skip", "dry_run", decision.due, decision.reason
            )
        # Evaluate the date alert even in dry-run so the CLI reports it.
        if decision.alert_no_new_period:
            opened.append(NO_NEW_PERIOD)
        return SeriesReport(
            core.external_code, "fetch", "dry_run", decision.due, decision.reason,
            alerts_opened=opened,
        )

    if institution_id is None and should_fetch:
        return SeriesReport(
            core.external_code,
            "skip",
            "failed",
            decision.due,
            decision.reason,
            error=f"institution {_institution_code(core)!r} is not catalogued",
        )

    job = None
    if should_fetch:
        document = find_dataset(session, _institution_code(core), core.dataset_code)
        if document is None:
            return SeriesReport(
                core.external_code,
                "skip",
                "failed",
                decision.due,
                decision.reason,
                error=f"dataset {core.dataset_code!r} is not catalogued",
            )

        job = fetch_jobs.create_job(
            session,
            institution_id=institution_id,
            external_code=core.external_code,
            series_id=series_id,
            attributes={"origin": ORIGIN},
        )
        fetch_jobs.mark_fetching(session, job, now=clock())
        session.commit()

        try:
            result = ingest_series(
                session,
                connector,
                dataset=document,
                codes={SERIE_DIMENSION: core.serie_code},
                start=CORE_START,
            )
        except ConnectorError as exc:
            fetch_jobs.mark_failed(
                session, job, error_reason=f"{exc.kind}: {exc.message}", now=clock()
            )
            if exc.kind == FORMAT_CHANGED:
                open_alert(
                    session,
                    institution_id=institution_id,
                    scope=core.external_code,
                    kind=ALERT_FORMAT_CHANGED,
                    message="Source answer shape changed",
                    detail={"external_code": core.external_code, "kind": exc.kind},
                    series_id=series_id,
                    now=now,
                )
                opened.append(ALERT_FORMAT_CHANGED)
            if _recent_jobs_all_failed(session, core.external_code, config.core_failure_threshold):
                open_alert(
                    session,
                    institution_id=institution_id,
                    scope=core.external_code,
                    kind=REPEATED_FAILURE,
                    message=f"{config.core_failure_threshold} consecutive refreshes failed",
                    detail={
                        "external_code": core.external_code,
                        "kind": exc.kind,
                        "attempts": config.core_failure_threshold,
                    },
                    series_id=series_id,
                    now=now,
                )
                opened.append(REPEATED_FAILURE)
            session.commit()
            return SeriesReport(
                core.external_code,
                "fetch",
                "failed",
                decision.due,
                decision.reason,
                alerts_opened=opened,
                error=f"{exc.kind}: {exc.message}",
            )
        except Exception as exc:  # noqa: BLE001 - recorded as a failed job, then continue
            fetch_jobs.mark_failed(session, job, error_reason=str(exc), now=clock())
            if _recent_jobs_all_failed(session, core.external_code, config.core_failure_threshold):
                open_alert(
                    session,
                    institution_id=institution_id,
                    scope=core.external_code,
                    kind=REPEATED_FAILURE,
                    message=f"{config.core_failure_threshold} consecutive refreshes failed",
                    detail={
                        "external_code": core.external_code,
                        "kind": "unexpected",
                        "attempts": config.core_failure_threshold,
                    },
                    series_id=series_id,
                    now=now,
                )
                opened.append(REPEATED_FAILURE)
            session.commit()
            return SeriesReport(
                core.external_code,
                "fetch",
                "failed",
                decision.due,
                decision.reason,
                alerts_opened=opened,
                error=str(exc),
            )

        fetch_jobs.mark_completed(session, job, now=clock())
        for kind in (REPEATED_FAILURE, ALERT_FORMAT_CHANGED):
            count = resolve_alerts(
                session,
                institution_id=institution_id,
                scope=core.external_code,
                kinds=[kind],
                now=now,
            )
            if count:
                resolved.append(kind)
        session.commit()

        # Re-read the state: the fetch may have satisfied the release.
        rows = _load_calendar_rows(session, core)
        state = build_state(session, core, rows, institution_id=institution_id)
        refreshed = decide(state, rows, now, config=config)
        date_opened, date_resolved = _handle_alerts(
            session,
            core,
            refreshed,
            institution_id=institution_id,
            series_id=result.series_id,
            now=now,
        )
        opened.extend(date_opened)
        resolved.extend(date_resolved)
        session.commit()
        return SeriesReport(
            core.external_code,
            "fetch",
            "completed",
            decision.due,
            decision.reason,
            inserted=result.inserted,
            alerts_opened=opened,
            alerts_resolved=resolved,
        )

    # Not fetched: still evaluate the date alert every run (only when the
    # institution exists — an alert row needs a non-null institution).
    if institution_id is not None:
        date_opened, date_resolved = _handle_alerts(
            session,
            core,
            decision,
            institution_id=institution_id,
            series_id=series_id,
            now=now,
        )
        opened.extend(date_opened)
        resolved.extend(date_resolved)
        session.commit()
    return SeriesReport(
        core.external_code,
        "skip",
        "skipped",
        decision.due,
        decision.reason,
        alerts_opened=opened,
        alerts_resolved=resolved,
    )


def refresh_core(
    session_factory: Callable[[], Session],
    connector_for: ConnectorFor,
    *,
    now: datetime | None = None,
    clock: Clock | None = None,
    only: str | None = None,
    force: bool = False,
    dry_run: bool = False,
    config: Settings | None = None,
) -> RefreshReport:
    """Refresh every core series whose release is due; return a per-series report.

    ``now`` drives the DECISIONS (and the alert timestamps) and is fixed in
    tests; ``clock`` stamps the ``fetch_jobs`` rows and defaults to a real clock
    so ``started_at``/``finished_at`` advance on a live run.
    """
    resolved = config or global_settings
    moment = now or datetime.now(UTC)
    real_clock = clock or _real_clock
    selected = list(CORE_SERIES)
    if only is not None:
        selected = [core for core in CORE_SERIES if core.external_code == only]
        if not selected:
            raise ValueError(f"{only!r} is not a core series")

    reports: list[SeriesReport] = []
    for core in selected:
        with session_factory() as session:
            institution_code = _institution_code(core)
            institution = find_institution(session, institution_code)
            institution_id = institution.id if institution else None
            rows = _load_calendar_rows(session, core)
            state = build_state(session, core, rows, institution_id=institution_id)
            decision = decide(state, rows, moment, config=resolved)
            series = None
            if institution_id is not None:
                series = session.scalar(
                    sa.select(Series).where(
                        Series.institution_id == institution_id,
                        Series.external_code == core.external_code,
                    )
                )
            series_id = series.id if series else None
            connector = connector_for(core.source)
            report = _process_series(
                session,
                core,
                connector,
                decision,
                force=force,
                dry_run=dry_run,
                now=moment,
                clock=real_clock,
                config=resolved,
                institution_id=institution_id,
                series_id=series_id,
            )
            reports.append(report)
    return RefreshReport(reports)


__all__ = [
    "CORE_START",
    "FREQUENCY_DAYS",
    "ISTANBUL",
    "ORIGIN",
    "CalendarRow",
    "Clock",
    "Decision",
    "RefreshReport",
    "SeriesReport",
    "SeriesState",
    "build_state",
    "decide",
    "refresh_core",
]
