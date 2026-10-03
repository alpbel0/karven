"""Integration tests for on-demand fetching against the isolated test stack.

Real PostgreSQL paths are exercised (the request lock/race, the runner's claim
and rollback, the watchdog and waiter resolution); the connector is a fake, so
no network is touched. Observations and fetch_jobs cannot be deleted from the
shared test database, so every test uses a unique institution/dataset/series and
asserts only its own rows.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
import sqlalchemy as sa

from app.connectors.base import EMPTY, ConnectorError, FetchResult, SourceConnector
from app.data import fetch_jobs
from app.data.fetch_requests import request_fetch
from app.data.models import (
    Dataset,
    DatasetDimension,
    DimensionCode,
    FetchJob,
    FetchJobWaiter,
    Institution,
    Observation,
    Series,
)
from app.db.session import SessionLocal
from app.fetching.runner import run_job
from app.fetching.watchdog import reap_stalled

pytestmark = pytest.mark.integration


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:10]}"


def _catalog(session, *, institution_code: str, dataset_code: str, serie_code: str = "X"):
    institution = Institution(code=institution_code, name=institution_code)
    session.add(institution)
    session.flush()
    dataset = Dataset(
        institution_id=institution.id,
        external_code=dataset_code,
        name=dataset_code,
        attributes={"channel": "evds3", "default_frequency": "monthly"},
    )
    session.add(dataset)
    session.flush()
    dimension = DatasetDimension(
        dataset_id=dataset.id, code="SERIE", label="SERIE", position=0, role="other"
    )
    session.add(dimension)
    session.flush()
    session.add(DimensionCode(dimension_id=dimension.id, code=serie_code, label=serie_code))
    session.flush()
    return institution.id, dataset.id


def _count_observations(session, institution_code: str, external_code: str) -> int:
    series = session.scalar(
        sa.select(Series)
        .join(Institution, Institution.id == Series.institution_id)
        .where(
            Institution.code == institution_code,
            Series.external_code == external_code,
        )
    )
    if series is None:
        return 0
    return int(
        session.scalar(
            sa.select(sa.func.count())
            .select_from(Observation)
            .where(Observation.series_id == series.id)
        )
        or 0
    )


class _FakeConnector(SourceConnector):
    """A connector that returns fixed points, optionally sleeping or blocking."""

    institution_code = "tcmb"
    institution_name = "Fake TCMB"
    channel = "evds3-fake"

    def __init__(self, points, *, delay: float = 0.0, started=None, release=None) -> None:
        self._points = points
        self._delay = delay
        self._started = started
        self._release = release
        self.fetch_calls = 0
        self.closed = False

    def list_datasets(self):
        return iter(())

    def fetch_series(self, dataset_code, codes, *, order=None, start=date(2000, 1, 1)):
        self.fetch_calls += 1
        if self._started is not None:
            self._started.set()
        if self._release is not None:
            self._release.wait(10)
        if self._delay:
            time.sleep(self._delay)
        points = [point for point in self._points if point[0] >= start]
        return FetchResult(
            external_code=f"{dataset_code}:{codes.get('SERIE', '')}",
            points=points,
            raw_object_keys=[],
            channel=self.channel,
        )

    def close(self) -> None:
        self.closed = True


def test_two_concurrent_requests_share_one_job_and_two_waiters() -> None:
    institution_code = _unique("inst")
    dataset_code = _unique("ds")
    with SessionLocal() as session:
        _catalog(session, institution_code=institution_code, dataset_code=dataset_code)
        session.commit()

    results: list = []
    barrier = threading.Barrier(2)

    def worker(waiter_id: str) -> None:
        with SessionLocal() as session:
            barrier.wait(5)
            result = request_fetch(
                session,
                institution_code=institution_code,
                dataset_code=dataset_code,
                codes={"SERIE": "X"},
                waiter=("idea", waiter_id),
            )
            session.commit()
            results.append(result)

    threads = [threading.Thread(target=worker, args=(str(i),)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(15)

    assert len(results) == 2
    with SessionLocal() as session:
        jobs = session.scalars(
            sa.select(FetchJob).where(FetchJob.external_code == f"{dataset_code}:X")
        ).all()
        active = [job for job in jobs if job.status in fetch_jobs.ACTIVE_STATUSES]
        waiters = session.scalars(
            sa.select(FetchJobWaiter).where(
                FetchJobWaiter.fetch_job_id.in_([job.id for job in jobs])
            )
        ).all()
    assert len(active) == 1
    assert len(waiters) == 2

    # Complete the job, then request again: a NEW job must open.
    job_id = active[0].id
    with SessionLocal() as session:
        job = session.get(FetchJob, job_id)
        fetch_jobs.mark_completed(session, job)
        session.commit()
    with SessionLocal() as session:
        again = request_fetch(
            session,
            institution_code=institution_code,
            dataset_code=dataset_code,
            codes={"SERIE": "X"},
        )
        session.commit()
    assert again.created is True
    assert again.job_id != job_id


def test_a_waiter_is_never_parked_on_a_terminal_job() -> None:
    institution_code = _unique("inst")
    dataset_code = _unique("ds")
    with SessionLocal() as session:
        _catalog(session, institution_code=institution_code, dataset_code=dataset_code)
        first = request_fetch(
            session,
            institution_code=institution_code,
            dataset_code=dataset_code,
            codes={"SERIE": "X"},
        )
        session.commit()

    with SessionLocal() as session:
        job = session.get(FetchJob, first.job_id)
        fetch_jobs.mark_completed(session, job)
        session.commit()

    with SessionLocal() as session:
        second = request_fetch(
            session,
            institution_code=institution_code,
            dataset_code=dataset_code,
            codes={"SERIE": "X"},
            waiter=("idea", _unique("w")),
        )
        session.commit()

    assert second.created is True
    with SessionLocal() as session:
        waiting = session.scalars(
            sa.select(FetchJobWaiter).where(FetchJobWaiter.status == fetch_jobs.WAITING)
        ).all()
        terminal_ids = set(
            session.scalars(
                sa.select(FetchJob.id).where(FetchJob.status.in_(fetch_jobs.TERMINAL_STATUSES))
            ).all()
        )
    assert all(row.fetch_job_id not in terminal_ids for row in waiting)
    assert any(row.fetch_job_id == second.job_id for row in waiting)


def test_a_long_fetch_heartbeats_and_completes_once() -> None:
    institution_code = _unique("inst")
    dataset_code = _unique("ds")
    points = [(date(2026, 1, 1), Decimal("1.0")), (date(2026, 2, 1), Decimal("2.0"))]
    with SessionLocal() as session:
        _catalog(session, institution_code=institution_code, dataset_code=dataset_code)
        request = request_fetch(
            session,
            institution_code=institution_code,
            dataset_code=dataset_code,
            codes={"SERIE": "X"},
            waiter=("idea", _unique("w")),
        )
        session.commit()

    # Sleep well past the stall threshold while beating far more often.
    threshold = 0.05
    connector = _FakeConnector(points, delay=threshold * 4)
    result = run_job(
        SessionLocal,
        request.job_id,
        connector_for=lambda institution, dataset: (connector, {}),
        clock=lambda: datetime.now(UTC),
        heartbeat_interval=threshold / 5,
    )

    assert result.outcome == "completed"
    with SessionLocal() as session:
        job = session.get(FetchJob, request.job_id)
        assert job.status == fetch_jobs.COMPLETED
        assert job.heartbeat_at > job.started_at
        waiters = session.scalars(
            sa.select(FetchJobWaiter).where(FetchJobWaiter.fetch_job_id == request.job_id)
        ).all()
        count = _count_observations(session, institution_code, f"{dataset_code}:X")
    assert [row.status for row in waiters] == [fetch_jobs.READY]
    assert count == len(points)

    # Claiming the terminal job again does nothing.
    second = run_job(
        SessionLocal,
        request.job_id,
        connector_for=lambda institution, dataset: (_FakeConnector(points), {}),
        clock=lambda: datetime.now(UTC),
        heartbeat_interval=threshold,
    )
    assert second.outcome == "skipped"


def test_a_hung_job_is_reaped_and_observations_are_not_committed() -> None:
    institution_code = _unique("inst")
    dataset_code = _unique("ds")
    points = [(date(2026, 1, 1), Decimal("9.0"))]
    with SessionLocal() as session:
        _catalog(session, institution_code=institution_code, dataset_code=dataset_code)
        request = request_fetch(
            session,
            institution_code=institution_code,
            dataset_code=dataset_code,
            codes={"SERIE": "X"},
            waiter=("idea", _unique("w")),
        )
        session.commit()

    started = threading.Event()
    release = threading.Event()
    connector = _FakeConnector(points, started=started, release=release)
    outcome: dict = {}

    def target() -> None:
        outcome["result"] = run_job(
            SessionLocal,
            request.job_id,
            connector_for=lambda institution, dataset: (connector, {}),
            clock=lambda: datetime.now(UTC),
            heartbeat_interval=3600,  # heartbeat effectively disabled
        )

    thread = threading.Thread(target=target)
    thread.start()
    assert started.wait(10)
    time.sleep(0.2)

    with SessionLocal() as session:
        report = reap_stalled(session, now=datetime.now(UTC), stall_after=timedelta(seconds=0.05))
    assert request.job_id in report.reaped

    release.set()
    thread.join(15)

    assert outcome["result"].outcome == "reaped"
    with SessionLocal() as session:
        job = session.get(FetchJob, request.job_id)
        waiters = session.scalars(
            sa.select(FetchJobWaiter).where(FetchJobWaiter.fetch_job_id == request.job_id)
        ).all()
        count = _count_observations(session, institution_code, f"{dataset_code}:X")
    assert job.status == fetch_jobs.FAILED
    assert job.error_reason == fetch_jobs.HEARTBEAT_LOST_REASON
    assert [row.status for row in waiters] == [fetch_jobs.DROPPED]
    assert count == 0


def test_fail_if_fetching_never_overwrites_a_job_completed_elsewhere() -> None:
    """The stale identity-map copy must not beat the freshly locked row."""
    institution_code = _unique("inst")
    dataset_code = _unique("ds")
    with SessionLocal() as session:
        _catalog(session, institution_code=institution_code, dataset_code=dataset_code)
        request = request_fetch(
            session,
            institution_code=institution_code,
            dataset_code=dataset_code,
            codes={"SERIE": "X"},
        )
        session.commit()

    old = datetime.now(UTC) - timedelta(minutes=10)
    with SessionLocal() as session:
        # This session loads the job as a stalled `fetching` row and keeps it in
        # its identity map across the commit (expire_on_commit=False).
        job = session.get(FetchJob, request.job_id)
        job.status = fetch_jobs.FETCHING
        job.started_at = old
        job.heartbeat_at = old
        session.commit()

        # Another session completes it while the stale copy is still loaded.
        with SessionLocal() as other:
            done = other.get(FetchJob, request.job_id)
            fetch_jobs.mark_completed(other, done, now=datetime.now(UTC))
            other.commit()

        result = fetch_jobs.fail_if_fetching(
            session,
            request.job_id,
            error_reason=fetch_jobs.HEARTBEAT_LOST_REASON,
            now=datetime.now(UTC),
        )
        session.commit()

    assert result is None
    with SessionLocal() as session:
        job = session.get(FetchJob, request.job_id)
        assert job.status == fetch_jobs.COMPLETED
        assert job.error_reason is None


def test_request_fetch_opens_a_new_job_when_the_active_one_completed_elsewhere() -> None:
    institution_code = _unique("inst")
    dataset_code = _unique("ds")
    with SessionLocal() as session:
        _catalog(session, institution_code=institution_code, dataset_code=dataset_code)
        first = request_fetch(
            session,
            institution_code=institution_code,
            dataset_code=dataset_code,
            codes={"SERIE": "X"},
        )
        session.commit()

    with SessionLocal() as session:
        # Preload the active job into this session's identity map.
        stale = session.get(FetchJob, first.job_id)
        assert stale.status == fetch_jobs.REQUESTED

        with SessionLocal() as other:
            done = other.get(FetchJob, first.job_id)
            fetch_jobs.mark_completed(other, done, now=datetime.now(UTC))
            other.commit()

        second = request_fetch(
            session,
            institution_code=institution_code,
            dataset_code=dataset_code,
            codes={"SERIE": "X"},
            waiter=("idea", _unique("w")),
        )
        session.commit()

    assert second.created is True
    assert second.job_id != first.job_id
    with SessionLocal() as session:
        terminal_waiters = session.scalars(
            sa.select(FetchJobWaiter).where(FetchJobWaiter.fetch_job_id == first.job_id)
        ).all()
        new_waiters = session.scalars(
            sa.select(FetchJobWaiter).where(FetchJobWaiter.fetch_job_id == second.job_id)
        ).all()
    assert terminal_waiters == []
    assert [row.status for row in new_waiters] == [fetch_jobs.WAITING]


def test_a_failed_fetch_marks_the_job_and_drops_waiters() -> None:
    institution_code = _unique("inst")
    dataset_code = _unique("ds")

    class _FailingConnector(_FakeConnector):
        def fetch_series(self, *args, **kwargs):
            raise ConnectorError(EMPTY, "no observations")

    with SessionLocal() as session:
        _catalog(session, institution_code=institution_code, dataset_code=dataset_code)
        request = request_fetch(
            session,
            institution_code=institution_code,
            dataset_code=dataset_code,
            codes={"SERIE": "X"},
            waiter=("idea", _unique("w")),
        )
        session.commit()

    result = run_job(
        SessionLocal,
        request.job_id,
        connector_for=lambda institution, dataset: (_FailingConnector(points=[]), {}),
        clock=lambda: datetime.now(UTC),
        heartbeat_interval=30,
    )

    assert result.outcome == "failed"
    assert result.error == "empty: no observations"
    with SessionLocal() as session:
        job = session.get(FetchJob, request.job_id)
        waiters = session.scalars(
            sa.select(FetchJobWaiter).where(FetchJobWaiter.fetch_job_id == request.job_id)
        ).all()
    assert job.status == fetch_jobs.FAILED
    assert job.error_reason == "empty: no observations"
    assert [row.status for row in waiters] == [fetch_jobs.DROPPED]


def test_fetch_job_waiters_schema() -> None:
    from app.db.session import engine

    inspector = sa.inspect(engine)
    assert "fetch_job_waiters" in inspector.get_table_names()
    indexes = {index["name"] for index in inspector.get_indexes("fetch_job_waiters")}
    assert {"ix_fetch_job_waiters_waiter", "ix_fetch_job_waiters_job"} <= indexes
    unique = {
        tuple(constraint["column_names"])
        for constraint in inspector.get_unique_constraints("fetch_job_waiters")
    }
    assert ("fetch_job_id", "waiter_type", "waiter_id") in unique
