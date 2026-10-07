"""In-memory doubles for the Task 3.3 first-agent unit tests (no database, no network)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.catalog.tree import load_tree
from app.first_agent.ports import GraphAgentAnswer, GraphIdeaRequest
from app.first_agent.state import RunState
from app.first_agent.tools import ToolDeps, build_tools
from app.graph.models import Hypothesis, PeriodResult, RelationRecord, SeriesRef, TestRecord

ALL = ["level", "percent_change_period", "percent_change_annual"]


def status_result(
    institution: str,
    dataset: str,
    name: str,
    *,
    measure_type: str | None = "endeks",
    nature: str | None = "gerceklesen",
    transforms: list[str] | None = None,
    frequency: str = "monthly",
    unit: str = "endeks",
) -> dict[str, Any]:
    """One ``data_status`` item with status ``ok``."""
    return {
        "institution": institution,
        "dataset": dataset,
        "codes": {},
        "status": "ok",
        "series_external_code": f"{dataset}:{name}",
        "series_name": name,
        "series_exists": True,
        "series_id": 1,
        "loaded": True,
        "frequency": frequency,
        "unit": unit,
        "available_range": {"start": "2000-01-01", "end": "2026-08-01"},
        "loaded_range": {"start": "2005-01-01", "end": "2026-08-01", "periods": 260},
        "measure": {"measure_type": measure_type, "data_nature": nature, "kumulatif": False}
        if measure_type is not None
        else None,
        "transforms": (transforms if transforms is not None else ALL)
        if measure_type is not None
        else None,
        "arsiv": False,
    }


class FakeCatalog:
    """Answers ``data_status`` from a table keyed by dataset code."""

    def __init__(self, results: dict[str, dict[str, Any]], tags: dict[str, set[str]] | None = None):
        self.results = results
        self.tags = tags or {}
        self.status_calls: list[list[dict[str, Any]]] = []

    def status_fn(self, recipes: list[dict[str, Any]]) -> dict[str, Any]:
        self.status_calls.append(recipes)
        out = []
        for recipe in recipes:
            result = self.results.get(str(recipe.get("dataset")))
            if result is None:
                out.append(
                    {
                        "institution": recipe.get("institution"),
                        "dataset": recipe.get("dataset"),
                        "status": "unknown_dataset",
                        "message": "no such dataset in the catalog",
                    }
                )
            else:
                out.append(result)
        return {"results": out}

    def tags_fn(self, institution: str, dataset: str) -> set[str]:
        return set(self.tags.get(dataset, set()))


class RecordingGraphAgent:
    """A graph agent double that records requests and answers with a fixed answer."""

    def __init__(self, answer: GraphAgentAnswer | None = None, raise_exc: Exception | None = None):
        self.answer = answer or GraphAgentAnswer(status="tested", summary="test done")
        self.raise_exc = raise_exc
        self.requests: list[GraphIdeaRequest] = []

    def ask(self, request: GraphIdeaRequest) -> GraphAgentAnswer:
        self.requests.append(request)
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.answer


def make_deps(
    catalog: FakeCatalog,
    *,
    graph_agent: RecordingGraphAgent | None = None,
    graph_lookup: Any = None,
    fetch_fn: Any = None,
    find_series: Any = None,
) -> ToolDeps:
    find_schema = {"type": "object", "description": "find", "properties": {}}
    return ToolDeps(
        status_fn=catalog.status_fn,
        tags_fn=catalog.tags_fn,
        graph_lookup=graph_lookup or (lambda target, drivers: None),
        graph_agent=graph_agent or RecordingGraphAgent(),
        fetch_fn=fetch_fn or (lambda question, context: {"answer": "ok", "data_status": {}}),
        tree=load_tree(),
        search_tools={
            "find_series": (find_schema, find_series or (lambda request: {"status": "ok"})),
            "data_status": (find_schema, lambda recipes: catalog.status_fn(recipes)),
        },
    )


def tools_for(catalog: FakeCatalog, **kwargs: Any):
    state = RunState(news_id=1)
    deps = make_deps(catalog, **kwargs)
    return state, deps, build_tools(state, deps)


def recipe(dataset: str, institution: str = "tuik") -> dict[str, Any]:
    return {"institution": institution, "dataset": dataset, "codes": {"X": "1"}}


def relation_record(*, status: str = "supported", reliable: bool | None = False) -> RelationRecord:
    target = SeriesRef("tuik", "T")
    driver = SeriesRef("tcmb", "D")
    hypothesis = Hypothesis(
        mechanism="m",
        driver_directions={driver.key: "positive"},
        lag_min=0,
        lag_max=3,
        transform="annual_pct_change",
    )
    tested_at = datetime(2026, 10, 1, tzinfo=UTC)
    test = TestRecord(
        id="t1",
        relation_key="k",
        tested_at=tested_at,
        period_results=(
            PeriodResult(
                period="all_years",
                result="supported",
                reliable=bool(reliable),
                reliability_reason=None if reliable else "experimental",
            ),
        ),
    )
    return RelationRecord(
        key="k",
        target=target,
        drivers=(driver,),
        hypothesis=hypothesis,
        status=status,
        reliable=reliable,
        tests=(test,),
    )


# --- chat client ------------------------------------------------------------


@dataclass
class Turn:
    """One scripted model answer."""

    content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


def call(name: str, call_id: str = "c", **arguments: Any) -> dict[str, Any]:
    """A tool call as the chat client returns it."""
    return {
        "id": f"{call_id}-{name}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


@dataclass
class _Result:
    content: str | None
    tool_calls: list[dict[str, Any]] | None
    usage: dict[str, Any]
    provider: str = "fake"
    model: str = "fake-model"


class ScriptedClient:
    """Pops scripted turns for non-final requests and finals for ``response_format``."""

    def __init__(self, turns: list[Turn], finals: list[Any]) -> None:
        self.turns = list(turns)
        self.finals = list(finals)
        self.requests: list[dict[str, Any]] = []
        self.closed = False

    def complete(self, messages, *, tools=None, response_format=None, prompt_ref=None, **kwargs):
        self.requests.append(
            {"messages": list(messages), "has_format": response_format is not None}
        )
        usage = {"prompt_tokens": 10, "completion_tokens": 5}
        if response_format is not None:
            final = self.finals.pop(0)
            content = final if isinstance(final, str) or final is None else json.dumps(final)
            return _Result(content, None, usage)
        turn = self.turns.pop(0) if self.turns else Turn(content="done")
        return _Result(turn.content, turn.tool_calls, usage)

    def close(self) -> None:
        self.closed = True
