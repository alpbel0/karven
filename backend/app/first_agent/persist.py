"""Writing one first-agent run to PostgreSQL (Task 3.3).

One transaction per run: the run row, its visual and idea candidates and one
``catalog_gaps`` row per missing series slot. The run is written after the agent
finished (or failed), so a crash mid-run leaves nothing half-written; a failed run
keeps the candidates registered so far for diagnosis.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.data.models import CatalogGap, FirstAgentIdea, FirstAgentRun, FirstAgentVisual
from app.first_agent.state import RunState, Slot


@dataclass(frozen=True)
class RunMeta:
    """Run-level facts written on ``first_agent_runs``."""

    status: str
    error: str | None = None
    summary: str | None = None
    selection: dict[str, Any] = field(default_factory=dict)
    prompt_key: str | None = None
    prompt_version: int | None = None
    prompt_checksum: str | None = None
    provider: str | None = None
    model: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    llm_attempts: int = 0
    tool_calls: int = 0


def _gap_rows(
    run_id: int, news_id: int, kind: str, local_id: int, slots: list[Slot], searches: list[str]
) -> list[CatalogGap]:
    return [
        CatalogGap(
            run_id=run_id,
            news_id=news_id,
            item_kind=kind,
            item_local_id=local_id,
            role=slot.role,
            description=slot.gap or "",
            searches=list(searches),
        )
        for slot in slots
        if slot.is_gap
    ]


def save_run(session: Session, state: RunState, meta: RunMeta) -> int:
    """Write the run and its candidates; the caller commits. Returns the run id."""
    run = FirstAgentRun(
        news_id=state.news_id,
        status=meta.status,
        error=meta.error,
        summary=meta.summary,
        selection=meta.selection,
        prompt_key=meta.prompt_key,
        prompt_version=meta.prompt_version,
        prompt_checksum=meta.prompt_checksum,
        llm_provider=meta.provider,
        llm_model=meta.model,
        llm_usage=meta.usage,
        llm_attempts=meta.llm_attempts,
        tool_calls=meta.tool_calls,
    )
    session.add(run)
    session.flush()
    for visual in state.visuals:
        session.add(
            FirstAgentVisual(
                run_id=run.id,
                local_id=visual.local_id,
                title=visual.title,
                reason=visual.reason,
                status=visual.status,
                series=None if visual.slot.is_gap else visual.slot.to_json(),
                news_value=visual.news_value,
                selected=visual.selected,
                selection_reason=visual.selection_reason,
            )
        )
        session.add_all(
            _gap_rows(
                run.id, state.news_id, "visual", visual.local_id, [visual.slot], state.searches
            )
        )
    for idea in state.ideas:
        session.add(
            FirstAgentIdea(
                run_id=run.id,
                local_id=idea.local_id,
                title=idea.title,
                mechanism=idea.mechanism,
                direction_hint=idea.direction_hint,
                transform=idea.transform,
                target=idea.target.to_json(),
                drivers=[slot.to_json() for slot in idea.drivers],
                status=idea.status,
                status_detail=idea.status_detail,
                graph_status=(idea.graph or {}).get("status"),
                graph_answer=idea.graph,
                selected=idea.selected,
                selection_reason=idea.selection_reason,
            )
        )
        session.add_all(
            _gap_rows(run.id, state.news_id, "idea", idea.local_id, idea.slots, state.searches)
        )
    session.flush()
    return int(run.id)


__all__ = ["RunMeta", "save_run"]
