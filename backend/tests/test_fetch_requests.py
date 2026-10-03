"""Unit tests for on-demand requests (no database, no broker)."""

from __future__ import annotations

import pytest

from app.data import fetch_requests
from app.data.errors import SeriesDefinitionError
from app.data.models import FetchJob, FetchJobWaiter
from tests.fetch_fakes import Store, factory, make_dataset, make_institution


def _install(monkeypatch, institution, dataset) -> None:
    monkeypatch.setattr(
        fetch_requests,
        "find_institution",
        lambda session, code: institution if code == institution.code else None,
    )
    monkeypatch.setattr(
        fetch_requests,
        "find_dataset",
        lambda session, code, dataset_code: (
            dataset if code == institution.code and dataset_code == dataset.external_code else None
        ),
    )
    monkeypatch.setattr(fetch_requests, "find_series", lambda session, code, external: None)


def _request(session, **kwargs):
    return fetch_requests.request_fetch(
        session,
        institution_code="tcmb",
        dataset_code="DS_TEST",
        codes={"SERIE": "X"},
        **kwargs,
    )


def test_new_job_then_attach_to_the_same_active_job(monkeypatch) -> None:
    store = Store()
    institution = make_institution(store)
    dataset = make_dataset(store, institution)
    _install(monkeypatch, institution, dataset)
    session = factory(store)()

    first = _request(session)
    assert first.created is True
    assert first.status == "requested"

    second = _request(session, waiter=("idea", "1"))
    assert second.created is False
    assert second.job_id == first.job_id
    assert len(store.rows[FetchJobWaiter]) == 1


def test_reattaching_the_same_waiter_is_idempotent(monkeypatch) -> None:
    store = Store()
    institution = make_institution(store)
    dataset = make_dataset(store, institution)
    _install(monkeypatch, institution, dataset)
    session = factory(store)()

    first = _request(session, waiter=("idea", "1"))
    _request(session, waiter=("idea", "1"))
    assert first.created is True
    assert len(store.rows[FetchJobWaiter]) == 1


def test_invalid_codes_raise_before_writing(monkeypatch) -> None:
    store = Store()
    institution = make_institution(store)
    dataset = make_dataset(store, institution)
    _install(monkeypatch, institution, dataset)
    session = factory(store)()

    with pytest.raises(SeriesDefinitionError):
        fetch_requests.request_fetch(
            session,
            institution_code="tcmb",
            dataset_code="DS_TEST",
            codes={"SERIE": "NOPE"},
        )
    assert store.rows[FetchJob] == []


def test_unknown_dataset_raises_lookup_error(monkeypatch) -> None:
    store = Store()
    institution = make_institution(store)
    dataset = make_dataset(store, institution)
    _install(monkeypatch, institution, dataset)
    session = factory(store)()

    with pytest.raises(LookupError):
        fetch_requests.request_fetch(
            session,
            institution_code="tcmb",
            dataset_code="MISSING",
            codes={"SERIE": "X"},
        )


def test_dispatch_only_for_a_new_job(monkeypatch) -> None:
    store = Store()
    institution = make_institution(store)
    dataset = make_dataset(store, institution)
    _install(monkeypatch, institution, dataset)
    session = factory(store)()
    dispatched: list[int] = []

    first = fetch_requests.request_and_dispatch(
        session,
        institution_code="tcmb",
        dataset_code="DS_TEST",
        codes={"SERIE": "X"},
        dispatch=dispatched.append,
    )
    second = fetch_requests.request_and_dispatch(
        session,
        institution_code="tcmb",
        dataset_code="DS_TEST",
        codes={"SERIE": "X"},
        dispatch=dispatched.append,
    )

    assert first.created is True
    assert second.created is False
    assert dispatched == [first.job_id]


def test_successful_dispatch_stamps_last_enqueued_at(monkeypatch) -> None:
    store = Store()
    institution = make_institution(store)
    dataset = make_dataset(store, institution)
    _install(monkeypatch, institution, dataset)
    session = factory(store)()

    result = fetch_requests.request_and_dispatch(
        session,
        institution_code="tcmb",
        dataset_code="DS_TEST",
        codes={"SERIE": "X"},
        dispatch=lambda job_id: None,
    )

    jobs = store.rows[FetchJob]
    assert len(jobs) == 1
    assert jobs[0].attributes["dataset_code"] == "DS_TEST"
    assert "last_enqueued_at" in jobs[0].attributes
    assert result.created is True


def test_dispatch_failure_does_not_stamp_last_enqueued_at(monkeypatch) -> None:
    store = Store()
    institution = make_institution(store)
    dataset = make_dataset(store, institution)
    _install(monkeypatch, institution, dataset)
    session = factory(store)()

    def boom(_job_id: int) -> None:
        raise RuntimeError("redis down")

    with pytest.raises(RuntimeError):
        fetch_requests.request_and_dispatch(
            session,
            institution_code="tcmb",
            dataset_code="DS_TEST",
            codes={"SERIE": "X"},
            dispatch=boom,
        )

    jobs = store.rows[FetchJob]
    assert "last_enqueued_at" not in jobs[0].attributes


def test_dispatch_failure_keeps_the_committed_request(monkeypatch) -> None:
    store = Store()
    institution = make_institution(store)
    dataset = make_dataset(store, institution)
    _install(monkeypatch, institution, dataset)
    session = factory(store)()

    def boom(_job_id: int) -> None:
        raise RuntimeError("redis down")

    with pytest.raises(RuntimeError):
        fetch_requests.request_and_dispatch(
            session,
            institution_code="tcmb",
            dataset_code="DS_TEST",
            codes={"SERIE": "X"},
            dispatch=boom,
        )

    jobs = store.rows[FetchJob]
    assert len(jobs) == 1
    assert jobs[0].status == "requested"
    assert store.commits >= 1
