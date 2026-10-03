"""Integration tests for the core scheduler (isolated test DB, fake clients).

``run_tick`` runs its real write path: the calendar job moves
``requested -> fetching -> completed/failed`` in ``fetch_jobs`` and alert rows
are opened/resolved. The calendar clients and connectors are fakes, so no
network is touched. The shared database cannot be cleaned (observations block
deletes), so assertions are order-independent and read results back from a
brand-new session; the calendar scope is fixed by the scheduler, so the tests
are written to tolerate whatever open alert state a previous module left.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.config import Settings
from app.connectors.base import (
    DatasetMeta,
    DimensionCodeMeta,
    DimensionMeta,
    FetchResult,
    SourceConnector,
    sync_catalog,
)
from app.core import alerts as alert_mod
from app.core import scheduler
from app.core.calendar import sync_calendar
from app.core.refresh import ORIGIN, RefreshReport
from app.data import fetch_jobs
from app.data.models import FetchJob, ReleaseCalendar

pytestmark = pytest.mark.integration

USD_SERIE = "TP.DK.USD.A.EF.YTL"
USD_EXTERNAL = f"bie_dkefkytl:{USD_SERIE}"
CALENDAR_DAY = date(2027, 7, 15)


class _Resp:
    def __init__(self, payload) -> None:
        self._payload = payload

    def json(self):
        return self._payload


def _evds_payload(day: date) -> list[dict]:
    return [
        {
            "id": "x",
            "tarih": f"{day.isoformat()} 23:00",
            "donem": "Test 2027",
            "yayinBilgi": {"veriGrubuKodu": "bie_cli2", "yayimAdi": "Fake"},
        }
    ]


class _EvdsCalendar:
    def __init__(self, day: date) -> None:
        self._day = day

    def get_calendar(self, year: int, month: int) -> _Resp:
        return _Resp(_evds_payload(self._day))

    def close(self) -> None:
        return None


class _TuikCalendar:
    def fetch(self, year: int) -> _Resp:
        return _Resp({})

    def close(self) -> None:
        return None


class _PointsConnector(SourceConnector):
    """A connector that answers any serie with one fixed point (no network)."""

    def __init__(self, source: str) -> None:
        self.institution_code = "tcmb" if source == "tcmb" else "tuik"
        self.institution_name = source
        self.channel = f"fake-{source}"

    def list_datasets(self):
        return iter(())

    def fetch_series(self, dataset_code, codes, *, order=None, start=date(2000, 1, 1), **kwargs):
        return FetchResult(
            external_code=f"{dataset_code}:{codes.get('SERIE', '')}",
            points=[(date(2020, 1, 1), Decimal("1.5"))],
            raw_object_keys=[],
            channel=self.channel,
        )


class _UsdConnector(_PointsConnector):
    """Self-contained USD (``bie_dkefkytl``) catalog + fetch, so the test can
    prove a refresh job without depending on another module's cataloguing."""

    def __init__(self) -> None:
        super().__init__("tcmb")
        self.channel = "fake-usd"

    def list_datasets(self):
        yield DatasetMeta(
            external_code="bie_dkefkytl",
            name="Fake USD",
            attributes={"default_frequency": "daily", "channel": self.channel},
            dimensions=[
                DimensionMeta(
                    code="SERIE",
                    label="Seri",
                    position=0,
                    role="other",
                    codes=[DimensionCodeMeta(USD_SERIE, USD_SERIE)],
                )
            ],
        )


def test_run_tick_success_writes_calendar_job_and_rows(monkeypatch) -> None:
    from app.db.session import SessionLocal

    monkeypatch.setattr(scheduler, "refresh_core", lambda *a, **k: RefreshReport([]))
    decision_now = datetime(2026, 12, 15, 12, tzinfo=UTC)
    first = datetime(2026, 12, 15, 12, 0, 5, tzinfo=UTC)
    second = datetime(2026, 12, 15, 12, 0, 9, tzinfo=UTC)
    times = iter([first, second])

    evds = _EvdsCalendar(CALENDAR_DAY)
    tuik = _TuikCalendar()

    def calendar_sync(session, *, now):
        return sync_calendar(session, now=now, evds_client=evds, tuik_fetch=tuik.fetch)

    def connector_for(source):
        raise AssertionError("refresh is stubbed and must not build a connector")

    report = scheduler.run_tick(
        SessionLocal,
        connector_for=connector_for,
        calendar_sync=calendar_sync,
        now=decision_now,
        clock=lambda: next(times),
    )

    assert report.calendar.outcome == "completed"
    assert report.calendar.inserted >= 1

    with SessionLocal() as session:
        rows = session.scalars(
            sa.select(ReleaseCalendar).where(
                ReleaseCalendar.source == "evds3",
                ReleaseCalendar.key == "bie_cli2",
                ReleaseCalendar.expected_on == CALENDAR_DAY,
            )
        ).all()
        jobs = session.scalars(
            sa.select(FetchJob).where(
                FetchJob.external_code == scheduler.CALENDAR_CODE,
                FetchJob.attributes["origin"].astext == scheduler.CALENDAR_ORIGIN,
                FetchJob.status == fetch_jobs.COMPLETED,
            )
        ).all()
    assert rows
    assert jobs
    job = max(jobs, key=lambda item: item.id)
    assert job.started_at == first
    assert job.finished_at == second
    assert job.started_at != decision_now
    assert job.finished_at >= job.started_at


def test_failing_calendar_records_jobs_and_refresh_still_runs() -> None:
    from app.db.session import SessionLocal

    def calendar_sync(session, *, now):
        raise RuntimeError("calendar down")

    usd = _UsdConnector()
    connectors = {"tcmb": usd, "tuik-evds": _PointsConnector("tuik-evds")}
    with SessionLocal() as session:
        sync_catalog(session, usd)
        session.commit()

    def connector_for(source):
        return connectors[source]

    config = Settings(_env_file=None)
    start = datetime(2030, 1, 1, 14, tzinfo=UTC)
    for index in range(3):
        moment = start + timedelta(hours=3 * index)
        report = scheduler.run_tick(
            SessionLocal,
            connector_for=connector_for,
            calendar_sync=calendar_sync,
            now=moment,
            clock=lambda moment=moment: moment,
            config=config,
        )
        assert report.calendar.failed is True

    with SessionLocal() as session:
        calendar_jobs = session.scalars(
            sa.select(FetchJob).where(
                FetchJob.external_code == scheduler.CALENDAR_CODE,
                FetchJob.attributes["origin"].astext == scheduler.CALENDAR_ORIGIN,
            )
        ).all()
        open_alerts = alert_mod.list_alerts(
            session, statuses={"open"}, scope=scheduler.CALENDAR_SCOPE
        )
        refresh_jobs = session.scalars(
            sa.select(FetchJob).where(
                FetchJob.external_code == USD_EXTERNAL,
                FetchJob.attributes["origin"].astext == ORIGIN,
            )
        ).all()

    assert len(calendar_jobs) >= 3
    assert all(job.status == fetch_jobs.FAILED for job in calendar_jobs[-3:])
    assert any(alert.kind == alert_mod.REPEATED_FAILURE for alert in open_alerts)
    # Step 2 still ran after every calendar failure and produced its own job.
    assert refresh_jobs
