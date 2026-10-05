"""Concept tree loader (Task 2.1 / 2.2).

The approved concept tree lives next to this module as ``concept_tree.yaml`` and
is packaged into the image because the Dockerfile copies ``app/``. The loader is
cached: the file is static for a process.

It exposes the pieces the enrichment rules need:

- ``measure_types``: id -> :class:`MeasureType` (``ad``, ``toplama``, ``tanim``),
- ``data_natures``: id -> :class:`DataNature`,
- the assignable leaf ids and their owning branch ids (navigation groups).

Nothing here touches the database or the network.
"""

from __future__ import annotations

from dataclasses import dataclass
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
class ConceptTree:
    """The parsed concept tree, indexed for rule lookups."""

    version: str
    measure_types: dict[str, MeasureType]
    data_natures: dict[str, DataNature]
    branch_ids: frozenset[str]
    leaf_ids: frozenset[str]
    leaf_to_branch: dict[str, str]

    def aggregation_rule(self, measure_type_id: str | None) -> str | None:
        """The default aggregation for a measure type (``None`` when unknown)."""
        if measure_type_id is None:
            return None
        measure_type = self.measure_types.get(measure_type_id)
        return measure_type.toplama if measure_type is not None else None


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
    for topic in raw["topics"]:
        for child in topic["children"]:
            leaf_to_branch[child["id"]] = topic["id"]
    return ConceptTree(
        version=str(raw.get("version", "")),
        measure_types=measure_types,
        data_natures=data_natures,
        branch_ids=frozenset({topic["id"] for topic in raw["topics"]}),
        leaf_ids=frozenset(leaf_to_branch),
        leaf_to_branch=leaf_to_branch,
    )


@lru_cache(maxsize=1)
def load_tree() -> ConceptTree:
    """Load and cache the concept tree from the packaged YAML file."""
    return _parse(TREE_PATH)


__all__ = ["TREE_PATH", "ConceptTree", "DataNature", "MeasureType", "load_tree"]
