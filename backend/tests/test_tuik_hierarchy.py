"""Unit tests for TÜİK dimension parent/child resolution (no network)."""

from __future__ import annotations

from app.connectors.tuik.hierarchy import (
    DERIVED_NACE,
    DERIVED_NUTS,
    SOURCE,
    derive_nace_parent,
    derive_nuts_parent,
    resolve_parents,
)


def test_nace_parent_derivation() -> None:
    assert derive_nace_parent("A") is None
    assert derive_nace_parent("C13") == "C"
    assert derive_nace_parent("C1310") == "C13"
    assert derive_nace_parent("A01") == "A"
    assert derive_nace_parent("13") is None


def test_nuts_parent_derivation() -> None:
    assert derive_nuts_parent("TR") is None
    assert derive_nuts_parent("TR1") == "TR"
    assert derive_nuts_parent("TR10") == "TR1"
    assert derive_nuts_parent("TR100") == "TR10"
    assert derive_nuts_parent("TR212") == "TR21"


def test_resolve_prefers_source_hierarchy() -> None:
    parents, source = resolve_parents(
        "REF_AREA",
        "Reference area",
        "TR+CL_IBBS+1.2",
        [("TR", None), ("TR1", "TR"), ("TR100", "TR1")],
    )
    assert source == SOURCE
    assert parents == {"TR": None, "TR1": "TR", "TR100": "TR1"}


def test_resolve_derives_nace_when_source_has_no_parent() -> None:
    parents, source = resolve_parents(
        "FAALIYET_NACE_REV2",
        "NACE Rev.2",
        "TR+CL_NACE+1.0",
        [("C", None), ("C13", None), ("C1310", None)],
    )
    assert source == DERIVED_NACE
    assert parents == {"C": None, "C13": "C", "C1310": "C13"}


def test_resolve_derives_nuts_when_source_has_no_parent() -> None:
    parents, source = resolve_parents(
        "REF_AREA",
        "Reference area",
        "TR+CL_IBBS+1.2",
        [("TR", None), ("TR100", None)],
    )
    assert source == DERIVED_NUTS
    assert parents == {"TR": None, "TR100": "TR10"}


def test_resolve_unknown_system_has_no_hierarchy() -> None:
    parents, source = resolve_parents("SEX", "Sex", None, [("1", None), ("2", None)])
    assert source is None
    assert parents == {"1": None, "2": None}
