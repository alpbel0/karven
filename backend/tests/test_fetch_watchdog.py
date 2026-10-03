"""Unit tests for the on-demand watchdog (fake session, no scheduler jobs)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.data import fetch_jobs
from app.data.fetch_requests import AGENT_RETRY, ON_DEMAND
from app.fetching.watchdog import reap_stalled
from tests.fetch_fakes import Store, factory, make_institution

NOW = datetime(2026, 10, 3, 12, tzinfo=UTC)
STALL = timedelta(minutes=5)
OLD = NOW - timedelta(minutes=10)
FRESH = NOW - timedelta(minutes=1)


def _job(
    store,
    session,
    *,
    status,
    origin=ON_DEMAND,
    heartbeat_at=None,
    requested_at=None,
    attributes=None,
):
    institution = make_institution(store)
    job = fetch_jobs.create_job(
        session,
        institution_id=institution.id,
        external_code=f"DS:{origin}",
        attributes={"origin": origin, **(attributes or {})},
    )
    job.status = status
    job.heartbeat_at = heartbeat_at
    job.requested_at = requested_at
    return job


def test_stalled_fetching_job_is_reaped() -> None:
    store = Store()
    session = factory(store)()
    job = _job(store, session, status=fetch_jobs.FETCHING, heartbeat_at=OLD)
    waiter = fetch_jobs.attach_waiter(session, job, waiter_type="idea", waiter_id="1")

    report = reap_stalled(session, now=NOW, stall_after=STALL)

    assert report.reaped == [job.id]
    assert job.status == fetch_jobs.FAILED
    assert job.error_reason == fetch_jobs.HEARTBEAT_LOST_REASON
    assert waiter.status == fetch_jobs.DROPPED


def test_fresh_heartbeat_is_untouched() -> None:
    store = Store()
    session = factory(store)()
    job = _job(store, session, status=fetch_jobs.FETCHING, heartbeat_at=FRESH)

    report = reap_stalled(session, now=NOW, stall_after=STALL)

    assert report.reaped == []
    assert job.status == fetch_jobs.FETCHING


def test_scheduler_origin_job_is_never_reaped() -> None:
    store = Store()
    session = factory(store)()
    job = _job(
        store,
        session,
        status=fetch_jobs.FETCHING,
        origin="core_refresh",
        heartbeat_at=OLD,
    )

    report = reap_stalled(session, now=NOW, stall_after=STALL)

    assert report.reaped == []
    assert job.status == fetch_jobs.FETCHING


def test_stalled_agent_retry_job_is_reaped() -> None:
    store = Store()
    session = factory(store)()
    job = _job(
        store,
        session,
        status=fetch_jobs.FETCHING,
        origin=AGENT_RETRY,
        heartbeat_at=OLD,
        attributes={"agent_round": 1, "parent_job_id": 99},
    )

    report = reap_stalled(session, now=NOW, stall_after=STALL)

    assert report.reaped == [job.id]
    assert job.status == fetch_jobs.FAILED


def test_requested_agent_retry_job_is_requeued() -> None:
    store = Store()
    session = factory(store)()
    job = _job(
        store,
        session,
        status=fetch_jobs.REQUESTED,
        origin=AGENT_RETRY,
        requested_at=OLD,
    )

    report = reap_stalled(session, now=NOW, stall_after=STALL)

    assert report.requeued == [job.id]


def test_requested_on_demand_job_is_requeued() -> None:
    store = Store()
    session = factory(store)()
    job = _job(
        store,
        session,
        status=fetch_jobs.REQUESTED,
        requested_at=OLD,
    )

    report = reap_stalled(session, now=NOW, stall_after=STALL)

    assert report.requeued == [job.id]
    assert job.status == fetch_jobs.REQUESTED


def test_fresh_requested_job_is_not_requeued() -> None:
    store = Store()
    session = factory(store)()
    job = _job(store, session, status=fetch_jobs.REQUESTED, requested_at=FRESH)

    report = reap_stalled(session, now=NOW, stall_after=STALL)

    assert report.requeued == []
    assert job.status == fetch_jobs.REQUESTED


def test_fresh_last_enqueued_at_suppresses_the_requeue() -> None:
    store = Store()
    session = factory(store)()
    job = _job(
        store,
        session,
        status=fetch_jobs.REQUESTED,
        requested_at=OLD,
        attributes={"last_enqueued_at": NOW.isoformat()},
    )

    report = reap_stalled(session, now=NOW, stall_after=STALL)

    assert report.requeued == []
    assert job.status == fetch_jobs.REQUESTED


def test_requested_job_is_requeued_once_then_suppressed_until_stall_passes() -> None:
    store = Store()
    session = factory(store)()
    job = _job(store, session, status=fetch_jobs.REQUESTED, requested_at=OLD)

    first = reap_stalled(session, now=NOW, stall_after=STALL)
    assert first.requeued == [job.id]

    # The watchdog stamps a successful hand-off; the next tick must not repeat.
    fetch_jobs.stamp_enqueued(session, job.id, now=NOW)
    session.commit()
    second = reap_stalled(session, now=NOW, stall_after=STALL)
    assert second.requeued == []

    # Once last_enqueued_at ages past the threshold it is due again.
    later = NOW + STALL + timedelta(minutes=1)
    third = reap_stalled(factory(store)(), now=later, stall_after=STALL)
    assert third.requeued == [job.id]


def test_stamp_enqueued_merges_without_clobbering_attributes() -> None:
    store = Store()
    session = factory(store)()
    job = _job(
        store,
        session,
        status=fetch_jobs.REQUESTED,
        attributes={"dataset_code": "DS_TEST", "codes": {"SERIE": "X"}},
    )

    stamped = fetch_jobs.stamp_enqueued(session, job.id, now=NOW)

    assert stamped is job
    assert stamped.attributes["dataset_code"] == "DS_TEST"
    assert stamped.attributes["codes"] == {"SERIE": "X"}
    assert stamped.attributes["last_enqueued_at"] == NOW.isoformat()


def test_stamp_enqueued_missing_job_is_none() -> None:
    store = Store()
    session = factory(store)()

    assert fetch_jobs.stamp_enqueued(session, 999, now=NOW) is None


def test_a_completed_job_is_never_overwritten() -> None:
    store = Store()
    session = factory(store)()
    job = _job(store, session, status=fetch_jobs.COMPLETED, heartbeat_at=OLD)

    report = reap_stalled(session, now=NOW, stall_after=STALL)

    assert report.reaped == []
    assert job.status == fetch_jobs.COMPLETED
    assert job.error_reason is None
