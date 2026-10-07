"""In-memory state of one first-agent run (Task 3.3).

The agent registers candidates through tools; the code validates each one on the
spot and keeps it here. The run is written to the database in one step at the end
(:mod:`app.first_agent.persist`), so ``local_id`` values (1, 2, ...) are run-local
and become ``first_agent_*.local_id`` columns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MAX_IDEAS = 8
MAX_VISUALS = 8
MAX_TOOL_CALLS = 60

ROLE_TARGET = "target"
ROLE_DRIVER = "driver"
ROLE_SERIES = "series"


@dataclass
class Slot:
    """One series place in a candidate: a verified series or a catalog gap.

    ``recipe`` is ``{institution, dataset, codes}``; ``info`` is the metadata the
    code verified with ``data_status`` (series name, frequency, unit, measure type,
    data nature, allowed transforms, loaded range). ``gap`` is the description of a
    series the search could not find; then ``recipe`` and ``info`` are ``None``.
    """

    role: str
    recipe: dict[str, Any] | None = None
    info: dict[str, Any] | None = None
    gap: str | None = None

    @property
    def is_gap(self) -> bool:
        return self.gap is not None

    def to_json(self) -> dict[str, Any]:
        if self.gap is not None:
            return {"role": self.role, "missing": self.gap}
        return {"role": self.role, "recipe": self.recipe, "info": self.info}


@dataclass
class Visual:
    """A news-visual candidate (list (a))."""

    local_id: int
    title: str
    reason: str
    slot: Slot
    news_value: dict[str, str | None] | None
    selected: bool = False
    selection_reason: str | None = None

    @property
    def status(self) -> str:
        return "catalog_gap" if self.slot.is_gap else "candidate"


@dataclass
class Idea:
    """A relation idea (list (b))."""

    local_id: int
    title: str
    mechanism: str
    direction_hint: str | None
    transform: str
    target: Slot
    drivers: list[Slot]
    status: str  # ready | catalog_gap | tautological
    status_detail: str | None = None
    graph: dict[str, Any] | None = None
    selected: bool = False
    selection_reason: str | None = None

    @property
    def slots(self) -> list[Slot]:
        return [self.target, *self.drivers]


@dataclass
class RunState:
    """Everything one run registered."""

    news_id: int
    visuals: list[Visual] = field(default_factory=list)
    ideas: list[Idea] = field(default_factory=list)
    searches: list[str] = field(default_factory=list)

    def visual(self, local_id: int) -> Visual | None:
        return next((item for item in self.visuals if item.local_id == local_id), None)

    def idea(self, local_id: int) -> Idea | None:
        return next((item for item in self.ideas if item.local_id == local_id), None)


__all__ = [
    "MAX_IDEAS",
    "MAX_TOOL_CALLS",
    "MAX_VISUALS",
    "ROLE_DRIVER",
    "ROLE_SERIES",
    "ROLE_TARGET",
    "Idea",
    "RunState",
    "Slot",
    "Visual",
]
