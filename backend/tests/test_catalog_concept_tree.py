"""Integrity checks for docs/catalog/concept-tree.yaml (no database, no network)."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

TREE_PATH = Path(__file__).resolve().parents[2] / "docs" / "catalog" / "concept-tree.yaml"

pytestmark = pytest.mark.skipif(not TREE_PATH.exists(), reason="docs/ is not mounted")

AGGREGATIONS = {"toplam", "ortalama", "donem_sonu", "yeniden_hesapla", "test_disi"}


def _load() -> dict:
    return yaml.safe_load(TREE_PATH.read_text(encoding="utf-8"))


def _all_ids(tree: dict) -> list[str]:
    ids = [t["id"] for t in tree["topics"]]
    ids += [c["id"] for t in tree["topics"] for c in t["children"]]
    return ids


def test_ids_are_unique_ascii_snake_case() -> None:
    ids = _all_ids(_load())
    assert [i for i, n in Counter(ids).items() if n > 1] == []
    assert [i for i in ids if not (i.isascii() and i == i.lower() and " " not in i)] == []


def test_every_label_has_a_definition() -> None:
    tree = _load()
    for topic in tree["topics"]:
        assert topic.get("tanim"), topic["id"]
        assert topic.get("degildir"), f"{topic['id']} has no degildir"
        assert topic["children"], f"{topic['id']} has no leaves"
        for child in topic["children"]:
            assert child.get("tanim"), child["id"]
    for key in ("measure_types", "data_nature"):
        for item in tree[key]:
            assert item.get("tanim"), f"{key}.{item['id']}"


def test_degildir_targets_exist() -> None:
    tree = _load()
    ids = set(_all_ids(tree))
    for topic in tree["topics"]:
        for entry in topic["degildir"]:
            assert entry["id"] in ids, f"{topic['id']} -> {entry['id']}"
            assert entry.get("ne"), f"{topic['id']} -> {entry['id']} has no reason"
            assert entry["id"] != topic["id"]


def test_measure_type_and_data_nature_ids_are_unique() -> None:
    tree = _load()
    for key in ("measure_types", "data_nature"):
        ids = [item["id"] for item in tree[key]]
        assert [i for i, n in Counter(ids).items() if n > 1] == [], key


def test_measure_types_carry_an_aggregation_rule() -> None:
    for item in _load()["measure_types"]:
        assert item["toplama"] in AGGREGATIONS, item["id"]
