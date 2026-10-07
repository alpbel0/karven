"""Unit tests for the first agent's final parser and run flow (Task 3.3)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.first_agent import service
from app.first_agent.ports import GraphAgentAnswer
from app.first_agent.schema import parse_selection
from app.first_agent.state import MAX_TOOL_CALLS, RunState
from app.llm.errors import LLMOutputError
from app.prompts.service import ActivePromptView
from tests.first_agent_fakes import (
    FakeCatalog,
    RecordingGraphAgent,
    ScriptedClient,
    Turn,
    call,
    make_deps,
    recipe,
    status_result,
    tools_for,
)

# --- parse_selection --------------------------------------------------------


def _state_with_candidates():
    catalog = FakeCatalog(
        {
            "FUEL": status_result("tuik", "FUEL", "Benzin"),
            "USD": status_result("tcmb", "USD", "USD/TRY", measure_type="fiyat_kur"),
            "CPI": status_result("tuik", "CPI", "TÜFE"),
            "RENT": status_result("tuik", "RENT", "Kira"),
        },
        tags={"CPI": {"fiyat"}, "RENT": {"fiyat"}},
    )
    state, _deps, tools = tools_for(catalog, graph_agent=RecordingGraphAgent())
    tools["find_series"][1](request="brent")
    tools["add_visual"][1](title="Benzin", reason="r", series=recipe("FUEL"))  # visual 1
    tools["add_visual"][1](title="Brent", reason="r", series={"missing": "Brent"})  # visual 2 gap
    args = {
        "title": "Kur -> fiyat",
        "target": recipe("CPI"),
        "drivers": [recipe("USD")],
        "transform": "annual_pct_change",
        "mechanism": "m",
    }
    tools["add_idea"][1](**args)  # idea 1 ready
    taut = {**args, "target": recipe("RENT"), "drivers": [recipe("CPI")]}
    tools["add_idea"][1](**taut)  # idea 2 tautological
    return state, tools


def _final(**kwargs):
    base = {"summary": "Özet.", "selected_visuals": [], "selected_ideas": []}
    base.update(kwargs)
    return base


def test_selection_accepts_a_candidate_and_an_answered_idea() -> None:
    state, tools = _state_with_candidates()
    tools["ask_graph_agent"][1](idea_id=1)
    selection = parse_selection(
        _final(
            selected_visuals=[{"id": 1, "reason": "çiz"}],
            selected_ideas=[{"id": 1, "reason": "test edildi"}],
        ),
        state,
    )
    assert selection.visuals == {1: "çiz"}
    assert selection.ideas == {1: "test edildi"}


@pytest.mark.parametrize(
    "final",
    [
        "not an object",
        _final(summary=" "),
        _final(selected_visuals=[{"id": 9, "reason": "x"}]),
        _final(selected_visuals=[{"id": 2, "reason": "gap has no series"}]),
        _final(selected_visuals=[{"id": 1, "reason": ""}]),
        _final(selected_visuals=[{"id": 1, "reason": "a"}, {"id": 1, "reason": "b"}]),
        _final(selected_visuals=[{"id": True, "reason": "a"}]),
        _final(selected_ideas=[{"id": 2, "reason": "tautological"}]),
        _final(selected_ideas=[{"id": 1, "reason": "never asked the graph agent"}]),
        _final(selected_ideas="x"),
    ],
)
def test_selection_rejects_what_the_run_did_not_register(final) -> None:
    state, _tools = _state_with_candidates()
    with pytest.raises(LLMOutputError):
        parse_selection(final, state)


def test_selection_rejects_an_idea_whose_graph_answer_is_parked() -> None:
    state, tools = _state_with_candidates()
    tools_with_parked = tools_for(
        FakeCatalog(
            {
                "USD": status_result("tcmb", "USD", "USD/TRY"),
                "CPI": status_result("tuik", "CPI", "TÜFE"),
            }
        ),
        graph_agent=RecordingGraphAgent(GraphAgentAnswer(status="parked", summary="veri yok")),
    )
    parked_state, _deps, parked_tools = tools_with_parked
    parked_tools["add_idea"][1](
        title="t",
        target=recipe("CPI"),
        drivers=[recipe("USD")],
        transform="annual_pct_change",
        mechanism="m",
    )
    parked_tools["ask_graph_agent"][1](idea_id=1)
    with pytest.raises(LLMOutputError):
        parse_selection(_final(selected_ideas=[{"id": 1, "reason": "x"}]), parked_state)
    assert state is not parked_state


# --- run_first_agent --------------------------------------------------------


class _Session:
    def __init__(self, news) -> None:
        self.news = news
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, model, key):
        return self.news if self.news is not None and self.news.id == key else None

    def commit(self) -> None:
        self.commits += 1


def _news(**overrides):
    base = {
        "id": 7,
        "source": "haberturk",
        "title": "Benzine zam geldi",
        "published_at": None,
        "content_text": "Benzine litre başına yüzde 4,2 zam geldi.",
        "text_status": "ok",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def saved(monkeypatch):
    records = []
    monkeypatch.setattr(
        service, "_prompt_view", lambda session, key: ActivePromptView(key, 3, "SYSTEM", "sum")
    )

    def fake_save(session, state, meta):
        records.append((state, meta))
        return 99

    monkeypatch.setattr(service, "save_run", fake_save)
    return records


def _run(client, catalog=None, news=None, graph=None):
    catalog = catalog or FakeCatalog(
        {
            "FUEL": status_result("tuik", "FUEL", "Benzin fiyatı", measure_type="fiyat_kur"),
            "USD": status_result("tcmb", "USD", "USD/TRY", measure_type="fiyat_kur"),
            "CPI": status_result("tuik", "CPI", "TÜFE"),
        }
    )
    closed = []
    deps = make_deps(catalog, graph_agent=graph or RecordingGraphAgent())
    return service.run_first_agent(
        7,
        session_factory=lambda: _Session(news if news is not None else _news()),
        chat_client_factory=lambda: client,
        deps_factory=lambda factory: (deps, lambda: closed.append(True)),
    ), closed


def _benzin_turns():
    return [
        Turn(
            tool_calls=[
                call(
                    "add_visual",
                    "a",
                    title="Benzin fiyatı",
                    reason="Haber benzin zammını anlatıyor.",
                    series=recipe("FUEL"),
                    news_value={"text": "yüzde 4,2 zam", "period": "Eylül 2026"},
                ),
                call(
                    "add_idea",
                    "b",
                    title="Kur -> fiyatlar",
                    target=recipe("CPI"),
                    drivers=[recipe("USD")],
                    transform="annual_pct_change",
                    mechanism="Kur maliyetleri yükseltir.",
                ),
            ]
        ),
        Turn(tool_calls=[call("ask_graph_agent", "c", idea_id=1)]),
        Turn(content="hazır"),
    ]


def test_run_records_candidates_and_the_selection(saved) -> None:
    client = ScriptedClient(
        _benzin_turns(),
        [
            {
                "summary": "Benzin zammı ve kur ilişkisi.",
                "selected_visuals": [{"id": 1, "reason": "haberin kendisi"}],
                "selected_ideas": [{"id": 1, "reason": "graph cevapladı"}],
            }
        ],
    )
    result, closed = _run(client)

    assert result.status == "ok"
    assert result.run_id == 99
    assert (result.visuals, result.ideas, result.catalog_gaps) == (1, 1, 0)
    assert result.selected_visuals == [1]
    assert result.selected_ideas == [1]
    assert result.tool_calls == 3
    assert result.llm_attempts == 4
    assert closed == [True]
    assert client.closed is True

    state, meta = saved[0]
    assert meta.prompt_key == "first_agent.system"
    assert meta.prompt_version == 3
    assert meta.usage == {
        "prompt_tokens": 40,
        "completion_tokens": 20,
        "tool_counts": {"add_visual": 1, "add_idea": 1, "ask_graph_agent": 1},
    }
    assert state.visuals[0].selected is True
    assert state.ideas[0].selected is True
    assert state.ideas[0].graph["status"] == "tested"
    # The agent was given the news text and the system prompt.
    first = client.requests[0]["messages"]
    assert first[0] == {"role": "system", "content": "SYSTEM"}
    assert "yüzde 4,2 zam" in first[1]["content"]
    assert "Benzine zam geldi" in first[1]["content"]


def test_an_empty_answer_is_retried_twice_then_succeeds(saved) -> None:
    client = ScriptedClient(
        [Turn(content=None), Turn(content=""), Turn(content="tamam")],
        [_final(summary="Hiçbir şey çıkmadı.")],
    )
    result, _closed = _run(client)
    assert result.status == "ok"
    assert result.llm_attempts == 4  # 3 requests for the first turn + the final one
    assert result.selected_visuals == [] and result.selected_ideas == []


def test_three_empty_answers_end_the_run_as_an_agent_error(saved) -> None:
    client = ScriptedClient([Turn(content="x")], [None, None, None])
    result, _closed = _run(client)
    assert result.status == "agent_error"
    assert "no JSON" in result.error
    assert result.llm_attempts == 4
    assert saved[0][1].status == "agent_error"


def test_an_invalid_selection_is_an_agent_error_but_keeps_the_candidates(saved) -> None:
    client = ScriptedClient(
        _benzin_turns(),
        [_final(selected_visuals=[{"id": 5, "reason": "olmayan"}])],
    )
    result, _closed = _run(client)
    assert result.status == "agent_error"
    assert "never registered" in result.error
    state, meta = saved[0]
    assert len(state.visuals) == 1 and len(state.ideas) == 1
    assert meta.selection == {}


def test_a_crashing_client_is_recorded_and_the_clients_are_closed(saved) -> None:
    class Boom:
        closed = False

        def complete(self, *args, **kwargs):
            raise RuntimeError("provider down")

        def close(self):
            self.closed = True

    client = Boom()
    result, closed = _run(client)
    assert result.status == "agent_error"
    assert "provider down" in result.error
    assert client.closed is True
    assert closed == [True]


def test_the_tool_call_limit_is_sixty() -> None:
    assert MAX_TOOL_CALLS == 60
    assert RunState(news_id=1).visuals == []


def test_news_without_usable_text_raises_and_records_nothing(saved) -> None:
    client = ScriptedClient([], [])
    with pytest.raises(ValueError, match="no usable text"):
        _run(client, news=_news(text_status="no_text", content_text=None))
    with pytest.raises(ValueError, match="does not exist"):
        service.run_first_agent(
            8,
            session_factory=lambda: _Session(_news()),
            chat_client_factory=lambda: client,
            deps_factory=lambda factory: (make_deps(FakeCatalog({})), lambda: None),
        )
    assert saved == []


def test_the_per_tool_limits_stop_a_runaway_search(saved) -> None:
    calls = [call("find_series", f"s{i}", request=f"seri {i}") for i in range(30)]
    client = ScriptedClient([Turn(tool_calls=calls), Turn(content="bitti")], [_final()])
    result, _closed = _run(client)
    assert result.status == "ok"
    assert len(saved[0][0].searches) == 24
