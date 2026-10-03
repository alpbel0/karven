"""Unit tests for the data-fetch agent diagnose flow (fake session, no network)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.data.models import FetchFailure, FetchJob, FetchJobWaiter
from app.fetching.agent import service
from app.llm.errors import LLMOutputError
from app.prompts.service import ActivePromptView
from tests.fetch_agent_fakes import (
    ScriptedChatClient,
    Store,
    add_waiter,
    factory,
    make_dataset,
    make_failed_job,
    make_institution,
)
from tests.llm_helpers import tool_call


@pytest.fixture(autouse=True)
def _patch_prompt(monkeypatch):
    monkeypatch.setattr(
        service,
        "get_active",
        lambda session, key: ActivePromptView(key, 1, "SYSTEM PROMPT", "checksum"),
    )


def _final(category: str, *, retry=None, alternatives=None) -> dict:
    return {
        "category": category,
        "diagnosis": "teşhis",
        "suggestion": "öneri",
        "retry": retry,
        "alternatives": alternatives if alternatives is not None else [],
    }


def _catalog() -> tuple[Store, object, object, object]:
    store = Store()
    institution = make_institution(store, code="tcmb")
    failed_dataset = make_dataset(
        store,
        institution,
        external_code="TP_DK",
        series_codes=("X",),
    )
    retry_dataset = make_dataset(
        store,
        institution,
        external_code="TP_CLI",
        dimension_code="SERIE",
        series_codes=("TP.CLI2",),
    )
    return store, institution, failed_dataset, retry_dataset


def _run(store, job_id, final, *, max_rounds=2, dispatch=None, **client_kwargs):
    client = ScriptedChatClient(final, **client_kwargs)
    result = service.diagnose_job(
        factory(store),
        job_id,
        chat_client_factory=lambda: client,
        max_rounds=max_rounds,
        dispatch=dispatch,
        clock=lambda: datetime(2026, 10, 3, 12, tzinfo=UTC),
    )
    return result, client


def _failures(store) -> list[FetchFailure]:
    return store.rows[FetchFailure]


def _jobs(store) -> list[FetchJob]:
    return store.rows[FetchJob]


def test_not_in_source_is_recorded() -> None:
    store, institution, _failed, _retry = _catalog()
    job = make_failed_job(store, institution, codes={"SERIE": "X"})

    result, client = _run(store, job.id, _final("not_in_source"))

    assert result.outcome == "recorded"
    assert result.category == "not_in_source"
    assert client.closed is True
    rows = _failures(store)
    assert len(rows) == 1
    assert rows[0].outcome == "recorded"
    assert rows[0].category == "not_in_source"
    assert rows[0].retry_job_id is None
    assert rows[0].prompt_version == 1
    assert rows[0].llm_provider == "fake"


def test_no_connector_never_retries_even_with_a_proposal() -> None:
    store, institution, _failed, retry_dataset = _catalog()
    job = make_failed_job(store, institution, codes={"SERIE": "X"})
    retry = {"institution": "tcmb", "dataset": "TP_CLI", "codes": {"SERIE": "TP.CLI2"}}

    result, _client = _run(store, job.id, _final("no_connector", retry=retry))

    assert result.outcome == "recorded"
    assert len(_jobs(store)) == 1  # only the failed job; no retry opened
    row = _failures(store)[0]
    assert "yeniden denemeye uygun değil" in (row.suggestion or "")


def test_retry_executed_with_corrected_codes_and_waiters() -> None:
    store, institution, _failed, retry_dataset = _catalog()
    job = make_failed_job(
        store,
        institution,
        external_code="TP_DK:X",
        codes={"SERIE": "X"},
        error_reason="no_connector: no on-demand connector",
    )
    old_waiter = add_waiter(store, job, waiter_type="idea", waiter_id="42", status="dropped")
    retry = {"institution": "tcmb", "dataset": "TP_CLI", "codes": {"SERIE": "TP.CLI2"}}
    dispatched: list[int] = []

    result, _client = _run(
        store, job.id, _final("bad_request", retry=retry), dispatch=dispatched.append
    )

    assert result.outcome == "retry_requested"
    jobs = _jobs(store)
    assert len(jobs) == 2
    new_job = jobs[-1]
    assert new_job.id == result.retry_job_id
    assert new_job.external_code == "TP_CLI:TP.CLI2"
    assert new_job.attributes["origin"] == "agent_retry"
    assert new_job.attributes["agent_round"] == 1
    assert new_job.attributes["parent_job_id"] == job.id
    assert "last_enqueued_at" in new_job.attributes
    assert dispatched == [new_job.id]

    new_waiters = [row for row in store.rows[FetchJobWaiter] if row.fetch_job_id == new_job.id]
    assert [(row.waiter_type, row.waiter_id, row.status) for row in new_waiters] == [
        ("idea", "42", "waiting")
    ]
    assert old_waiter.status == "dropped"

    row = _failures(store)[0]
    assert row.outcome == "retry_requested"
    assert row.retry_job_id == new_job.id


def test_retry_attaching_to_an_active_job_is_not_double_dispatched() -> None:
    from app.data.fetch_requests import request_fetch

    store, institution, _failed, _retry = _catalog()
    session = factory(store)()
    active = request_fetch(
        session,
        institution_code="tcmb",
        dataset_code="TP_CLI",
        codes={"SERIE": "TP.CLI2"},
    )
    session.commit()
    job = make_failed_job(
        store,
        institution,
        external_code="TP_DK:X",
        dataset_code="TP_DK",
        codes={"SERIE": "X"},
    )
    add_waiter(store, job, waiter_type="visual", waiter_id="7", status="dropped")
    retry = {"institution": "tcmb", "dataset": "TP_CLI", "codes": {"SERIE": "TP.CLI2"}}
    dispatched: list[int] = []

    result, _client = _run(
        store, job.id, _final("bad_request", retry=retry), dispatch=dispatched.append
    )

    assert result.outcome == "retry_requested"
    assert result.retry_job_id == active.job_id
    assert dispatched == []  # attached, never re-enqueued
    target = next(row for row in _jobs(store) if row.id == active.job_id)
    assert target.attributes["origin"] == "on_demand"  # not overwritten
    waiters = [row for row in store.rows[FetchJobWaiter] if row.fetch_job_id == active.job_id]
    assert [(row.waiter_type, row.waiter_id, row.status) for row in waiters] == [
        ("visual", "7", "waiting")
    ]


def test_transient_identical_retry_is_allowed() -> None:
    store, institution, _failed, _retry = _catalog()
    job = make_failed_job(
        store, institution, external_code="TP_DK:X", dataset_code="TP_DK", codes={"SERIE": "X"}
    )
    retry = {"institution": "tcmb", "dataset": "TP_DK", "codes": {"SERIE": "X"}}

    alternative = {
        "institution": "tcmb",
        "dataset": "TP_DK",
        "codes": {"SERIE": "X"},
        "note": "aynı seri",
    }
    result, _client = _run(
        store, job.id, _final("transient", retry=retry, alternatives=[alternative])
    )

    assert result.outcome == "retry_requested"
    assert len(_jobs(store)) == 2


def test_identical_retry_rejected_for_bad_request() -> None:
    store, institution, _failed, _retry = _catalog()
    job = make_failed_job(
        store, institution, external_code="TP_DK:X", dataset_code="TP_DK", codes={"SERIE": "X"}
    )
    retry = {"institution": "tcmb", "dataset": "TP_DK", "codes": {"SERIE": "X"}}

    result, _client = _run(store, job.id, _final("bad_request", retry=retry))

    assert result.outcome == "recorded"
    assert len(_jobs(store)) == 1
    assert "aynı istek" in (_failures(store)[0].suggestion or "")


def test_round_limit_rejects_retry() -> None:
    store, institution, _failed, _retry = _catalog()
    job = make_failed_job(store, institution, codes={"SERIE": "X"}, agent_round=1)
    retry = {"institution": "tcmb", "dataset": "TP_CLI", "codes": {"SERIE": "TP.CLI2"}}

    result, _client = _run(store, job.id, _final("bad_request", retry=retry), max_rounds=2)

    assert result.outcome == "recorded"
    assert len(_jobs(store)) == 1
    assert "tur sınırı" in (_failures(store)[0].suggestion or "")


def test_uncatalogued_retry_dataset_rejected() -> None:
    store, institution, _failed, _retry = _catalog()
    job = make_failed_job(store, institution, codes={"SERIE": "X"})
    retry = {"institution": "tcmb", "dataset": "MISSING", "codes": {"SERIE": "X"}}

    result, _client = _run(store, job.id, _final("bad_request", retry=retry))

    assert result.outcome == "recorded"
    assert "kataloğunda yok" in (_failures(store)[0].suggestion or "")


def test_invalid_retry_codes_rejected() -> None:
    store, institution, _failed, _retry = _catalog()
    job = make_failed_job(store, institution, codes={"SERIE": "X"})
    retry = {"institution": "tcmb", "dataset": "TP_CLI", "codes": {"SERIE": "NOPE"}}

    result, _client = _run(store, job.id, _final("bad_request", retry=retry))

    assert result.outcome == "recorded"
    assert "kodlar geçersiz" in (_failures(store)[0].suggestion or "")


def test_second_diagnose_is_idempotent() -> None:
    store, institution, _failed, _retry = _catalog()
    job = make_failed_job(store, institution, codes={"SERIE": "X"})

    first, _c1 = _run(store, job.id, _final("not_in_source"))
    second, client2 = _run(store, job.id, _final("not_in_source"))

    assert first.outcome == "recorded"
    assert second.outcome == "skipped"
    assert second.reason == "already diagnosed"
    assert client2.calls == []  # the LLM is never called for a re-delivery
    assert len(_failures(store)) == 1


def test_agent_error_is_recorded_and_not_retried() -> None:
    store, institution, _failed, _retry = _catalog()
    job = make_failed_job(store, institution, codes={"SERIE": "X"})

    result, client = _run(
        store, job.id, _final("transient"), raise_exc=LLMOutputError("provider exploded")
    )

    assert result.outcome == "agent_error"
    assert client.closed is True
    assert len(_jobs(store)) == 1
    row = _failures(store)[0]
    assert row.outcome == "agent_error"
    assert row.category == "unknown"
    assert row.diagnosis is None
    assert "LLMOutputError" in (row.agent_error or "")


def test_invalid_agent_output_is_agent_error() -> None:
    store, institution, _failed, _retry = _catalog()
    job = make_failed_job(store, institution, codes={"SERIE": "X"})

    result, _client = _run(store, job.id, _final("not-a-category"))

    assert result.outcome == "agent_error"
    assert _failures(store)[0].category == "unknown"


def test_concurrent_duplicate_rolls_back_the_retry_and_skips_dispatch() -> None:
    store, institution, _failed, _retry = _catalog()
    job = make_failed_job(
        store, institution, external_code="TP_DK:X", dataset_code="TP_DK", codes={"SERIE": "X"}
    )
    retry = {"institution": "tcmb", "dataset": "TP_CLI", "codes": {"SERIE": "TP.CLI2"}}
    dispatched: list[int] = []
    client = ScriptedChatClient(_final("bad_request", retry=retry))

    result = service.diagnose_job(
        factory(store, fail_failure_flush=True),
        job.id,
        chat_client_factory=lambda: client,
        max_rounds=2,
        dispatch=dispatched.append,
        clock=lambda: datetime(2026, 10, 3, 12, tzinfo=UTC),
    )

    assert result.outcome == "skipped"
    assert result.reason == "already diagnosed (concurrent delivery)"
    assert dispatched == []


def test_origin_filter_skips_non_on_demand() -> None:
    store, institution, _failed, _retry = _catalog()
    job = make_failed_job(store, institution, origin="core_refresh")

    result, client = _run(store, job.id, _final("transient"))

    assert result.outcome == "skipped"
    assert client.calls == []
    assert _failures(store) == []


def test_skips_a_job_that_is_not_failed() -> None:
    store, institution, _failed, _retry = _catalog()
    job = make_failed_job(store, institution, origin="core_refresh")
    job.status = "completed"

    result, _client = _run(store, job.id, _final("transient"))

    assert result.outcome == "skipped"
    assert _failures(store) == []


def test_tools_run_inside_the_loop() -> None:
    store, institution, _failed, _retry = _catalog()
    job = make_failed_job(store, institution, codes={"SERIE": "X"})

    result, client = _run(
        store,
        job.id,
        _final("transient"),
        tool_calls=[tool_call("call_1", "search_catalog", {"query": "TP_DK"})],
    )

    assert result.outcome == "recorded"
    tool_messages = [
        message
        for call in client.calls
        for message in call["messages"]
        if message.get("role") == "tool"
    ]
    assert any("TP_DK" in message["content"] for message in tool_messages)
