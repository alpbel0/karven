"""Unit tests for agent service helpers: prompt seeding and the failure list."""

from __future__ import annotations

from types import SimpleNamespace

from app.data.models import FetchFailure
from app.fetching.agent import service
from app.fetching.agent.schema import ASK_PROMPT_KEY, SYSTEM_PROMPT_KEY
from app.prompts.errors import PromptNotFoundError
from tests.fetch_agent_fakes import Store, factory, make_failed_job, make_institution


def test_ensure_default_prompt_seeds_both_files_when_none_active(monkeypatch) -> None:
    store = Store()
    session = factory(store)()
    added: list[str] = []
    activated: list[tuple[str, int]] = []

    def fake_get(_session, key):
        raise PromptNotFoundError(key)

    def fake_add(_session, *, key, body, note=None):
        assert body.strip()
        added.append(key)
        return SimpleNamespace(version=1)

    def fake_activate(_session, *, key, version):
        activated.append((key, version))

    monkeypatch.setattr(service, "get_active", fake_get)
    monkeypatch.setattr(service, "add_version", fake_add)
    monkeypatch.setattr(service, "activate", fake_activate)

    service.ensure_default_prompt(session)

    assert added == [SYSTEM_PROMPT_KEY, ASK_PROMPT_KEY]
    assert activated == [(SYSTEM_PROMPT_KEY, 1), (ASK_PROMPT_KEY, 1)]


def test_list_failures_returns_plain_dicts() -> None:
    store = Store()
    institution = make_institution(store, code="tcmb")
    job = make_failed_job(store, institution, external_code="DS:X")
    store.rows[FetchFailure].append(
        FetchFailure(
            id=1,
            fetch_job_id=job.id,
            round=1,
            institution_id=institution.id,
            external_code="DS:X",
            dataset_code="DS",
            codes={"SERIE": "X"},
            error_reason="boom",
            category="transient",
            outcome="recorded",
            diagnosis="geçici hata",
            suggestion="tekrar dene",
            alternatives=[{"institution": "tcmb", "dataset": "DS", "codes": {}, "note": "x"}],
            created_at=None,
        )
    )
    session = factory(store)()

    rows = service.list_failures(session, limit=10)
    assert rows[0]["institution"] == "tcmb"
    assert rows[0]["category"] == "transient"
    assert rows[0]["alternatives"][0]["note"] == "x"
    assert rows[0]["created_at"] is None
    assert service.list_failures(session, outcome="agent_error") == []
