"""The first agent's final JSON and its strict parser (Task 3.3).

The agent registers candidates through tools. Its last answer is only the final
selection: which visual candidates and which relation ideas (after the graph
agent's answers) go to the visualization agent. Anything that does not match the
run's state is an :class:`~app.llm.errors.LLMOutputError`; the service records it
as an agent error and does not act on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.first_agent.state import RunState
from app.llm.errors import LLMOutputError

SYSTEM_PROMPT_KEY = "first_agent.system"

#: Graph answers whose relation can be visualized (a parked or untestable idea has
#: no result to draw).
VISUALIZABLE_GRAPH_STATUSES = ("existing", "tested", "queued")

_PICK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "integer", "description": "visual_id or idea_id from the tools."},
        "reason": {"type": "string", "description": "One short Turkish sentence."},
    },
    "required": ["id", "reason"],
}

FINAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "Two or three Turkish sentences: what the news gave, what was chosen.",
        },
        "selected_visuals": {"type": "array", "items": _PICK_SCHEMA},
        "selected_ideas": {"type": "array", "items": _PICK_SCHEMA},
    },
    "required": ["summary", "selected_visuals", "selected_ideas"],
}


@dataclass(frozen=True)
class Selection:
    """A validated final selection (ids are run-local)."""

    summary: str
    visuals: dict[int, str]
    ideas: dict[int, str]


def _picks(raw: Any, what: str) -> dict[int, str]:
    if not isinstance(raw, list):
        raise LLMOutputError(f"{what} must be a list")
    picks: dict[int, str] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise LLMOutputError(f"each entry of {what} must be an object")
        local_id = item.get("id")
        reason = item.get("reason")
        if isinstance(local_id, bool) or not isinstance(local_id, int):
            raise LLMOutputError(f"{what}.id must be an integer")
        if not isinstance(reason, str) or not reason.strip():
            raise LLMOutputError(f"{what}.reason must be a non-empty string")
        if local_id in picks:
            raise LLMOutputError(f"{what} lists id {local_id} twice")
        picks[local_id] = reason.strip()
    return picks


def parse_selection(output: Any, state: RunState) -> Selection:
    """Validate the final object against what the run registered."""
    if not isinstance(output, dict):
        raise LLMOutputError("final output is not a JSON object")
    summary = output.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise LLMOutputError("summary must be a non-empty string")
    visuals = _picks(output.get("selected_visuals"), "selected_visuals")
    ideas = _picks(output.get("selected_ideas"), "selected_ideas")
    for local_id in visuals:
        visual = state.visual(local_id)
        if visual is None:
            raise LLMOutputError(f"selected visual {local_id} was never registered")
        if visual.slot.is_gap:
            raise LLMOutputError(f"selected visual {local_id} has no series (catalog gap)")
    for local_id in ideas:
        idea = state.idea(local_id)
        if idea is None:
            raise LLMOutputError(f"selected idea {local_id} was never registered")
        if idea.status != "ready":
            raise LLMOutputError(f"selected idea {local_id} is {idea.status}, not ready")
        if idea.graph is None or idea.graph.get("status") not in VISUALIZABLE_GRAPH_STATUSES:
            raise LLMOutputError(
                f"selected idea {local_id} has no usable graph answer "
                f"(needs one of {list(VISUALIZABLE_GRAPH_STATUSES)})"
            )
    return Selection(summary=summary.strip(), visuals=visuals, ideas=ideas)


__all__ = [
    "FINAL_SCHEMA",
    "SYSTEM_PROMPT_KEY",
    "VISUALIZABLE_GRAPH_STATUSES",
    "Selection",
    "parse_selection",
]
