"""The first agent's tools (Task 3.3): read-only helpers plus the registration tools.

Value-free by construction: every tool returns names, codes, frequencies, units,
coverage dates, statuses and verdicts, never an observation value.

Read-only helpers: ``browse_concepts``, ``find_series`` (Task 2.4), ``data_status``
(Task 2.5), ``read_graph``, ``ask_graph_agent`` (a port, stub until Task 3.4) and
``ask_fetch_agent`` (Task 1.6).

Registration tools: ``add_visual`` and ``add_idea``. They are where the code
validates what the agent proposes and answers with a rejection it can fix:

- every series is verified against the catalog (``data_status``),
- an idea's transform must fit every series (:func:`checks.check_transform`),
- a series the search could not find is kept as a ``catalog gap`` (and only after
  at least one search ran), the idea is not dropped,
- a tautological idea (series of one concept family) is stored with that status and
  never reaches the graph agent,
- the limits (8 ideas, 8 visuals) are enforced here.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.catalog.status import STATUS_TOOL_LIMIT
from app.catalog.tree import ConceptTree
from app.first_agent.checks import (
    SeriesMeta,
    check_transform,
    find_tautology,
    series_meta_from_status,
)
from app.first_agent.ports import GraphAgentPort, GraphIdeaRequest
from app.first_agent.state import (
    MAX_IDEAS,
    MAX_VISUALS,
    ROLE_DRIVER,
    ROLE_SERIES,
    ROLE_TARGET,
    Idea,
    RunState,
    Slot,
    Visual,
)
from app.graph.models import TRANSFORMS, SeriesRef

logger = logging.getLogger(__name__)

ToolSpec = tuple[dict[str, Any], Callable[..., Any]]

MAX_DRIVERS = 4
DIRECTION_HINTS = ("positive", "negative")

#: How often one tool may run in a single agent run (the global cap of 60 still binds).
TOOL_LIMITS = {
    "browse_concepts": 6,
    "find_series": 24,
    "data_status": 24,
    "read_graph": 16,
    "ask_graph_agent": MAX_IDEAS,
    "ask_fetch_agent": 6,
}


@dataclass
class ToolDeps:
    """Everything the tools need from the outside (injected, so tests need no network)."""

    status_fn: Callable[[list[dict[str, Any]]], dict[str, Any]]
    tags_fn: Callable[[str, str], Iterable[str]]
    graph_lookup: Callable[[SeriesRef, list[SeriesRef]], Any]
    graph_agent: GraphAgentPort
    fetch_fn: Callable[[str, dict[str, Any] | None], dict[str, Any]]
    tree: ConceptTree
    search_tools: Mapping[str, ToolSpec]


_RECIPE_PROPERTIES: dict[str, Any] = {
    "institution": {"type": "string"},
    "dataset": {"type": "string"},
    "codes": {"type": "object", "additionalProperties": {"type": "string"}},
}

_SLOT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "A series: either the recipe find_series gave you (institution, dataset, "
        "codes) or, only after searching, `missing` with a short Turkish description "
        "of a series the catalog does not have."
    ),
    "properties": {
        **_RECIPE_PROPERTIES,
        "missing": {"type": "string"},
    },
}


# --------------------------------------------------------------------------- #
# Slot verification
# --------------------------------------------------------------------------- #


def _recipe_of(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "institution": raw.get("institution"),
        "dataset": raw.get("dataset"),
        "codes": raw.get("codes"),
    }


def _info_of(result: Mapping[str, Any]) -> dict[str, Any]:
    measure = result.get("measure") or {}
    return {
        "series_external_code": result.get("series_external_code"),
        "series_name": result.get("series_name"),
        "frequency": result.get("frequency"),
        "unit": result.get("unit"),
        "measure_type": measure.get("measure_type"),
        "data_nature": measure.get("data_nature"),
        "transforms": result.get("transforms"),
        "loaded": result.get("loaded"),
        "loaded_range": result.get("loaded_range"),
        "available_range": result.get("available_range"),
    }


def _status_batches(deps: ToolDeps, recipes: Sequence[dict[str, Any]]) -> list[Mapping[str, Any]]:
    """``data_status`` results for ``recipes`` in order (the tool takes 3 at a time)."""
    results: list[Mapping[str, Any]] = []
    for start in range(0, len(recipes), STATUS_TOOL_LIMIT):
        chunk = list(recipes[start : start + STATUS_TOOL_LIMIT])
        output = deps.status_fn(chunk)
        results.extend(output.get("results") or [])
    return results


def _parse_slots(
    state: RunState, deps: ToolDeps, raws: Sequence[tuple[str, Any]]
) -> tuple[list[Slot], list[SeriesMeta | None], list[str]]:
    """Verified slots, their check metadata (``None`` for a gap) and error messages."""
    errors: list[str] = []
    slots: list[Slot] = []
    pending: list[tuple[int, dict[str, Any]]] = []
    for role, raw in raws:
        if not isinstance(raw, Mapping):
            errors.append(f"{role}: must be an object")
            slots.append(Slot(role=role))
            continue
        missing = raw.get("missing")
        if isinstance(missing, str) and missing.strip():
            if not state.searches:
                errors.append(
                    f"{role}: run find_series first; a missing series may only be declared "
                    "after at least one search"
                )
            slots.append(Slot(role=role, gap=missing.strip()))
            continue
        recipe = _recipe_of(raw)
        slots.append(Slot(role=role, recipe=recipe))
        pending.append((len(slots) - 1, recipe))

    metas: list[SeriesMeta | None] = [None] * len(slots)
    if pending:
        results = _status_batches(deps, [recipe for _index, recipe in pending])
        for (index, _recipe), result in zip(pending, results, strict=False):
            slot = slots[index]
            if result.get("status") != "ok":
                errors.append(
                    f"{slot.role}: series not usable ({result.get('status')}): "
                    f"{result.get('message', '')}"
                )
                continue
            slot.info = _info_of(result)
            tags = deps.tags_fn(str(result.get("institution")), str(result.get("dataset")))
            metas[index] = series_meta_from_status(
                str(slot.info.get("series_name") or ""), result, tags
            )
        if len(results) < len(pending):
            errors.append("the catalog check returned fewer results than series")
    return slots, metas, errors


# --------------------------------------------------------------------------- #
# Registration tools
# --------------------------------------------------------------------------- #


def _clean(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _news_value(raw: Any) -> tuple[dict[str, str | None] | None, str | None]:
    if raw is None:
        return None, None
    if not isinstance(raw, Mapping):
        return None, "news_value must be an object with text and period, or null"
    text = _clean(raw.get("text"))
    if text is None:
        return None, "news_value.text is required when news_value is given"
    return {"text": text, "period": _clean(raw.get("period"))}, None


def _add_visual(state: RunState, deps: ToolDeps, args: Mapping[str, Any]) -> dict[str, Any]:
    if len(state.visuals) >= MAX_VISUALS:
        return {"status": "rejected", "errors": [f"visual limit reached ({MAX_VISUALS})"]}
    title = _clean(args.get("title"))
    reason = _clean(args.get("reason"))
    errors: list[str] = []
    if title is None:
        errors.append("title is required")
    if reason is None:
        errors.append("reason is required")
    news_value, value_error = _news_value(args.get("news_value"))
    if value_error:
        errors.append(value_error)
    slots, _metas, slot_errors = _parse_slots(state, deps, [(ROLE_SERIES, args.get("series"))])
    errors.extend(slot_errors)
    if errors:
        return {"status": "rejected", "errors": errors}
    visual = Visual(
        local_id=len(state.visuals) + 1,
        title=title or "",
        reason=reason or "",
        slot=slots[0],
        news_value=news_value,
    )
    state.visuals.append(visual)
    return {
        "status": "stored",
        "visual_id": visual.local_id,
        "visual_status": visual.status,
        "visuals_left": MAX_VISUALS - len(state.visuals),
    }


def _slot_key(slot: Slot) -> str:
    info = slot.info or {}
    return f"{(slot.recipe or {}).get('institution')}|{info.get('series_external_code')}"


def _add_idea(state: RunState, deps: ToolDeps, args: Mapping[str, Any]) -> dict[str, Any]:
    if len(state.ideas) >= MAX_IDEAS:
        return {"status": "rejected", "errors": [f"idea limit reached ({MAX_IDEAS})"]}
    errors: list[str] = []
    title = _clean(args.get("title"))
    mechanism = _clean(args.get("mechanism"))
    transform = args.get("transform")
    hint = args.get("direction_hint")
    if title is None:
        errors.append("title is required")
    if mechanism is None:
        errors.append("mechanism is required")
    if transform not in TRANSFORMS:
        errors.append(f"transform must be one of {list(TRANSFORMS)}")
    if hint is not None and hint not in DIRECTION_HINTS:
        errors.append(f"direction_hint must be one of {list(DIRECTION_HINTS)} or null")
    drivers_raw = args.get("drivers")
    if not isinstance(drivers_raw, list) or not 1 <= len(drivers_raw) <= MAX_DRIVERS:
        errors.append(f"drivers must be a list of 1 to {MAX_DRIVERS} series")
        drivers_raw = []
    slots, metas, slot_errors = _parse_slots(
        state,
        deps,
        [(ROLE_TARGET, args.get("target")), *((ROLE_DRIVER, raw) for raw in drivers_raw)],
    )
    errors.extend(slot_errors)
    if errors:
        return {"status": "rejected", "errors": errors}

    target, drivers = slots[0], slots[1:]
    found = [(slot, meta) for slot, meta in zip(slots, metas, strict=True) if meta is not None]
    keys = [_slot_key(slot) for slot, _meta in found]
    if len(set(keys)) != len(keys):
        return {
            "status": "rejected",
            "errors": [
                "the same series appears twice in the idea (target and drivers must differ)"
            ],
        }
    transform_error, verified = check_transform(str(transform), (meta for _s, meta in found))
    if transform_error:
        return {"status": "rejected", "errors": [transform_error]}
    identity = frozenset(keys)
    if not any(slot.is_gap for slot in slots):
        for other in state.ideas:
            if (
                not any(s.is_gap for s in other.slots)
                and frozenset(_slot_key(s) for s in other.slots) == identity
                and _slot_key(other.target) == _slot_key(target)
            ):
                return {
                    "status": "rejected",
                    "errors": [f"this relation is already registered as idea {other.local_id}"],
                }

    detail: list[str] = []
    if any(slot.is_gap for slot in slots):
        status = "catalog_gap"
        detail.append("a series is missing from the catalog; the gap is recorded for the admin")
    else:
        tautology = find_tautology(metas[0], [meta for meta in metas[1:] if meta is not None])
        if tautology is not None:
            status = "tautological"
            detail.append(tautology)
        else:
            status = "ready"
    if not verified:
        detail.append("transform unverified: the measure of at least one series is unknown")

    idea = Idea(
        local_id=len(state.ideas) + 1,
        title=title or "",
        mechanism=mechanism or "",
        direction_hint=hint,
        transform=str(transform),
        target=target,
        drivers=drivers,
        status=status,
        status_detail="; ".join(detail) or None,
    )
    state.ideas.append(idea)
    return {
        "status": "stored",
        "idea_id": idea.local_id,
        "idea_status": idea.status,
        "note": idea.status_detail,
        "ideas_left": MAX_IDEAS - len(state.ideas),
    }


# --------------------------------------------------------------------------- #
# Read-only helpers
# --------------------------------------------------------------------------- #


def _browse_concepts(deps: ToolDeps, branch: str | None = None) -> dict[str, Any]:
    tree = deps.tree
    if branch is None:
        return {
            "branches": [
                {
                    "id": item,
                    "name": tree.branches[item].ad,
                    "definition": tree.branches[item].tanim,
                }
                for item in tree.branch_order
            ]
        }
    if branch not in tree.branches:
        return {"error": f"unknown branch {branch!r}", "branches": list(tree.branch_order)}
    return {
        "branch": branch,
        "leaves": [
            {"id": leaf, "name": tree.leaves[leaf].ad, "definition": tree.leaves[leaf].tanim}
            for leaf in tree.branch_leaves[branch]
        ],
    }


def _series_ref(deps: ToolDeps, raw: Any, role: str) -> tuple[SeriesRef | None, str | None]:
    if not isinstance(raw, Mapping):
        return None, f"{role}: must be a series recipe object"
    recipe = _recipe_of(raw)
    results = _status_batches(deps, [recipe])
    if not results or results[0].get("status") != "ok":
        message = results[0].get("message", "") if results else "no result"
        return None, f"{role}: series not usable: {message}"
    return SeriesRef(str(recipe["institution"]), str(results[0]["series_external_code"])), None


def _read_graph(deps: ToolDeps, args: Mapping[str, Any]) -> dict[str, Any]:
    target, error = _series_ref(deps, args.get("target"), ROLE_TARGET)
    if error:
        return {"status": "invalid_request", "message": error}
    drivers_raw = args.get("drivers")
    if not isinstance(drivers_raw, list) or not drivers_raw:
        return {"status": "invalid_request", "message": "drivers must be a non-empty list"}
    drivers: list[SeriesRef] = []
    for raw in drivers_raw:
        ref, error = _series_ref(deps, raw, ROLE_DRIVER)
        if error:
            return {"status": "invalid_request", "message": error}
        assert ref is not None
        drivers.append(ref)
    try:
        record = deps.graph_lookup(target, drivers)
    except Exception as exc:  # noqa: BLE001 - the graph may be off; tell the agent
        logger.warning("graph lookup failed: %s", exc)
        return {"status": "graph_unavailable", "message": str(exc)}
    if record is None:
        return {"status": "not_found", "message": "this relation is not in the graph yet"}
    latest = max(record.tests, key=lambda test: test.tested_at, default=None)
    return {
        "status": "found",
        "relation_key": record.key,
        "relation_status": record.status,
        "reliable_support": record.is_reliable_support,
        "mechanism": record.hypothesis.mechanism,
        "tests": len(record.tests),
        "latest_test": None
        if latest is None
        else {
            "tested_at": latest.tested_at.isoformat(),
            "periods": [
                {"period": item.period, "result": item.result, "reliable": item.reliable}
                for item in latest.period_results
            ],
        },
    }


def _ask_graph_agent(state: RunState, deps: ToolDeps, idea_id: int) -> dict[str, Any]:
    idea = state.idea(int(idea_id))
    if idea is None:
        return {"status": "rejected", "message": f"no idea {idea_id}"}
    if idea.status != "ready":
        return {
            "status": "rejected",
            "message": f"idea {idea.local_id} is {idea.status}; only ready ideas go to the graph "
            "agent",
        }
    if idea.graph is not None:
        return {"status": "rejected", "message": "this idea was already sent", "answer": idea.graph}
    request = GraphIdeaRequest(
        news_id=state.news_id,
        idea_id=idea.local_id,
        title=idea.title,
        mechanism=idea.mechanism,
        direction_hint=idea.direction_hint,
        transform=idea.transform,
        target=idea.target.to_json(),
        drivers=[slot.to_json() for slot in idea.drivers],
    )
    try:
        answer = deps.graph_agent.ask(request)
    except Exception as exc:  # noqa: BLE001 - reported to the agent, the idea stays askable
        logger.warning("graph agent failed for idea %s: %s", idea.local_id, exc)
        return {"status": "error", "message": f"{type(exc).__name__}: {exc}"}
    idea.graph = {
        "status": answer.status,
        "summary": answer.summary,
        "relation_key": answer.relation_key,
        "relation_status": answer.relation_status,
        "reliable_support": answer.reliable_support,
        "detail": answer.detail,
    }
    return {"idea_id": idea.local_id, **idea.graph}


def _ask_fetch_agent(
    deps: ToolDeps, question: str, context: dict[str, Any] | None = None
) -> dict[str, Any]:
    try:
        return deps.fetch_fn(question, context)
    except Exception as exc:  # noqa: BLE001 - reported to the agent
        logger.warning("fetch agent question failed: %s", exc)
        return {"status": "error", "message": f"{type(exc).__name__}: {exc}"}


# --------------------------------------------------------------------------- #
# Tool table
# --------------------------------------------------------------------------- #


def _schema(description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "description": description,
        "properties": properties,
        "required": required,
    }


def build_tools(state: RunState, deps: ToolDeps) -> dict[str, ToolSpec]:
    """The tool table for ``run_tool_loop`` (``{name: (schema, function)}``)."""
    tools: dict[str, ToolSpec] = {}

    tools["browse_concepts"] = (
        _schema(
            "Browse the concept tree. Without `branch` it lists the topic branches; "
            "with a branch id it lists that branch's leaf concepts. Use it to see what "
            "kinds of data the catalog holds before you search.",
            {"branch": {"type": ["string", "null"]}},
            [],
        ),
        lambda branch=None: _browse_concepts(deps, branch),
    )

    search_schema, search_function = deps.search_tools["find_series"]

    def find_series(request: str) -> Any:
        state.searches.append(str(request))
        return search_function(request=request)

    tools["find_series"] = (search_schema, find_series)
    status_schema, status_function = deps.search_tools["data_status"]
    tools["data_status"] = (status_schema, status_function)

    tools["read_graph"] = (
        _schema(
            "Look up one relation in the knowledge graph by its series: the target "
            "(affected series) and the drivers (affecting series), each a recipe from "
            "find_series. Returns whether the relation exists, its status, whether the "
            "support is reliable (`reliable_support`) and the latest test's per-period "
            "verdicts. Never returns values. Role matters: swapping target and drivers "
            "is another relation.",
            {
                "target": _SLOT_SCHEMA,
                "drivers": {"type": "array", "items": _SLOT_SCHEMA},
            },
            ["target", "drivers"],
        ),
        lambda target=None, drivers=None: _read_graph(deps, {"target": target, "drivers": drivers}),
    )

    tools["ask_graph_agent"] = (
        _schema(
            "Send one READY idea (by the idea_id add_idea gave you) to the knowledge "
            "graph agent, which finds an existing result or tests the relation. "
            "Answers: existing, tested, parked (data missing), not_testable or queued "
            "(the graph agent is not connected yet). One call per idea. A `supported` "
            "relation whose reliable_support is false is experimental statistical "
            "support, not established knowledge.",
            {"idea_id": {"type": "integer"}},
            ["idea_id"],
        ),
        lambda idea_id: _ask_graph_agent(state, deps, idea_id),
    )

    tools["ask_fetch_agent"] = (
        _schema(
            "Ask the data-fetch agent a question about data availability, for example "
            "when the news talks about a newer period than the series covers ('the news "
            "says September, we have August'). Pass a plain Turkish question and an "
            "optional context object (institution, dataset, codes). Returns an answer, "
            "a data_status (available, coverage, latest period) and a suggestion.",
            {
                "question": {"type": "string"},
                "context": {"type": ["object", "null"]},
            },
            ["question"],
        ),
        lambda question, context=None: _ask_fetch_agent(deps, question, context),
    )

    tools["add_visual"] = (
        _schema(
            "Register a news-visual candidate (list (a)): a series that the news itself "
            "describes (a petrol price rise news -> the petrol price series). The code "
            f"verifies the series. At most {MAX_VISUALS} per news. Give the figure and "
            "period the news states in news_value; it is shown as a note and never "
            "compared with the data.",
            {
                "title": {"type": "string", "description": "Short Turkish chart title."},
                "reason": {"type": "string", "description": "Why this chart helps the reader."},
                "series": _SLOT_SCHEMA,
                "news_value": {
                    "type": ["object", "null"],
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "The figure as the news states it.",
                        },
                        "period": {"type": ["string", "null"]},
                    },
                },
            },
            ["title", "reason", "series"],
        ),
        lambda **args: _add_visual(state, deps, args),
    )

    tools["add_idea"] = (
        _schema(
            "Register a relation idea (list (b)): drivers (affecting series) -> target "
            "(affected series). The code verifies every series, checks that the "
            "transform fits each series and that target and drivers are not one concept "
            "family (a tautology is stored but never sent on). If a series is not in "
            "the catalog after searching, give `missing` for it: the idea is kept as a "
            f"catalog gap. At most {MAX_IDEAS} per news. Returns the idea_id and status "
            "(ready, catalog_gap, tautological) or the errors to fix.",
            {
                "title": {"type": "string", "description": "Short Turkish relation title."},
                "target": _SLOT_SCHEMA,
                "drivers": {"type": "array", "items": _SLOT_SCHEMA},
                "transform": {"type": "string", "enum": list(TRANSFORMS)},
                "mechanism": {
                    "type": "string",
                    "description": "One or two Turkish sentences: why the drivers move the target.",
                },
                "direction_hint": {"type": ["string", "null"], "enum": [*DIRECTION_HINTS, None]},
            },
            ["title", "target", "drivers", "transform", "mechanism"],
        ),
        lambda **args: _add_idea(state, deps, args),
    )
    return tools


__all__ = [
    "DIRECTION_HINTS",
    "MAX_DRIVERS",
    "TOOL_LIMITS",
    "ToolDeps",
    "ToolSpec",
    "build_tools",
]
