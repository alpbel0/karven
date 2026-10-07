"""Code checks on a relation idea (Task 3.3): transform fit and tautology.

Both checks work on the metadata ``data_status`` returns (never values).

- **Transform fit.** The idea names one transform for the whole relation. Every
  series must allow it according to ``data_status`` (a percent change needs a
  quantity-type series; a series that already is a % change, a rate or a share
  allows only ``level``). When the measure is unknown the transform cannot be
  judged: it passes and the idea is marked ``transform unverified``.
- **Tautology.** Two series of the same concept family (they share an accepted
  leaf tag of the concept tree and have the same measure type and data nature)
  are the parts of one thing, for example the general consumer price index and one
  of its sub-items. A relation between them is near-tautological and is not sent
  to the graph agent. Known coarseness: two series of one family that are not
  hierarchical (exports and imports both tagged with one leaf) are flagged too; the
  flagged idea is stored with its reason, so the admin can see it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

#: Idea transform (graph vocabulary) -> data_status vocabulary.
_TRANSFORM_TO_STATUS = {
    "annual_pct_change": "percent_change_annual",
    "period_pct_change": "percent_change_period",
    "difference": "level",
}


@dataclass(frozen=True)
class SeriesMeta:
    """The metadata of one series the checks need."""

    label: str
    institution: str
    dataset: str
    measure_type: str | None
    data_nature: str | None
    transforms: tuple[str, ...] | None
    leaf_tags: frozenset[str]


def check_transform(transform: str, series: Iterable[SeriesMeta]) -> tuple[str | None, bool]:
    """``(error, verified)`` for ``transform`` over every series.

    ``error`` is a message for the agent when a series does not allow the transform;
    ``verified`` is false when at least one series' allowed transforms are unknown.
    """
    wanted = _TRANSFORM_TO_STATUS.get(transform)
    if wanted is None:
        return f"unknown transform {transform!r}", False
    verified = True
    for item in series:
        if item.transforms is None:
            verified = False
            continue
        if wanted not in item.transforms:
            allowed = ", ".join(item.transforms)
            return (
                f"series {item.label!r} does not allow transform {transform!r} "
                f"(allowed for this series: {allowed}); pick another series "
                "(for example the index level instead of a % change series) or another transform",
                verified,
            )
    return None, verified


def same_family(left: SeriesMeta, right: SeriesMeta) -> str | None:
    """The reason two series are one concept family, or ``None``."""
    shared = left.leaf_tags & right.leaf_tags
    if not shared:
        return None
    if not left.measure_type or left.measure_type != right.measure_type:
        return None
    if not left.data_nature or left.data_nature != right.data_nature:
        return None
    return (
        f"{left.label!r} and {right.label!r} share the concept {sorted(shared)[0]!r} "
        f"with the same measure type ({left.measure_type}) and data nature "
        f"({left.data_nature}): they are parts of one family"
    )


def find_tautology(target: SeriesMeta, drivers: Sequence[SeriesMeta]) -> str | None:
    """The first tautology reason between the target and any driver."""
    for driver in drivers:
        reason = same_family(target, driver)
        if reason is not None:
            return reason
    return None


def series_meta_from_status(
    label: str, status: Mapping[str, Any], leaf_tags: Iterable[str]
) -> SeriesMeta:
    """Build a :class:`SeriesMeta` from one ``data_status`` result."""
    measure = status.get("measure") or {}
    transforms = status.get("transforms")
    return SeriesMeta(
        label=label,
        institution=str(status.get("institution")),
        dataset=str(status.get("dataset")),
        measure_type=measure.get("measure_type"),
        data_nature=measure.get("data_nature"),
        transforms=tuple(transforms) if transforms is not None else None,
        leaf_tags=frozenset(leaf_tags),
    )


__all__ = [
    "SeriesMeta",
    "check_transform",
    "find_tautology",
    "same_family",
    "series_meta_from_status",
]
