"""Unit tests for ``ask_fetch_agent`` (read-only, typed answer)."""

from __future__ import annotations

import pytest

from app.data.models import FetchFailure, FetchJob
from app.fetching.agent import service
from app.llm.errors import LLMOutputError
from app.prompts.service import ActivePromptView
from tests.fetch_agent_fakes import ScriptedChatClient, Store, factory, make_institution


@pytest.fixture(autouse=True)
def _patch_prompt(monkeypatch):
    monkeypatch.setattr(
        service,
        "get_active",
        lambda session, key: ActivePromptView(key, 2, "ASK PROMPT", "checksum"),
    )


def _answer() -> dict:
    return {
        "answer": "Eylül verisi henüz yok; seri Ağustos 2026'ya kadar.",
        "data_status": {
            "available": True,
            "coverage_start": "2000-01-01",
            "coverage_end": "2026-08-01",
            "latest_period": "2026-08",
            "note": "Eylül henüz yayımlanmadı",
        },
        "suggestion": "Ekim başında tekrar sor.",
    }


def test_ask_returns_typed_answer_and_writes_nothing() -> None:
    store = Store()
    make_institution(store, code="tcmb")
    client = ScriptedChatClient(_answer(), provider="evren", model="deepseek-v4.1-flash")

    answer = service.ask_fetch_agent(
        "Haber Eylül diyor ama bende Ağustos var; Eylül var mı?",
        {"dataset": "TP.CLI2"},
        session_factory=factory(store),
        chat_client_factory=lambda: client,
    )

    assert answer.answer.startswith("Eylül verisi")
    assert answer.data_status["available"] is True
    assert answer.data_status["latest_period"] == "2026-08"
    assert answer.suggestion == "Ekim başında tekrar sor."
    assert answer.provider == "evren"
    assert client.closed is True
    assert store.rows[FetchJob] == []
    assert store.rows[FetchFailure] == []


def test_ask_rejects_a_non_string_answer() -> None:
    store = Store()
    client = ScriptedChatClient({"answer": 5, "data_status": {}, "suggestion": None})

    with pytest.raises(LLMOutputError):
        service.ask_fetch_agent(
            "soru",
            session_factory=factory(store),
            chat_client_factory=lambda: client,
        )
    assert store.rows[FetchJob] == []
