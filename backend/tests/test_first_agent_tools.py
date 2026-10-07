"""Unit tests for the first agent's tools (Task 3.3): registration checks and helpers."""

from __future__ import annotations

from app.first_agent.ports import GraphAgentAnswer
from app.first_agent.state import MAX_IDEAS, MAX_VISUALS
from tests.first_agent_fakes import (
    FakeCatalog,
    RecordingGraphAgent,
    recipe,
    relation_record,
    status_result,
    tools_for,
)


def _catalog() -> FakeCatalog:
    return FakeCatalog(
        {
            "FUEL": status_result("tuik", "FUEL", "Benzin fiyatı", measure_type="fiyat_kur"),
            "USD": status_result("tcmb", "USD", "USD/TRY", measure_type="fiyat_kur", unit="TL"),
            "CPI": status_result("tuik", "CPI", "TÜFE endeksi"),
            "CPIPCT": status_result(
                "tuik",
                "CPIPCT",
                "TÜFE yıllık % değişim",
                measure_type="yillik_yuzde_degisim",
                transforms=["level"],
            ),
            "RENT": status_result("tuik", "RENT", "TÜFE kira"),
            "EXPECT": status_result("tcmb", "EXPECT", "Enflasyon beklentisi", nature="beklenti"),
        },
        tags={"CPI": {"tuketici_fiyatlari"}, "RENT": {"tuketici_fiyatlari"}, "USD": {"kur"}},
    )


def _idea_args(target: str = "CPI", drivers: tuple[str, ...] = ("USD",), **extra):
    args = {
        "title": "Kur -> fiyatlar",
        "target": recipe(target),
        "drivers": [recipe(item) for item in drivers],
        "transform": "annual_pct_change",
        "mechanism": "Kur artışı maliyetleri yükseltir.",
        "direction_hint": "positive",
    }
    args.update(extra)
    return args


# --- add_visual -------------------------------------------------------------


def test_add_visual_stores_a_verified_series_with_the_news_figure() -> None:
    state, _deps, tools = tools_for(_catalog())
    result = tools["add_visual"][1](
        title="Benzin fiyatı",
        reason="Haber benzin zammını anlatıyor.",
        series=recipe("FUEL"),
        news_value={"text": "yüzde 4,2 zam", "period": "Eylül 2026"},
    )
    assert result["status"] == "stored"
    assert result["visual_id"] == 1
    assert result["visual_status"] == "candidate"
    visual = state.visuals[0]
    assert visual.slot.info["series_name"] == "Benzin fiyatı"
    assert visual.news_value == {"text": "yüzde 4,2 zam", "period": "Eylül 2026"}


def test_add_visual_rejects_a_series_that_is_not_in_the_catalog() -> None:
    state, _deps, tools = tools_for(_catalog())
    result = tools["add_visual"][1](title="x", reason="y", series=recipe("NOPE"))
    assert result["status"] == "rejected"
    assert "unknown_dataset" in result["errors"][0]
    assert state.visuals == []


def test_add_visual_gap_needs_a_search_first_and_is_kept_afterwards() -> None:
    state, _deps, tools = tools_for(_catalog())
    early = tools["add_visual"][1](title="x", reason="y", series={"missing": "Brent petrol"})
    assert early["status"] == "rejected"
    assert "find_series" in early["errors"][0]

    tools["find_series"][1](request="Brent petrol fiyatı")
    kept = tools["add_visual"][1](title="x", reason="y", series={"missing": "Brent petrol"})
    assert kept["status"] == "stored"
    assert kept["visual_status"] == "catalog_gap"
    assert state.searches == ["Brent petrol fiyatı"]
    assert state.visuals[0].slot.gap == "Brent petrol"


def test_add_visual_limit() -> None:
    state, _deps, tools = tools_for(_catalog())
    for _ in range(MAX_VISUALS):
        assert (
            tools["add_visual"][1](title="x", reason="y", series=recipe("FUEL"))["status"]
            == "stored"
        )
    over = tools["add_visual"][1](title="x", reason="y", series=recipe("FUEL"))
    assert over["status"] == "rejected"
    assert len(state.visuals) == MAX_VISUALS


def test_add_visual_requires_title_and_reason_and_a_valid_news_value() -> None:
    _state, _deps, tools = tools_for(_catalog())
    result = tools["add_visual"][1](
        title=" ", reason="", series=recipe("FUEL"), news_value={"period": "Eylül"}
    )
    assert result["status"] == "rejected"
    assert len(result["errors"]) == 3


# --- add_idea ---------------------------------------------------------------


def test_add_idea_ready() -> None:
    state, _deps, tools = tools_for(_catalog())
    result = tools["add_idea"][1](**_idea_args())
    assert result["status"] == "stored"
    assert result["idea_status"] == "ready"
    assert result["idea_id"] == 1
    assert state.ideas[0].drivers[0].info["series_name"] == "USD/TRY"


def test_add_idea_wrong_series_type_goes_back_to_the_agent_and_is_not_stored() -> None:
    state, _deps, tools = tools_for(_catalog())
    result = tools["add_idea"][1](**_idea_args(target="CPIPCT"))
    assert result["status"] == "rejected"
    assert "TÜFE yıllık % değişim" in result["errors"][0]
    assert state.ideas == []

    fixed = tools["add_idea"][1](**_idea_args(target="CPI"))
    assert fixed["status"] == "stored"


def test_add_idea_tautology_is_stored_but_marked_and_not_ready() -> None:
    state, _deps, tools = tools_for(_catalog())
    result = tools["add_idea"][1](**_idea_args(target="RENT", drivers=("CPI",)))
    assert result["status"] == "stored"
    assert result["idea_status"] == "tautological"
    assert "tuketici_fiyatlari" in state.ideas[0].status_detail


def test_add_idea_with_a_missing_series_is_kept_as_a_catalog_gap() -> None:
    state, _deps, tools = tools_for(_catalog())
    tools["find_series"][1](request="Brent petrol")
    args = _idea_args()
    args["drivers"] = [{"missing": "Brent petrol fiyatı (aylık)"}]
    result = tools["add_idea"][1](**args)
    assert result["status"] == "stored"
    assert result["idea_status"] == "catalog_gap"
    assert state.ideas[0].drivers[0].gap == "Brent petrol fiyatı (aylık)"


def test_add_idea_gap_still_checks_the_transform_of_the_found_series() -> None:
    _state, _deps, tools = tools_for(_catalog())
    tools["find_series"][1](request="x")
    args = _idea_args(target="CPIPCT")
    args["drivers"] = [{"missing": "Brent"}]
    assert tools["add_idea"][1](**args)["status"] == "rejected"


def test_add_idea_rejects_the_same_series_as_target_and_driver() -> None:
    _state, _deps, tools = tools_for(_catalog())
    result = tools["add_idea"][1](**_idea_args(target="CPI", drivers=("CPI",)))
    assert result["status"] == "rejected"
    assert "twice" in result["errors"][0]


def test_add_idea_rejects_a_duplicate_relation() -> None:
    _state, _deps, tools = tools_for(_catalog())
    assert tools["add_idea"][1](**_idea_args())["status"] == "stored"
    again = tools["add_idea"][1](**_idea_args())
    assert again["status"] == "rejected"
    assert "already registered" in again["errors"][0]


def test_add_idea_validates_the_plain_fields() -> None:
    _state, _deps, tools = tools_for(_catalog())
    result = tools["add_idea"][1](
        **_idea_args(transform="log", mechanism=" ", direction_hint="up", drivers=[])
    )
    assert result["status"] == "rejected"
    joined = " ".join(result["errors"])
    assert "transform" in joined
    assert "mechanism" in joined
    assert "direction_hint" in joined
    assert "drivers" in joined


def test_add_idea_limit() -> None:
    catalog = FakeCatalog(
        {f"D{i}": status_result("tcmb", f"D{i}", f"Seri {i}") for i in range(12)},
    )
    state, _deps, tools = tools_for(catalog)
    for i in range(MAX_IDEAS):
        result = tools["add_idea"][1](**_idea_args(target=f"D{i}", drivers=(f"D{i + 1}",)))
        assert result["status"] == "stored"
    over = tools["add_idea"][1](**_idea_args(target="D9", drivers=("D10",)))
    assert over["status"] == "rejected"
    assert len(state.ideas) == MAX_IDEAS


def test_more_than_three_series_are_verified_in_batches() -> None:
    catalog = FakeCatalog({f"D{i}": status_result("tcmb", f"D{i}", f"Seri {i}") for i in range(6)})
    _state, _deps, tools = tools_for(catalog)
    result = tools["add_idea"][1](**_idea_args(target="D0", drivers=("D1", "D2", "D3", "D4")))
    assert result["status"] == "stored"
    assert [len(chunk) for chunk in catalog.status_calls] == [3, 2]


# --- ask_graph_agent --------------------------------------------------------


def test_only_ready_ideas_go_to_the_graph_agent_once() -> None:
    graph = RecordingGraphAgent(GraphAgentAnswer(status="existing", summary="var"))
    state, _deps, tools = tools_for(_catalog(), graph_agent=graph)
    tools["add_idea"][1](**_idea_args())
    tools["add_idea"][1](**_idea_args(target="RENT", drivers=("CPI",)))

    answered = tools["ask_graph_agent"][1](idea_id=1)
    assert answered["status"] == "existing"
    assert answered["idea_id"] == 1
    assert state.ideas[0].graph["status"] == "existing"
    assert graph.requests[0].idea_id == 1
    assert graph.requests[0].transform == "annual_pct_change"

    again = tools["ask_graph_agent"][1](idea_id=1)
    assert again["status"] == "rejected"
    taut = tools["ask_graph_agent"][1](idea_id=2)
    assert taut["status"] == "rejected"
    assert "tautological" in taut["message"]
    assert tools["ask_graph_agent"][1](idea_id=9)["status"] == "rejected"
    assert len(graph.requests) == 1


def test_graph_agent_failure_is_reported_and_the_idea_stays_askable() -> None:
    graph = RecordingGraphAgent(raise_exc=RuntimeError("boom"))
    state, _deps, tools = tools_for(_catalog(), graph_agent=graph)
    tools["add_idea"][1](**_idea_args())
    result = tools["ask_graph_agent"][1](idea_id=1)
    assert result["status"] == "error"
    assert "boom" in result["message"]
    assert state.ideas[0].graph is None


# --- read_graph / ask_fetch_agent / browse_concepts -------------------------


def test_read_graph_found_not_found_and_unavailable() -> None:
    record = relation_record(status="supported", reliable=False)
    state, _deps, tools = tools_for(_catalog(), graph_lookup=lambda target, drivers: record)
    found = tools["read_graph"][1](target=recipe("CPI"), drivers=[recipe("USD")])
    assert found["status"] == "found"
    assert found["relation_status"] == "supported"
    assert found["reliable_support"] is False
    assert found["latest_test"]["periods"][0]["reliable"] is False
    assert "p_value" not in str(found)

    _state, _deps, tools = tools_for(_catalog(), graph_lookup=lambda target, drivers: None)
    assert (
        tools["read_graph"][1](target=recipe("CPI"), drivers=[recipe("USD")])["status"]
        == "not_found"
    )

    def broken(target, drivers):
        raise RuntimeError("neo4j down")

    _state, _deps, tools = tools_for(_catalog(), graph_lookup=broken)
    result = tools["read_graph"][1](target=recipe("CPI"), drivers=[recipe("USD")])
    assert result["status"] == "graph_unavailable"


def test_read_graph_rejects_a_series_that_is_not_catalogued() -> None:
    _state, _deps, tools = tools_for(_catalog())
    result = tools["read_graph"][1](target=recipe("NOPE"), drivers=[recipe("USD")])
    assert result["status"] == "invalid_request"


def test_ask_fetch_agent_passes_question_and_reports_errors() -> None:
    seen: list[tuple] = []

    def fetch(question, context):
        seen.append((question, context))
        return {"answer": "Eylül yok", "data_status": {"available": False}}

    _state, _deps, tools = tools_for(_catalog(), fetch_fn=fetch)
    answer = tools["ask_fetch_agent"][1](question="Eylül var mı?", context={"dataset": "CPI"})
    assert answer["answer"] == "Eylül yok"
    assert seen == [("Eylül var mı?", {"dataset": "CPI"})]

    def broken(question, context):
        raise RuntimeError("down")

    _state, _deps, tools = tools_for(_catalog(), fetch_fn=broken)
    assert tools["ask_fetch_agent"][1](question="?")["status"] == "error"


def test_browse_concepts_lists_branches_then_leaves() -> None:
    _state, _deps, tools = tools_for(_catalog())
    branches = tools["browse_concepts"][1]()
    assert branches["branches"]
    first = branches["branches"][0]["id"]
    leaves = tools["browse_concepts"][1](branch=first)
    assert leaves["leaves"]
    assert "error" in tools["browse_concepts"][1](branch="yok-boyle-dal")


def test_no_tool_output_carries_an_observation_value_field() -> None:
    state, _deps, tools = tools_for(_catalog())
    tools["add_visual"][1](title="x", reason="y", series=recipe("FUEL"))
    tools["add_idea"][1](**_idea_args())
    dumped = str([visual.slot.to_json() for visual in state.visuals]) + str(
        [idea.target.to_json() for idea in state.ideas]
    )
    assert "'value'" not in dumped
    assert "observations" not in dumped
