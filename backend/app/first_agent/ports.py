"""The graph agent port (Task 3.3).

The graph agent is written in Task 3.4. The first agent already calls it as a tool,
so the interface and the answer shape are fixed here and the default
implementation is a stub that answers ``queued``. Task 3.4 replaces the stub with
the real agent without touching the first agent.

The request carries series identities and the idea's text, never values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

#: What the graph agent can answer. ``queued`` is the stub's answer: the idea was
#: accepted but nobody has tested it yet.
GRAPH_ANSWER_STATUSES = (
    "existing",  # the relation was already in the graph; its stored result is returned
    "tested",  # a new hypothesis was tested and written to the graph
    "parked",  # data is missing; the idea is parked until the fetch agent delivers it
    "not_testable",  # the graph agent could not form a testable hypothesis
    "queued",  # stub: not wired yet
)


@dataclass(frozen=True)
class GraphIdeaRequest:
    """One relation idea sent to the graph agent."""

    news_id: int
    idea_id: int
    title: str
    mechanism: str
    direction_hint: str | None
    transform: str
    target: dict[str, Any]
    drivers: list[dict[str, Any]]


@dataclass(frozen=True)
class GraphAgentAnswer:
    """The graph agent's answer to one idea.

    ``reliable_support`` is ``RelationRecord.is_reliable_support``: a ``supported``
    status with an unreliable test is experimental statistical support, never
    established knowledge, and the first agent must word it that way.
    """

    status: str
    summary: str
    relation_key: str | None = None
    relation_status: str | None = None
    reliable_support: bool | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in GRAPH_ANSWER_STATUSES:
            raise ValueError(f"unknown graph answer status {self.status!r}")


class GraphAgentPort(Protocol):
    """What the first agent needs from the graph agent."""

    def ask(self, request: GraphIdeaRequest) -> GraphAgentAnswer:
        """Answer one idea."""


class StubGraphAgent:
    """Placeholder until Task 3.4: accepts every idea and tests nothing."""

    def ask(self, request: GraphIdeaRequest) -> GraphAgentAnswer:
        return GraphAgentAnswer(
            status="queued",
            summary=(
                "Graph ajanı henüz bağlı değil (Task 3.4); fikir kabul edildi ama test edilmedi."
            ),
        )


__all__ = [
    "GRAPH_ANSWER_STATUSES",
    "GraphAgentAnswer",
    "GraphAgentPort",
    "GraphIdeaRequest",
    "StubGraphAgent",
]
