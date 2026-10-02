"""Parent/child resolution for dataset dimension codes.

Preferred source is the databrowser2 ``PartialCodelists`` ``parentId`` field
(seen live for IBBS/NUTS region code lists). When the source exposes no
hierarchy, parents are derived only for code systems where the relationship is
unambiguous: NACE Rev.2 (``C13`` -> ``C``, ``C1310`` -> ``C13``) and
IBBS/NUTS regions (``TR100`` -> ``TR10`` -> ``TR1`` -> ``TR``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable

SOURCE = "source"
DERIVED_NACE = "derived_nace"
DERIVED_NUTS = "derived_nuts"

# NACE: a section letter, then the division (2 digits) and optionally the class
# group (2 more digits). ``C13`` -> ``C``; ``C1310`` -> ``C13``.
_NACE_GROUP_RE = re.compile(r"^([A-Z])(\d{2})(\d{2})$")
_NACE_DIVISION_RE = re.compile(r"^([A-Z])(\d{2})$")
# IBBS/NUTS: two-letter country prefix then one to three hierarchy digits.
_NUTS_RE = re.compile(r"^([A-Z]{2})(\d{1,3})$")


def is_nace_dimension(dimension_code: str, label: str, dsd_ref: str | None) -> bool:
    haystack = f"{dimension_code} {label} {dsd_ref or ''}".upper()
    return "NACE" in haystack


def is_nuts_dimension(dimension_code: str, label: str, dsd_ref: str | None) -> bool:
    haystack = f"{dimension_code} {label} {dsd_ref or ''}".upper()
    return "IBBS" in haystack or "NUTS" in haystack


def derive_nace_parent(code: str) -> str | None:
    """Parent of a NACE Rev.2 code, or ``None`` for a top-level section."""
    value = code.strip().upper()
    match = _NACE_GROUP_RE.fullmatch(value)
    if match:
        return value[:3]
    match = _NACE_DIVISION_RE.fullmatch(value)
    if match:
        return match.group(1)
    return None


def derive_nuts_parent(code: str) -> str | None:
    """Parent of an IBBS/NUTS region code, or ``None`` for the country root."""
    value = code.strip().upper()
    match = _NUTS_RE.fullmatch(value)
    if not match:
        return None
    prefix, digits = match.group(1), match.group(2)
    if len(digits) == 1:
        return prefix
    return prefix + digits[:-1]


def resolve_parents(
    dimension_code: str,
    label: str,
    dsd_ref: str | None,
    codes: Iterable[tuple[str, str | None]],
) -> tuple[dict[str, str | None], str | None]:
    """Return ``(parent_by_code, hierarchy_source)`` for one dimension's codes.

    ``codes`` are ``(code, source_parent)`` pairs. A source parent anywhere makes
    the whole dimension source-derived; otherwise parents are derived for a
    recognised code system, and ``None`` is reported for an unrecognised one.
    """
    pairs = list(codes)
    if any(parent for _, parent in pairs):
        return {code: parent for code, parent in pairs}, SOURCE
    if is_nace_dimension(dimension_code, label, dsd_ref):
        return {code: derive_nace_parent(code) for code, _ in pairs}, DERIVED_NACE
    if is_nuts_dimension(dimension_code, label, dsd_ref):
        return {code: derive_nuts_parent(code) for code, _ in pairs}, DERIVED_NUTS
    return {code: None for code, _ in pairs}, None


__all__ = [
    "DERIVED_NACE",
    "DERIVED_NUTS",
    "SOURCE",
    "derive_nace_parent",
    "derive_nuts_parent",
    "is_nace_dimension",
    "is_nuts_dimension",
    "resolve_parents",
]
