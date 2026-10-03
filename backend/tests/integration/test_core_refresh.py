"""Integration tests for the release calendar, alerts and the refresh engine.

The EVDS/TÜİK calendar fetches and the series connectors are fakes, so no
network is touched, but ``sync_calendar``/``refresh_core`` run against the
isolated test database created by migration 0014. As in ``test_core_load`` the
shared database cannot be cleaned (the observations delete trigger), so every
assertion is order-independent: alerts are idempotent by (scope, kind), series
are identified by external code, and "nothing due" is checked as "no new job".

Unique alert scopes are used wherever the engine is not involved, so another
module's state cannot interfere. Every test that drives ``refresh_core`` owns a
distinct core series (see :data:`SERIES_FOR_TEST`): observations and
``fetch_jobs`` cannot be deleted, and both the repeated-failure check and the
open-alert uniqueness are keyed by the series/scope, so two tests sharing a
series would observe each other's history.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from app.connectors.base import (
    EMPTY,
    FORMAT_CHANGED,
    ConnectorError,
    DatasetMeta,
    DimensionCodeMeta,
    DimensionMeta,
    FetchResult,
    SourceConnector,
    sync_catalog,
)
from app.connectors.tcmb.connector import TcmbConnector
from app.core import alerts as alert_mod
from app.core.__main__ import _cmd_calendar_sync, _cmd_refresh, build_parser
from app.core.calendar import TUIK_GDP_KEY, sync_calendar
from app.core.refresh import ORIGIN, refresh_core
from app.core.registry import CORE_SERIES, CoreSeries
from app.data import fetch_jobs
from app.data.models import DataAlert, Institution, Observation, ReleaseCalendar, Series

pytestmark = pytest.mark.integration

FIXTURES_TCMB = Path(__file__).parent.parent / "fixtures" / "tcmb"
FIXTURES_CALENDAR = Path(__file__).parent.parent / "fixtures" / "calendar"
GDP_GROUP = "bie_gsyhuretcar"


# --- fakes ------------------------------------------------------------------


class _Resp:
    def __init__(self, payload, key: str | None = None) -> None:
        self._payload = payload
        self.raw_object_key = key

    def json(self):
        return self._payload


def _evds_payload(key: str, day: str) -> list[dict]:
    return [
        {
            "id": "x",
            "yayin_id": 1,
            "rip": "R",
            "period": "Aylık",
            "donem": "Eylül 2026",
            "tarih": f"{day} 23:00",
            "arsiv": 0,
            "yayinBilgi": {"veriGrubuKodu": key, "yayimAdi": "Fake"},
        },
        {"id": "y", "yayinBilgi": {"veriGrubuKodu": None}},
        {"id": "z", "yayinBilgi": {"veriGrubuKodu": "bie_notcore"}, "tarih": f"{day} 10:00"},
    ]


class FakeEvdsCalendarClient:
    """Duck-typed ``get_calendar`` for the EVDS3 monthly calendar."""

    def __init__(self, *, day: str = "2026-10-30", key: str = "bie_cli2") -> None:
        self._day = day
        self._key = key
        self.calls = 0

    def get_calendar(self, year: int, month: int) -> _Resp:
        self.calls += 1
        return _Resp(_evds_payload(self._key, self._day))


def _tuik_payload() -> dict:
    gdp = {
        "sorumluKisaAd": "TÜİK",
        "gTarih": "2026-12-01T10:00:00",
        "donemi": "III. Çeyrek",
        "adi": "Dönemsel Gayrisafi Yurt İçi Hasıla",
        "id": 0,
    }
    other = {**gdp, "sorumluKisaAd": "SPK", "gTarih": "2026-12-05T10:00:00"}
    return {"yayindaOlanlarList": [other], "yayindaOlmayanlarList": [gdp]}


def _tuik_fetch(year: int) -> _Resp:
    return _Resp(_tuik_payload())


def _load_calendar_fixture(name: str):
    return json.loads((FIXTURES_CALENDAR / name).read_text(encoding="utf-8"))


class _FixtureEvdsCalendar:
    """Duck-typed EVDS calendar client serving the trimmed 2026-10 fixture."""

    def __init__(self) -> None:
        self._payload = _load_calendar_fixture("evds-2026-10.trimmed.json")

    def get_calendar(self, year: int, month: int) -> _Resp:
        return _Resp(self._payload)

    def close(self) -> None:
        return None


class _FixtureTuikCalendar:
    """Duck-typed TÜİK calendar client serving the trimmed 2026 fixture."""

    def __init__(self) -> None:
        self._payload = _load_calendar_fixture("tuik-2026.trimmed.json")

    def fetch(self, year: int) -> _Resp:
        return _Resp(self._payload)

    def close(self) -> None:
        return None


class FakeConnector(SourceConnector):
    """A connector serving one dataset, optionally always failing."""

    institution_code = "tcmb"
    institution_name = "Fake TCMB"
    channel = "evds3-fake"

    def __init__(
        self,
        *,
        dataset_code: str,
        frequency: str,
        serie_code: str,
        points: list[tuple[date, Decimal]] | None = None,
        failure: ConnectorError | None = None,
        institution_code: str = "tcmb",
    ) -> None:
        self.institution_code = institution_code
        self._dataset_code = dataset_code
        self._frequency = frequency
        self._serie_code = serie_code
        self._points = points or []
        self.failure = failure
        self.fetch_calls = 0

    def list_datasets(self):
        yield DatasetMeta(
            external_code=self._dataset_code,
            name=f"Fake {self._dataset_code}",
            attributes={"default_frequency": self._frequency, "channel": self.channel},
            dimensions=[
                DimensionMeta(
                    code="SERIE",
                    label="Series",
                    position=0,
                    role="other",
                    codes=[DimensionCodeMeta(self._serie_code, self._serie_code)],
                )
            ],
        )

    def fetch_series(self, dataset_code, codes, *, order=None, start=date(2000, 1, 1)):
        self.fetch_calls += 1
        if self.failure is not None:
            raise self.failure
        points = [point for point in self._points if point[0] >= start]
        return FetchResult(
            external_code=f"{dataset_code}:{codes.get('SERIE', self._serie_code)}",
            points=points,
            raw_object_keys=[],
            channel=self.channel,
        )


def _catalog(session, connector: FakeConnector) -> None:
    sync_catalog(session, connector)
    session.commit()


def _connector_for(connectors: dict[str, SourceConnector]):
    def factory(source: str) -> SourceConnector:
        return connectors[source]

    return factory


# --- migration / schema -----------------------------------------------------


def test_new_tables_and_partial_index_exist() -> None:
    from app.db.session import engine

    inspector = sa.inspect(engine)
    assert "release_calendar" in inspector.get_table_names()
    assert "data_alerts" in inspector.get_table_names()
    indexes = {index["name"] for index in inspector.get_indexes("data_alerts")}
    assert "uq_data_alerts_open_scope_kind" in indexes
    assert "ix_data_alerts_status" in indexes
    release_unique = {
        tuple(index["column_names"]) for index in inspector.get_unique_constraints(
            "release_calendar"
        )
    }
    assert ("source", "key", "expected_on") in release_unique


def test_partial_unique_index_blocks_a_duplicate_open_alert() -> None:
    from app.db.session import SessionLocal

    code = f"alerttest-{uuid4().hex[:8]}"
    with SessionLocal() as session:
        institution = Institution(code=code, name="alert test")
        session.add(institution)
        session.commit()
        institution_id = institution.id

    now = datetime(2026, 10, 30, tzinfo=UTC)
    with SessionLocal() as session:
        session.add(
            DataAlert(
                institution_id=institution_id,
                scope="unique-scope",
                kind=alert_mod.NO_NEW_PERIOD,
                message="first",
                opened_at=now,
                last_seen_at=now,
            )
        )
        session.commit()
    with SessionLocal() as session:
        session.add(
            DataAlert(
                institution_id=institution_id,
                scope="unique-scope",
                kind=alert_mod.NO_NEW_PERIOD,
                message="duplicate",
                opened_at=now,
                last_seen_at=now,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


# --- calendar sync ----------------------------------------------------------


def _count_calendar_rows(session) -> int:
    return int(session.scalar(sa.select(sa.func.count()).select_from(ReleaseCalendar)) or 0)


def test_sync_calendar_is_idempotent() -> None:
    from app.db.session import SessionLocal

    day = f"2026-11-{uuid4().int % 27 + 1:02d}"
    evds = FakeEvdsCalendarClient(day=day)
    now = datetime(2026, 10, 3, 12, tzinfo=UTC)
    with SessionLocal() as session:
        first = sync_calendar(session, now=now, evds_client=evds, tuik_fetch=_tuik_fetch)
        session.commit()
    with SessionLocal() as session:
        before = _count_calendar_rows(session)
        second = sync_calendar(session, now=now, evds_client=evds, tuik_fetch=_tuik_fetch)
        session.commit()
    assert first.total > 0
    assert second.inserted == 0
    assert second.updated == 0
    with SessionLocal() as session:
        after = _count_calendar_rows(session)
    assert after == before


def test_sync_calendar_stores_core_relevant_rows_only() -> None:
    from app.db.session import SessionLocal

    day = f"2026-11-{uuid4().int % 27 + 1:02d}"
    evds = FakeEvdsCalendarClient(day=day)
    now = datetime(2026, 10, 3, 12, tzinfo=UTC)
    with SessionLocal() as session:
        sync_calendar(session, now=now, evds_client=evds, tuik_fetch=_tuik_fetch)
        session.commit()
    with SessionLocal() as session:
        keys = {
            (row.source, row.key)
            for row in session.scalars(sa.select(ReleaseCalendar)).all()
        }
    assert ("evds3", "bie_cli2") in keys
    assert ("tuik", "Dönemsel Gayrisafi Yurt İçi Hasıla") in keys
    assert all(key != "bie_notcore" for _source, key in keys)


def test_calendar_sync_command_commits_rows_readable_from_a_new_session() -> None:
    """The REAL CLI path: the command body owns the commit (the old bug)."""
    from app.db.session import SessionLocal

    args = build_parser().parse_args(["calendar-sync"])
    now = datetime(2026, 10, 3, 12, tzinfo=UTC)
    with SessionLocal() as session:
        code = _cmd_calendar_sync(
            session,
            args,
            now=now,
            evds=_FixtureEvdsCalendar(),
            tuik=_FixtureTuikCalendar(),
        )
    assert code == 0

    # Read back from a brand-new session: the command's session must have committed.
    with SessionLocal() as session:
        stored = {
            (row.source, row.key, row.expected_on)
            for row in session.scalars(sa.select(ReleaseCalendar)).all()
        }
    assert {
        ("evds3", "bie_cli2", date(2026, 10, 30)),
        ("evds3", "bie_cli2", date(2026, 10, 20)),
        ("tuik", TUIK_GDP_KEY, date(2026, 8, 31)),
        ("tuik", TUIK_GDP_KEY, date(2026, 12, 1)),
    } <= stored


# --- refresh engine ---------------------------------------------------------


# Every refresh_core test gets its own registry series. Observations and
# fetch_jobs cannot be removed from the shared test database, and the
# repeated-failure check plus the open-alert uniqueness are keyed by
# series/scope, so two tests on one series would leak failures and alerts into
# each other regardless of run order. The 15-series registry covers them all.
SERIES_FOR_TEST: dict[str, str] = {
    "test_forced_refresh_completes_and_resolves_alerts": "bie_cli2:TP.CLI2.A01",
    "test_three_failures_open_one_repeated_failure_alert": (
        "bie_gsyhuretcar:TP.GSYIH040.IFK.B1GQ"
    ),
    "test_format_changed_opens_on_first_failure": "bie_gsyhuretcar:TP.GSYIH040.IFK.A",
    "test_dry_run_writes_nothing": "bie_gsyhuretcar:TP.GSYIH040.IFK.BTE",
    "test_refresh_when_nothing_due_does_no_work": "bie_dkefkytl:TP.DK.USD.A.EF.YTL",
    "test_refresh_command_commits_job_and_observations": (
        "bie_gsyhuretcar:TP.GSYIH040.IFK.C"
    ),
    "test_refresh_job_times_use_the_clock": "bie_gsyhuretcar:TP.GSYIH040.IFK.F",
}


def _core_series_for(test_name: str) -> CoreSeries:
    external_code = SERIES_FOR_TEST[test_name]
    return next(core for core in CORE_SERIES if core.external_code == external_code)


def _load_fixture(name: str):
    return json.loads((FIXTURES_TCMB / name).read_text(encoding="utf-8"))


class _GdpCatalogClient:
    """Serves the real TÜİK GDP catalog so a test's ``sync_catalog`` is a no-op.

    The GDP tests share ``bie_gsyhuretcar`` with ``test_core_load``/``test_tcmb``,
    which assert that re-cataloguing it changes nothing. A synthetic
    :class:`FakeConnector` would rewrite the dataset's attributes and break them,
    so GDP cataloguing goes through the real connector and its fixtures.
    """

    def get_catalog(self) -> _Resp:
        return _Resp(_load_fixture("catalog-tuik.raw.json"))

    def get_serie_list(self, group: str) -> _Resp:
        if group == GDP_GROUP:
            return _Resp(_load_fixture(f"serielist-{GDP_GROUP}.raw.json"))
        return _Resp([])

    def close(self) -> None:
        return None


class _FailingConnector(SourceConnector):
    """The real catalog of ``base`` but a fetch that always raises ``failure``."""

    def __init__(self, base: TcmbConnector, failure: ConnectorError) -> None:
        self._base = base
        self.failure = failure
        self.institution_code = base.institution_code
        self.institution_name = base.institution_name
        self.channel = base.channel

    def list_datasets(self):
        yield from self._base.list_datasets()

    def fetch_series(self, *args, **kwargs):
        raise self.failure


class _PointConnector(SourceConnector):
    """The real catalog of ``base`` but a fetch returning fixed ``points``."""

    def __init__(self, base: TcmbConnector, points: list[tuple[date, Decimal]]) -> None:
        self._base = base
        self._points = points
        self.institution_code = base.institution_code
        self.institution_name = base.institution_name
        self.channel = base.channel

    def list_datasets(self):
        yield from self._base.list_datasets()

    def fetch_series(self, dataset_code, codes, *, order=None, start=date(2000, 1, 1)):
        points = [point for point in self._points if point[0] >= start]
        return FetchResult(
            external_code=f"{dataset_code}:{codes.get('SERIE', '')}",
            points=points,
            raw_object_keys=[],
            channel=self.channel,
        )


def _connector_for_series(
    core: CoreSeries,
    *,
    failure: ConnectorError | None = None,
    points: list[tuple[date, Decimal]] | None = None,
) -> SourceConnector:
    if core.source == "tuik-evds":
        base = TcmbConnector(source="tuik-evds", client=_GdpCatalogClient())
        if failure is not None:
            return _FailingConnector(base, failure)
        return base
    return FakeConnector(
        dataset_code=core.dataset_code,
        frequency="daily" if core.dataset_code == "bie_dkefkytl" else "monthly",
        serie_code=core.serie_code,
        points=points or [],
        failure=failure,
    )


def test_forced_refresh_completes_and_resolves_alerts() -> None:
    from app.db.session import SessionLocal

    core = _core_series_for("test_forced_refresh_completes_and_resolves_alerts")
    connector = _connector_for_series(
        core,
        points=[
            (date(2026, 8, 1), Decimal("100.1")),
            (date(2026, 9, 1), Decimal("101.2")),
        ],
    )
    connectors = {core.source: connector}
    with SessionLocal() as session:
        _catalog(session, connector)
        institution = session.scalar(
            sa.select(Institution).where(Institution.code == connector.institution_code)
        )
        assert institution is not None
        institution_id = institution.id
    # Pre-open a repeated_failure alert; the successful run must resolve it.
    with SessionLocal() as session:
        alert_mod.open_alert(
            session,
            institution_id=institution_id,
            scope=core.external_code,
            kind=alert_mod.REPEATED_FAILURE,
            message="old failure",
        )
        session.commit()

    now = datetime(2026, 10, 30, 12, tzinfo=UTC)
    report = refresh_core(
        SessionLocal,
        _connector_for(connectors),
        now=now,
        only=core.external_code,
        force=True,
    )
    assert report.failed is False
    assert report.fetched == 1
    assert alert_mod.REPEATED_FAILURE in report.series[0].alerts_resolved

    with SessionLocal() as session:
        jobs = session.scalars(
            sa.select(fetch_jobs.FetchJob).where(
                fetch_jobs.FetchJob.external_code == core.external_code,
                fetch_jobs.FetchJob.attributes["origin"].astext == ORIGIN,
                fetch_jobs.FetchJob.status == fetch_jobs.COMPLETED,
            )
        ).all()
    assert jobs
    with SessionLocal() as session:
        open_alerts = alert_mod.list_alerts(session, statuses={"open"})
    assert not any(
        row.kind == alert_mod.REPEATED_FAILURE and row.scope == core.external_code
        for row in open_alerts
    )


def test_refresh_command_commits_job_and_observations() -> None:
    """The REAL CLI path: ``_cmd_refresh`` leaves a durable job and observations."""
    from app.db.session import SessionLocal

    core = _core_series_for("test_refresh_command_commits_job_and_observations")
    points = [
        (date(2026, 7, 1), Decimal("100.1")),
        (date(2026, 10, 1), Decimal("101.2")),
    ]
    connector = _PointConnector(
        TcmbConnector(source="tuik-evds", client=_GdpCatalogClient()), points
    )
    connectors = {core.source: connector}
    with SessionLocal() as session:
        _catalog(session, connector)

    args = build_parser().parse_args(
        ["refresh", "--only", core.external_code, "--force"]
    )
    assert _cmd_refresh(_connector_for(connectors), args) == 0

    # Read back from a brand-new session: the job and the points must be committed.
    with SessionLocal() as session:
        jobs = session.scalars(
            sa.select(fetch_jobs.FetchJob).where(
                fetch_jobs.FetchJob.external_code == core.external_code,
                fetch_jobs.FetchJob.attributes["origin"].astext == ORIGIN,
                fetch_jobs.FetchJob.status == fetch_jobs.COMPLETED,
            )
        ).all()
    assert jobs

    with SessionLocal() as session:
        institution = session.scalar(
            sa.select(Institution).where(Institution.code == connector.institution_code)
        )
        assert institution is not None
        series = session.scalar(
            sa.select(Series).where(
                Series.institution_id == institution.id,
                Series.external_code == core.external_code,
            )
        )
        assert series is not None
        periods = set(
            session.scalars(
                sa.select(Observation.period).where(Observation.series_id == series.id)
            ).all()
        )
    assert {date(2026, 7, 1), date(2026, 10, 1)} <= periods


def test_three_failures_open_one_repeated_failure_alert() -> None:
    from app.db.session import SessionLocal

    core = _core_series_for("test_three_failures_open_one_repeated_failure_alert")
    failing = _connector_for_series(
        core, failure=ConnectorError(EMPTY, "no observations")
    )
    connectors = {core.source: failing}
    with SessionLocal() as session:
        _catalog(session, failing)

    now = datetime(2026, 11, 20, 12, tzinfo=UTC)
    for _ in range(3):
        report = refresh_core(
            SessionLocal,
            _connector_for(connectors),
            now=now,
            only=core.external_code,
            force=True,
        )
        assert report.failed is True

    with SessionLocal() as session:
        rows = [
            row
            for row in alert_mod.list_alerts(session, statuses={"open"})
            if row.scope == core.external_code and row.kind == alert_mod.REPEATED_FAILURE
        ]
    assert len(rows) == 1


def test_format_changed_opens_on_first_failure() -> None:
    from app.db.session import SessionLocal

    core = _core_series_for("test_format_changed_opens_on_first_failure")
    failing = _connector_for_series(
        core, failure=ConnectorError(FORMAT_CHANGED, "missing field")
    )
    connectors = {core.source: failing}
    with SessionLocal() as session:
        _catalog(session, failing)

    now = datetime(2026, 11, 20, 12, tzinfo=UTC)
    report = refresh_core(
        SessionLocal,
        _connector_for(connectors),
        now=now,
        only=core.external_code,
        force=True,
    )
    assert alert_mod.FORMAT_CHANGED in report.series[0].alerts_opened
    with SessionLocal() as session:
        exchange = alert_mod.list_alerts(
            session, statuses={"open"}, scope=core.external_code
        )
    assert [row.kind for row in exchange] == [alert_mod.FORMAT_CHANGED]


def test_refresh_when_nothing_due_does_no_work() -> None:
    from app.db.session import SessionLocal

    core = _core_series_for("test_refresh_when_nothing_due_does_no_work")
    connector = _connector_for_series(core)
    connectors = {core.source: connector}
    with SessionLocal() as session:
        _catalog(session, connector)

    def _job_count() -> int:
        with SessionLocal() as session:
            return int(
                session.scalar(
                    sa.select(sa.func.count()).select_from(fetch_jobs.FetchJob).where(
                        fetch_jobs.FetchJob.external_code == core.external_code,
                        fetch_jobs.FetchJob.attributes["origin"].astext == ORIGIN,
                    )
                )
                or 0
            )

    before = _job_count()
    # A Saturday, after the daily cut-off: the daily series is not due.
    saturday = datetime(2026, 10, 31, 14, tzinfo=UTC)
    report = refresh_core(
        SessionLocal,
        _connector_for(connectors),
        now=saturday,
        only=core.external_code,
    )
    assert report.fetched == 0
    assert _job_count() == before


def test_dry_run_writes_nothing() -> None:
    from app.db.session import SessionLocal

    core = _core_series_for("test_dry_run_writes_nothing")
    connector = _connector_for_series(core, points=[(date(2026, 10, 1), Decimal("1.0"))])
    connectors = {core.source: connector}
    with SessionLocal() as session:
        _catalog(session, connector)

    def _job_count() -> int:
        with SessionLocal() as session:
            return int(
                session.scalar(
                    sa.select(sa.func.count()).select_from(fetch_jobs.FetchJob).where(
                        fetch_jobs.FetchJob.external_code == core.external_code,
                        fetch_jobs.FetchJob.attributes["origin"].astext == ORIGIN,
                    )
                )
                or 0
            )

    before = _job_count()
    now = datetime(2026, 10, 30, 12, tzinfo=UTC)
    report = refresh_core(
        SessionLocal,
        _connector_for(connectors),
        now=now,
        only=core.external_code,
        force=True,
        dry_run=True,
    )
    assert report.fetched == 1
    assert report.series[0].outcome == "dry_run"
    assert _job_count() == before


def test_refresh_job_times_use_the_clock() -> None:
    """A live job's times advance with the clock, not with the decision ``now``."""
    from app.db.session import SessionLocal

    core = _core_series_for("test_refresh_job_times_use_the_clock")
    points = [
        (date(2026, 7, 1), Decimal("100.1")),
        (date(2026, 10, 1), Decimal("101.2")),
    ]
    connector = _PointConnector(
        TcmbConnector(source="tuik-evds", client=_GdpCatalogClient()), points
    )
    connectors = {core.source: connector}
    with SessionLocal() as session:
        _catalog(session, connector)

    decision_now = datetime(2030, 1, 1, 12, tzinfo=UTC)
    first = datetime(2030, 1, 1, 12, 0, 5, tzinfo=UTC)
    second = datetime(2030, 1, 1, 12, 7, 0, tzinfo=UTC)
    times = iter([first, second])
    report = refresh_core(
        SessionLocal,
        _connector_for(connectors),
        now=decision_now,
        clock=lambda: next(times),
        only=core.external_code,
        force=True,
    )
    assert report.failed is False

    with SessionLocal() as session:
        jobs = session.scalars(
            sa.select(fetch_jobs.FetchJob).where(
                fetch_jobs.FetchJob.external_code == core.external_code,
                fetch_jobs.FetchJob.attributes["origin"].astext == ORIGIN,
                fetch_jobs.FetchJob.status == fetch_jobs.COMPLETED,
            )
        ).all()
    assert jobs
    job = max(jobs, key=lambda item: item.id)
    assert job.started_at == first
    assert job.finished_at == second
    assert job.started_at != decision_now
    assert job.finished_at >= job.started_at


def test_refresh_only_rejects_unknown_external_code() -> None:
    from app.db.session import SessionLocal

    def factory(source: str) -> FakeConnector:
        raise AssertionError("no connector is built before the code is validated")

    with pytest.raises(ValueError):
        refresh_core(
            SessionLocal,
            factory,
            now=datetime(2026, 10, 30, 12, tzinfo=UTC),
            only="not-a-core-series",
        )
