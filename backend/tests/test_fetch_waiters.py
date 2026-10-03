"""Unit tests for waiter parking and resolution in ``app.data.fetch_jobs``."""

from __future__ import annotations

from datetime import UTC, datetime

from app.data import fetch_jobs
from tests.fetch_fakes import Store, factory, make_institution

NOW = datetime(2026, 10, 3, 12, tzinfo=UTC)


def _job(store: Store, session):
    institution = make_institution(store)
    return fetch_jobs.create_job(session, institution_id=institution.id, external_code="DS:X")


def test_attach_waiter_is_idempotent_and_queryable() -> None:
    store = Store()
    session = factory(store)()
    job = _job(store, session)

    first = fetch_jobs.attach_waiter(session, job, waiter_type="idea", waiter_id="1", now=NOW)
    again = fetch_jobs.attach_waiter(session, job, waiter_type="idea", waiter_id="1")
    assert first is again
    assert first.status == fetch_jobs.WAITING
    assert fetch_jobs.waiters_for(session, "idea", "1") == [first]
    assert fetch_jobs.ready_waiters(session, "idea") == []


def test_mark_completed_resolves_waiters_ready() -> None:
    store = Store()
    session = factory(store)()
    job = _job(store, session)
    idea = fetch_jobs.attach_waiter(session, job, waiter_type="idea", waiter_id="1")
    visual = fetch_jobs.attach_waiter(session, job, waiter_type="visual", waiter_id="2")

    fetch_jobs.mark_completed(session, job, now=NOW)

    assert idea.status == fetch_jobs.READY
    assert idea.resolved_at == NOW
    assert visual.status == fetch_jobs.READY
    assert fetch_jobs.ready_waiters(session, "idealessonly") == []
    assert {row.id for row in fetch_jobs.ready_waiters(session, "idea")} == {idea.id}


def test_mark_failed_drops_waiters() -> None:
    store = Store()
    session = factory(store)()
    job = _job(store, session)
    waiter = fetch_jobs.attach_waiter(session, job, waiter_type="idea", waiter_id="1")

    fetch_jobs.mark_failed(session, job, error_reason="boom", now=NOW)

    assert waiter.status == fetch_jobs.DROPPED
    assert waiter.resolved_at == NOW
    assert fetch_jobs.ready_waiters(session, "idea") == []


def test_fail_if_fetching_skips_a_terminal_job() -> None:
    store = Store()
    session = factory(store)()
    institution = make_institution(store)
    job = fetch_jobs.create_job(session, institution_id=institution.id, external_code="DS:X")
    fetch_jobs.mark_completed(session, job, now=NOW)

    assert fetch_jobs.fail_if_fetching(session, job.id, error_reason="late") is None
    assert job.status == fetch_jobs.COMPLETED
    assert job.error_reason is None


def test_fail_if_fetching_records_only_while_fetching() -> None:
    store = Store()
    session = factory(store)()
    institution = make_institution(store)
    job = fetch_jobs.create_job(session, institution_id=institution.id, external_code="DS:X")
    fetch_jobs.mark_fetching(session, job, now=NOW)

    failed = fetch_jobs.fail_if_fetching(
        session, job.id, error_reason=fetch_jobs.HEARTBEAT_LOST_REASON, now=NOW
    )
    assert failed is not None
    assert failed.status == fetch_jobs.FAILED
    assert failed.error_reason == fetch_jobs.HEARTBEAT_LOST_REASON


def test_mark_status_without_waiters_still_works() -> None:
    """The plain ``mark_status`` path keeps working when there are no waiters."""
    store = Store()
    session = factory(store)()
    institution = make_institution(store)
    job = fetch_jobs.create_job(session, institution_id=institution.id, external_code="DS:X")
    fetch_jobs.mark_status(session, job, fetch_jobs.COMPLETED, now=NOW)
    assert job.status == fetch_jobs.COMPLETED
    assert job.finished_at == NOW
