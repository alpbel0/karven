"""Concept tree loader (Task 2.1 / 2.2).

The approved concept tree lives next to this module as ``concept_tree.yaml`` and
is packaged into the image because the Dockerfile copies ``app/``. The loader is
cached: the file is static for a process.

It exposes the pieces the enrichment rules need:

- ``measure_types``: id -> :class:`MeasureType` (``ad``, ``toplama``, ``tanim``),
- ``data_natures``: id -> :class:`DataNature`,
- the assignable leaf ids and their owning branch ids (navigation groups),
- per-label names, definitions and ``degildir`` exclusion hints (Task 2.3 asks
  Jev whether a dataset belongs to a branch/leaf using those fields).

Nothing here touches the database or the network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

TREE_PATH = Path(__file__).parent / "concept_tree.yaml"


@dataclass(frozen=True)
class MeasureType:
    """One measurement type: id, Turkish name and its default aggregation rule."""

    id: str
    ad: str
    toplama: str
    tanim: str


@dataclass(frozen=True)
class DataNature:
    """One data nature: realised, expectation or forecast/projection."""

    id: str
    ad: str
    tanim: str


@dataclass(frozen=True)
class Concept:
    """One branch or leaf: id, Turkish name, definition and exclusion hints.

    ``degildir`` holds ``(target_id, reason)`` pairs from the tree; branches carry
    them, leaves usually do not.
    """

    id: str
    ad: str
    tanim: str
    degildir: tuple[tuple[str, str], ...] = ()

    def degildir_text(self) -> str:
        """The exclusion clause for a Jev question, empty when there is none."""
        if not self.degildir:
            return ""
        reasons = "; ".join(reason for _target, reason in self.degildir)
        return f" Şunlar bu etiket değildir: {reasons}"


def _degildir(raw: Any) -> tuple[tuple[str, str], ...]:
    entries: list[tuple[str, str]] = []
    for entry in raw or ():
        if not isinstance(entry, dict):
            continue
        target = entry.get("id")
        reason = entry.get("ne")
        if target and reason:
            entries.append((str(target), str(reason)))
    return tuple(entries)


@dataclass(frozen=True)
class ConceptTree:
    """The parsed concept tree, indexed for rule lookups."""

    version: str
    measure_types: dict[str, MeasureType]
    data_natures: dict[str, DataNature]
    branch_ids: frozenset[str]
    leaf_ids: frozenset[str]
    leaf_to_branch: dict[str, str]
    branches: dict[str, Concept] = field(default_factory=dict)
    leaves: dict[str, Concept] = field(default_factory=dict)
    branch_order: tuple[str, ...] = ()
    leaf_order: tuple[str, ...] = ()
    branch_leaves: dict[str, tuple[str, ...]] = field(default_factory=dict)
    branch_position: dict[str, int] = field(default_factory=dict)
    leaf_position: dict[str, int] = field(default_factory=dict)

    def aggregation_rule(self, measure_type_id: str | None) -> str | None:
        """The default aggregation for a measure type (``None`` when unknown)."""
        if measure_type_id is None:
            return None
        measure_type = self.measure_types.get(measure_type_id)
        return measure_type.toplama if measure_type is not None else None

    def branch_of(self, tag_id: str) -> str | None:
        """The owning branch of a tag id (the id itself when it is a branch)."""
        if tag_id in self.branch_ids:
            return tag_id
        return self.leaf_to_branch.get(tag_id)


def _parse(path: Path) -> ConceptTree:
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    measure_types = {
        item["id"]: MeasureType(
            id=item["id"], ad=item["ad"], toplama=item["toplama"], tanim=item["tanim"]
        )
        for item in raw["measure_types"]
    }
    data_natures = {
        item["id"]: DataNature(id=item["id"], ad=item["ad"], tanim=item["tanim"])
        for item in raw["data_nature"]
    }
    leaf_to_branch: dict[str, str] = {}
    branches: dict[str, Concept] = {}
    leaves: dict[str, Concept] = {}
    branch_order: list[str] = []
    leaf_order: list[str] = []
    branch_leaves: dict[str, tuple[str, ...]] = {}
    branch_position: dict[str, int] = {}
    leaf_position: dict[str, int] = {}
    for topic in raw["topics"]:
        branch_id = topic["id"]
        branches[branch_id] = Concept(
            id=branch_id,
            ad=topic["ad"],
            tanim=topic["tanim"],
            degildir=_degildir(topic.get("degildir")),
        )
        branch_position[branch_id] = len(branch_order)
        branch_order.append(branch_id)
        children: list[str] = []
        for child in topic["children"]:
            leaf_id = child["id"]
            leaves[leaf_id] = Concept(
                id=leaf_id,
                ad=child["ad"],
                tanim=child["tanim"],
                degildir=_degildir(child.get("degildir")),
            )
            leaf_position[leaf_id] = len(leaf_order)
            leaf_order.append(leaf_id)
            children.append(leaf_id)
            leaf_to_branch[leaf_id] = branch_id
        branch_leaves[branch_id] = tuple(children)
    return ConceptTree(
        version=str(raw.get("version", "")),
        measure_types=measure_types,
        data_natures=data_natures,
        branch_ids=frozenset(branch_order),
        leaf_ids=frozenset(leaf_order),
        leaf_to_branch=leaf_to_branch,
        branches=branches,
        leaves=leaves,
        branch_order=tuple(branch_order),
        leaf_order=tuple(leaf_order),
        branch_leaves=branch_leaves,
        branch_position=branch_position,
        leaf_position=leaf_position,
    )


@lru_cache(maxsize=1)
def load_tree() -> ConceptTree:
    """Load and cache the concept tree from the packaged YAML file."""
    return _parse(TREE_PATH)


__all__ = [
    "TREE_PATH",
    "Concept",
    "ConceptTree",
    "DataNature",
    "MeasureType",
    "load_tree",
]
