"""Unit tests for the core scheduler (no database).

``run_tick`` is exercised with a hand-rolled in-memory session that understands
the small set of ``SELECT`` shapes the scheduler builds (the same style as
``test_core_alerts``): real model instances live in lists, so job bookkeeping and
alert idempotency are pinned without a database. ``refresh_core`` is stubbed:
this file tests the scheduler's orchestration, not the refresh engine.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

from sqlalchemy.sql.elements import BooleanClauseList, UnaryExpression

from app.config import Settings
from app.connectors.base import FORMAT_CHANGED, ConnectorError
from app.core import alerts as alert_mod
from app.core import scheduler
from app.core.calendar import CalendarSyncResult
from app.core.refresh import RefreshReport, SeriesReport
from app.data import fetch_jobs
from app.data.models import DataAlert, FetchJob, Institution, ReleaseCalendar

NO_ENV = Settings(_env_file=None)
BASE = datetime(2026, 10, 3, 12, tzinfo=UTC)


# --- minimal session double -------------------------------------------------


def _flatten(criteria):
    for crit in criteria:
        if isinstance(crit, BooleanClauseList):
            yield from _flatten(crit.clauses)
        else:
            yield crit


def _matches(obj, statement) -> bool:
    for expr in _flatten(statement._where_criteria):
        if isinstance(expr, UnaryExpression):
            raise AssertionError(f"unsupported predicate {expr!r}")
        column = expr.left.name
        operator = expr.operator
        right = expr.right
        if operator.__name__ == "in_op":
            if getattr(obj, column) not in set(right.value):
                return False
        elif operator.__name__ == "eq":
            if getattr(obj, column) != right.value:
                return False
        else:
            raise AssertionError(f"unsupported operator {operator!r}")
    return True


class _FakeResult:
    def __init__(self, rows) -> None:
        self._rows = rows

    def all(self):
        return self._rows

    def __iter__(self):
        return iter(self._rows)


class FakeSession:
    """In-memory session: add/flush/commit/scalar/scalars over real model rows."""

    def __init__(self, *, institutions=(), calendars=(), jobs=(), alerts=()) -> None:
        self.institutions = list(institutions)
        self.calendars = list(calendars)
        self.jobs = list(jobs)
        self.alerts = list(alerts)
        self.commits = 0
        self.rollbacks = 0
        self._next_id = 1

    def _store(self):
        return {
            Institution: self.institutions,
            ReleaseCalendar: self.calendars,
            FetchJob: self.jobs,
            DataAlert: self.alerts,
        }

    def add(self, obj) -> None:
        if getattr(obj, "id", None) is None:
            obj.id = self._next_id
            self._next_id += 1
        self._store()[type(obj)].append(obj)

    def flush(self) -> None:
        return None

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def get(self, entity, ident):
        for row in self._store()[entity]:
            if row.id == ident:
                return row
        return None

    @staticmethod
    def _entity(statement):
        return statement.column_descriptions[0]["entity"]

    def scalar(self, statement):
        rows = [r for r in self._store()[self._entity(statement)] if _matches(r, statement)]
        return rows[0] if rows else None

    def scalars(self, statement):
        rows = [r for r in self._store()[self._entity(statement)] if _matches(r, statement)]
        return _FakeResult(rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _tuik_institution() -> Institution:
    return Institution(id=1, code="tuik", name="Türkiye İstatistik Kurumu")


def _fresh_calendar() -> ReleaseCalendar:
    return ReleaseCalendar(
        source="evds3",
        key="bie_cli2",
        expected_on=BASE.date(),
        fetched_at=BASE - timedelta(hours=1),
    )


class FakeCalendarSync:
    def __init__(self, *, error: BaseException | None = None) -> None:
        self.error = error
        self.calls = 0

    def __call__(self, session, *, now):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return CalendarSyncResult(2, 1, 0, ("evds3", "tuik"))


def _stub_refresh(monkeypatch, *, calls: list | None = None, failed: bool = False):
    def fake(session_factory, connector_for, *, now=None, clock=None, config=None):
        if calls is not None:
            calls.append((now, clock))
        outcome = "failed" if failed else "skipped"
        return RefreshReport(
            [SeriesReport("bie_cli2:TP.CLI2.A01", "skip", outcome, False, "not_due")]
        )

    monkeypatch.setattr(scheduler, "refresh_core", fake)


def _factory(session: FakeSession):
    return lambda: session


# --- pure due rule ----------------------------------------------------------


def test_calendar_due_when_table_empty() -> None:
    assert scheduler.calendar_sync_due(scheduler.CalendarState(), BASE, config=NO_ENV) is True


def test_calendar_due_when_stale() -> None:
    state = scheduler.CalendarState(latest_fetched_at=BASE - timedelta(hours=25))
    assert scheduler.calendar_sync_due(state, BASE, config=NO_ENV) is True


def test_calendar_not_due_when_fresh() -> None:
    state = scheduler.CalendarState(latest_fetched_at=BASE - timedelta(hours=1))
    assert scheduler.calendar_sync_due(state, BASE, config=NO_ENV) is False


def test_calendar_retry_spacing_blocks_a_stale_calendar() -> None:
    stale = BASE - timedelta(hours=48)
    recent = scheduler.CalendarState(last_attempt_at=BASE - timedelta(hours=1))
    old = scheduler.CalendarState(last_attempt_at=BASE - timedelta(hours=3))
    assert scheduler.calendar_sync_due(
        scheduler.CalendarState(stale, recent.last_attempt_at), BASE, config=NO_ENV
    ) is False
    assert scheduler.calendar_sync_due(
        scheduler.CalendarState(stale, old.last_attempt_at), BASE, config=NO_ENV
    ) is True


# --- run_tick ---------------------------------------------------------------


def test_run_tick_skips_calendar_when_fresh_and_still_refreshes(monkeypatch) -> None:
    session = FakeSession(calendars=[_fresh_calendar()])
    refresh_calls: list = []
    _stub_refresh(monkeypatch, calls=refresh_calls)
    sync = FakeCalendarSync()

    report = scheduler.run_tick(
        _factory(session), connector_for=lambda source: object(), calendar_sync=sync,
        now=BASE, clock=lambda: BASE, config=NO_ENV,
    )

    assert report.calendar.outcome == "skipped"
    assert sync.calls == 0
    assert session.jobs == []
    assert len(refresh_calls) == 1


def test_run_tick_records_a_completed_calendar_job(monkeypatch) -> None:
    session = FakeSession(institutions=[_tuik_institution()])
    _stub_refresh(monkeypatch)
    sync = FakeCalendarSync()

    report = scheduler.run_tick(
        _factory(session), connector_for=lambda source: object(), calendar_sync=sync,
        now=BASE, clock=lambda: BASE, config=NO_ENV,
    )

    assert report.calendar.outcome == "completed"
    assert report.calendar.inserted == 2
    assert len(session.jobs) == 1
    job = session.jobs[0]
    assert job.status == fetch_jobs.COMPLETED
    assert job.attributes == {"origin": scheduler.CALENDAR_ORIGIN}
    assert job.external_code == scheduler.CALENDAR_CODE


def test_calendar_job_times_use_the_clock_not_the_decision_now(monkeypatch) -> None:
    session = FakeSession(institutions=[_tuik_institution()])
    _stub_refresh(monkeypatch)
    decision_now = datetime(2030, 1, 1, tzinfo=UTC)
    first = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
    second = datetime(2030, 1, 1, 12, 5, tzinfo=UTC)
    times = iter([first, second])

    scheduler.run_tick(
        _factory(session), connector_for=lambda source: object(),
        calendar_sync=FakeCalendarSync(), now=decision_now, clock=lambda: next(times),
        config=NO_ENV,
    )

    job = session.jobs[0]
    assert job.started_at == first
    assert job.finished_at == second
    assert job.started_at != decision_now
    assert job.finished_at >= job.started_at


def test_calendar_failure_records_failed_job_and_refresh_still_runs(monkeypatch) -> None:
    session = FakeSession(institutions=[_tuik_institution()])
    refresh_calls: list = []
    _stub_refresh(monkeypatch, calls=refresh_calls)
    sync = FakeCalendarSync(error=RuntimeError("boom"))

    report = scheduler.run_tick(
        _factory(session), connector_for=lambda source: object(), calendar_sync=sync,
        now=BASE, clock=lambda: BASE, config=NO_ENV,
    )

    assert report.calendar.outcome == "failed"
    assert report.failed is True
    assert session.jobs[0].status == fetch_jobs.FAILED
    assert "boom" in (session.jobs[0].error_reason or "")
    assert sync.calls == 1
    assert len(refresh_calls) == 1


def test_format_changed_opens_an_immediate_alert(monkeypatch) -> None:
    session = FakeSession(institutions=[_tuik_institution()])
    _stub_refresh(monkeypatch)
    error = ConnectorError(FORMAT_CHANGED, "shape changed")

    report = scheduler.run_tick(
        _factory(session), connector_for=lambda source: object(),
        calendar_sync=FakeCalendarSync(error=error), now=BASE, clock=lambda: BASE,
        config=NO_ENV,
    )

    assert alert_mod.FORMAT_CHANGED in report.calendar.alerts_opened
    assert session.jobs[0].status == fetch_jobs.FAILED
    open_alerts = [row for row in session.alerts if row.status == "open"]
    assert [row.kind for row in open_alerts] == [alert_mod.FORMAT_CHANGED]


def test_three_failed_calendar_jobs_open_one_repeated_failure(monkeypatch) -> None:
    session = FakeSession(institutions=[_tuik_institution()])
    _stub_refresh(monkeypatch)
    sync = FakeCalendarSync(error=RuntimeError("boom"))

    for index in range(3):
        moment = BASE + timedelta(hours=3 * index)
        scheduler.run_tick(
            _factory(session), connector_for=lambda source: object(), calendar_sync=sync,
            now=moment, clock=lambda moment=moment: moment, config=NO_ENV,
        )

    assert sync.calls == 3
    open_alerts = [row for row in session.alerts if row.status == "open"]
    repeated = [row for row in open_alerts if row.kind == alert_mod.REPEATED_FAILURE]
    assert len(repeated) == 1


def test_calendar_success_resolves_both_alerts(monkeypatch) -> None:
    session = FakeSession(institutions=[_tuik_institution()])
    _stub_refresh(monkeypatch)
    alert_mod.open_alert(
        session, institution_id=1, scope=scheduler.CALENDAR_SCOPE,
        kind=alert_mod.FORMAT_CHANGED, message="old", now=BASE,
    )
    alert_mod.open_alert(
        session, institution_id=1, scope=scheduler.CALENDAR_SCOPE,
        kind=alert_mod.REPEATED_FAILURE, message="old", now=BASE,
    )

    report = scheduler.run_tick(
        _factory(session), connector_for=lambda source: object(), calendar_sync=FakeCalendarSync(),
        now=BASE, clock=lambda: BASE, config=NO_ENV,
    )

    assert set(report.calendar.alerts_resolved) == {
        alert_mod.FORMAT_CHANGED,
        alert_mod.REPEATED_FAILURE,
    }
    assert all(row.status == alert_mod.RESOLVED for row in session.alerts)


def test_missing_calendar_institution_is_recorded_not_raised(monkeypatch) -> None:
    session = FakeSession()
    _stub_refresh(monkeypatch)

    report = scheduler.run_tick(
        _factory(session), connector_for=lambda source: object(), calendar_sync=FakeCalendarSync(),
        now=BASE, clock=lambda: BASE, config=NO_ENV,
    )

    assert report.calendar.failed is True
    assert "tuik" in (report.calendar.error or "")
    assert session.jobs == []


def test_refresh_exception_is_recorded_and_marks_the_tick_failed(monkeypatch) -> None:
    session = FakeSession()

    def boom(*args, **kwargs):
        raise RuntimeError("refresh exploded")

    monkeypatch.setattr(scheduler, "refresh_core", boom)
    report = scheduler.run_tick(
        _factory(session), connector_for=lambda source: object(), calendar_sync=FakeCalendarSync(),
        now=BASE, clock=lambda: BASE, config=NO_ENV,
    )

    assert report.refresh is None
    assert report.error is not None and "refresh exploded" in report.error
    assert report.failed is True


# --- run_forever ------------------------------------------------------------


def test_stop_flag_ends_run_forever(monkeypatch, tmp_path) -> None:
    stop = threading.Event()
    session = FakeSession()

    def once(*args, **kwargs):
        stop.set()
        return scheduler.TickReport(scheduler.CalendarOutcome(outcome="skipped"))

    monkeypatch.setattr(scheduler, "run_tick", once)
    code = scheduler.run_forever(
        _factory(session), connector_for=lambda source: object(), calendar_sync=FakeCalendarSync(),
        tick_minutes=0, heartbeat_path=tmp_path / "hb", stop_event=stop, config=NO_ENV,
    )

    assert code == 0
    assert (tmp_path / "hb").exists()


def test_five_consecutive_crashed_ticks_exit_nonzero(monkeypatch, tmp_path) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("x")

    monkeypatch.setattr(scheduler, "run_tick", boom)
    code = scheduler.run_forever(
        _factory(FakeSession()), connector_for=lambda source: object(),
        calendar_sync=FakeCalendarSync(), tick_minutes=0, heartbeat_path=tmp_path / "hb",
        stop_event=threading.Event(), config=NO_ENV,
    )

    assert code == 1
    assert (tmp_path / "hb").exists()


def test_heartbeat_is_written_under_a_new_parent(tmp_path) -> None:
    target = tmp_path / "nested" / "core.heartbeat"
    scheduler.write_heartbeat(target, BASE)
    assert target.read_text(encoding="utf-8") == BASE.isoformat()


# --- settings and CLI -------------------------------------------------------


def test_scheduler_settings_defaults() -> None:
    settings = Settings(_env_file=None)
    assert settings.core_tick_minutes == 15
    assert settings.core_calendar_refresh_hours == 24
    assert settings.core_heartbeat_path == "/tmp/core-scheduler.heartbeat"


def test_build_parser_once_and_tick_minutes() -> None:
    args = scheduler.build_parser().parse_args(["--once", "--tick-minutes", "5"])
    assert args.once is True
    assert args.tick_minutes == 5
    defaults = scheduler.build_parser().parse_args([])
    assert defaults.once is False
    assert defaults.tick_minutes is None


def test_once_exit_codes(monkeypatch, capsys) -> None:
    _stub_refresh(monkeypatch)
    good = scheduler._once(
        _factory(FakeSession(calendars=[_fresh_calendar()])),
        connector_for=lambda source: object(),
        calendar_sync=FakeCalendarSync(), now=BASE, clock=lambda: BASE, config=NO_ENV,
    )
    assert good == 0

    monkeypatch.setattr(
        scheduler, "refresh_core",
        lambda *a, **k: RefreshReport([SeriesReport("x", "fetch", "failed", True, "release_due")]),
    )
    bad = scheduler._once(
        _factory(FakeSession(institutions=[_tuik_institution()])),
        connector_for=lambda source: object(),
        calendar_sync=FakeCalendarSync(error=RuntimeError("x")),
        now=BASE, clock=lambda: BASE, config=NO_ENV,
    )
    assert bad == 1
    capsys.readouterr()
