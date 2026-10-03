"""Unit tests for the on-demand runner (fake session, fake connector)."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

import app.fetching.runner as runner
from app.connectors.base import EMPTY, ConnectorError
from app.data import fetch_jobs
from app.data.errors import SeriesDefinitionError
from tests.fetch_fakes import FakeSession, Store, factory, make_dataset, make_institution

FIXED = datetime(2026, 10, 3, 12, tzinfo=UTC)


@dataclass
class _Ingest:
    inserted: int = 1
    unchanged: int = 0
    series_id: int = 10


class _Connector:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _clock() -> datetime:
    return FIXED


def _make_job(store: Store, session) -> tuple:
    institution = make_institution(store)
    dataset = make_dataset(store, institution)
    job = fetch_jobs.create_job(
        session,
        institution_id=institution.id,
        external_code="DS_TEST:X",
        attributes={
            "origin": "on_demand",
            "dataset_code": "DS_TEST",
            "codes": {"SERIE": "X"},
            "start": "2000-01-01",
        },
    )
    return institution, dataset, job


def _patch(monkeypatch, dataset, ingest) -> None:
    monkeypatch.setattr(
        runner,
        "find_dataset",
        lambda session, code, dataset_code: (
            dataset if dataset_code == dataset.external_code else None
        ),
    )
    monkeypatch.setattr(runner, "ingest_series", ingest)


def _run(store: Store, job, connector, **overrides):
    kwargs = {
        "connector_for": lambda institution, dataset: (connector, {}),
        "clock": _clock,
        "heartbeat_interval": 3600,
    }
    kwargs.update(overrides)
    return runner.run_job(factory(store), job.id, **kwargs)


def test_success_path_marks_completed(monkeypatch) -> None:
    store = Store()
    session = factory(store)()
    institution, dataset, job = _make_job(store, session)
    connector = _Connector()
    seen: list = []

    def ingest(session, connector, *, dataset, codes, start, **kwargs):
        seen.append((dataset.external_code, dict(codes), start.isoformat()))
        return _Ingest(inserted=3, unchanged=1)

    _patch(monkeypatch, dataset, ingest)
    result = _run(store, job, connector)

    assert result.outcome == "completed"
    assert (result.inserted, result.unchanged) == (3, 1)
    assert job.status == fetch_jobs.COMPLETED
    assert job.started_at == FIXED
    assert job.finished_at == FIXED
    assert connector.closed is True
    assert seen == [("DS_TEST", {"SERIE": "X"}, "2000-01-01")]


def test_second_run_of_the_same_job_is_a_noop(monkeypatch) -> None:
    store = Store()
    session = factory(store)()
    institution, dataset, job = _make_job(store, session)
    calls: list = []

    def ingest(session, connector, *, dataset, codes, start, **kwargs):
        calls.append(1)
        return _Ingest()

    _patch(monkeypatch, dataset, ingest)
    first = _run(store, job, _Connector())
    second = _run(store, job, _Connector())

    assert first.outcome == "completed"
    assert second.outcome == "skipped"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "exc,prefix",
    [
        (ConnectorError(EMPTY, "no data"), "empty: no data"),
        (SeriesDefinitionError("bad breakdown"), "SeriesDefinitionError: bad breakdown"),
        (LookupError("no such dataset"), "LookupError: no such dataset"),
    ],
)
def test_known_failure_classes_record_a_reason(monkeypatch, exc, prefix) -> None:
    store = Store()
    session = factory(store)()
    institution, dataset, job = _make_job(store, session)

    def ingest(session, connector, *, dataset, codes, start, **kwargs):
        raise exc

    _patch(monkeypatch, dataset, ingest)
    result = _run(store, job, _Connector())

    assert result.outcome == "failed"
    assert result.error == prefix
    assert job.status == fetch_jobs.FAILED
    assert job.error_reason == prefix


def test_unexpected_exception_is_reraised_after_recording(monkeypatch) -> None:
    store = Store()
    session = factory(store)()
    institution, dataset, job = _make_job(store, session)

    def ingest(session, connector, *, dataset, codes, start, **kwargs):
        raise ValueError("weird")

    _patch(monkeypatch, dataset, ingest)
    with pytest.raises(ValueError):
        _run(store, job, _Connector())

    assert job.status == fetch_jobs.FAILED
    assert job.error_reason == "ValueError: weird"


def test_missing_dataset_fails_without_calling_the_connector(monkeypatch) -> None:
    store = Store()
    session = factory(store)()
    institution, dataset, job = _make_job(store, session)
    monkeypatch.setattr(runner, "find_dataset", lambda *args, **kwargs: None)

    def ingest(*args, **kwargs):
        raise AssertionError("ingest must not run without a dataset")

    monkeypatch.setattr(runner, "ingest_series", ingest)
    result = _run(store, job, _Connector())

    assert result.outcome == "failed"
    assert result.error is not None and result.error.startswith("LookupError")


def test_an_unsupported_channel_fails_without_raising(monkeypatch) -> None:
    from app.fetching.connectors import connector_for as real_connector_for

    store = Store()
    session = factory(store)()
    institution = make_institution(store)
    dataset = make_dataset(store, institution, channel="bi-trade")
    job = fetch_jobs.create_job(
        session,
        institution_id=institution.id,
        external_code="DS_TEST:X",
        attributes={
            "origin": "on_demand",
            "dataset_code": "DS_TEST",
            "codes": {"SERIE": "X"},
            "start": "2000-01-01",
        },
    )
    waiter = fetch_jobs.attach_waiter(session, job, waiter_type="idea", waiter_id="w-1")

    def ingest(*args, **kwargs):
        raise AssertionError("ingest must not run without a connector")

    monkeypatch.setattr(runner, "find_dataset", lambda *args, **kwargs: dataset)
    monkeypatch.setattr(runner, "ingest_series", ingest)

    result = runner.run_job(
        factory(store),
        job.id,
        connector_for=real_connector_for,
        clock=_clock,
        heartbeat_interval=3600,
    )

    reason = "no_connector: no on-demand connector for dataset DS_TEST (channel bi-trade)"
    assert result.outcome == "failed"
    assert result.error == reason
    assert job.status == fetch_jobs.FAILED
    assert job.error_reason == reason
    assert waiter.status == fetch_jobs.DROPPED


def test_heartbeat_beats_while_the_connector_blocks(monkeypatch) -> None:
    store = Store()
    session = factory(store)()
    institution, dataset, job = _make_job(store, session)

    def ingest(session, connector, *, dataset, codes, start, **kwargs):
        time.sleep(0.15)
        return _Ingest()

    _patch(monkeypatch, dataset, ingest)
    result = _run(
        store,
        job,
        _Connector(),
        clock=lambda: datetime.now(UTC),
        heartbeat_interval=0.02,
    )

    assert result.outcome == "completed"
    assert job.started_at is not None and job.heartbeat_at is not None
    assert job.heartbeat_at > job.started_at
    assert not any(thread.name.startswith("fetch-heartbeat") for thread in threading.enumerate())


def test_heartbeat_database_error_is_surfaced(monkeypatch) -> None:
    store = Store()
    session = factory(store)()
    institution, dataset, job = _make_job(store, session)

    def ingest(session, connector, *, dataset, codes, start, **kwargs):
        time.sleep(0.08)
        return _Ingest()

    _patch(monkeypatch, dataset, ingest)

    def build():
        return FakeSession(store, heartbeat_fail=True)

    result = runner.run_job(
        build,
        job.id,
        connector_for=lambda institution, dataset: (_Connector(), {}),
        clock=lambda: datetime.now(UTC),
        heartbeat_interval=0.02,
    )

    assert result.outcome == "completed"
    assert result.heartbeat_error is not None


def test_a_reaped_job_rolls_back_the_ingest(monkeypatch) -> None:
    store = Store()
    session = factory(store)()
    institution, dataset, job = _make_job(store, session)
    started = threading.Event()
    release = threading.Event()
    observed: dict = {}
    sessions: list[FakeSession] = []

    def ingest(session, connector, *, dataset, codes, start, **kwargs):
        started.set()
        release.wait(2)
        observed["called"] = True
        return _Ingest()

    _patch(monkeypatch, dataset, ingest)

    def build():
        made = FakeSession(store)
        sessions.append(made)
        return made

    results: dict = {}

    def target() -> None:
        results["result"] = runner.run_job(
            build,
            job.id,
            connector_for=lambda institution, dataset: (_Connector(), {}),
            clock=lambda: datetime.now(UTC),
            heartbeat_interval=0.01,
        )

    thread = threading.Thread(target=target)
    thread.start()
    assert started.wait(2)

    with factory(store)() as watchdog_session:
        fetch_jobs.fail_if_fetching(
            watchdog_session,
            job.id,
            error_reason=fetch_jobs.HEARTBEAT_LOST_REASON,
            now=datetime.now(UTC),
        )
        watchdog_session.commit()

    release.set()
    thread.join(3)

    assert observed.get("called") is True
    assert results["result"].outcome == "reaped"
    assert job.status == fetch_jobs.FAILED
    assert any(item.rollbacks for item in sessions)
