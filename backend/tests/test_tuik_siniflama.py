"""Unit tests for the TÜİK classification-server connector (no network; fixtures)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from app.connectors.base import EMPTY, FORMAT_CHANGED, ConnectorError, InMemoryObjectStore
from app.connectors.tuik import __main__ as tuik_cli
from app.connectors.tuik.__main__ import build_parser
from app.connectors.tuik.siniflama import (
    BI_DIMENSION_RULES,
    NORMALIZATION,
    ClassificationItemRow,
    SiniflamaClient,
    _plan_item_changes,
    bi_dimension_candidates,
    dimension_classification_matches,
    dimension_union_matches,
    discover_versions,
    is_aggregate_code,
    looks_like_classification,
    normalize_classification_code,
    normalize_label,
    parse_correspondence_detail,
    parse_correspondence_list,
    parse_tree,
    parse_version_list,
    stale_link_keys,
)

FIXTURES = Path(__file__).parent / "fixtures" / "tuik" / "siniflama"

VERSION_LIST = "version-list.tur1.raw.json"
TREE_62 = "tree-62.trimmed.raw.json"
TREE_49 = "tree-49.missing-parent.raw.json"
CORRESPONDENCE_LIST = "correspondence-list.raw.json"
CORRESPONDENCE_DETAIL = "correspondence-detail-271.trimmed.raw.json"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def fixture_json(name: str):
    return json.loads(fixture_bytes(name))


def json_response(name: str) -> httpx.Response:
    return httpx.Response(
        200, content=fixture_bytes(name), headers={"content-type": "application/json"}
    )


def build_client(handler, store: InMemoryObjectStore) -> SiniflamaClient:
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    return SiniflamaClient(http_client=http_client, store=store, sleeper=lambda _: None)


# --- version listing --------------------------------------------------------


def test_parse_version_list() -> None:
    versions = parse_version_list(fixture_json(VERSION_LIST), type_code="1")
    assert len(versions) == 14
    first = versions[0]
    assert first.external_id == "127"
    assert first.type_code == "1"
    assert first.short_name == "ISIC Rev.2"
    assert first.owner == "Birleşmiş Milletler"
    assert first.owner_en == "UNITED NATIONS"
    assert first.name.startswith("Tüm Ekonomik Faaliyetlerin")
    assert first.attributes["tur_sira_no"] == 1
    assert first.attributes["href"].startswith("https://")


def test_parse_version_list_accepts_empty_and_rejects_non_list() -> None:
    assert parse_version_list([], type_code="4") == []
    with pytest.raises(ConnectorError) as excinfo:
        parse_version_list({"Id": 1}, type_code="4")
    assert excinfo.value.kind == FORMAT_CHANGED


def test_discover_versions_scans_every_type() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tur = int(request.url.params.get("tur"))
        calls.append(tur)
        if tur == 1:
            return json_response(VERSION_LIST)
        return httpx.Response(200, content=b"[]", headers={"content-type": "application/json"})

    client = build_client(handler, InMemoryObjectStore())
    versions, failures = discover_versions(client)

    assert calls == list(range(0, 31))
    assert len(versions) == 14
    assert failures == []
    client.close()


# --- tree parsing -----------------------------------------------------------


def test_parse_tree_keeps_levels_and_metadata() -> None:
    items = parse_tree(fixture_json(TREE_62), raw_object_key="raw")
    by_code = {item.code: item for item in items}
    assert by_code["01"].parent_code is None
    assert by_code["01"].level == 1
    assert "parent_missing" not in by_code["01"].attributes
    leaf = by_code["01.1.1"]
    assert leaf.parent_code == "01.1"
    assert leaf.level == 3
    assert leaf.attributes["detay2"].startswith("-Yönetici")
    assert leaf.attributes["segment9"] == "4"


def test_parse_tree_keeps_source_row_id() -> None:
    items = parse_tree(fixture_json(TREE_62), raw_object_key="raw")
    by_code = {item.code: item for item in items}
    assert by_code["01"].attributes["source_row_id"] == "291766"


def test_parse_tree_flags_missing_parent_on_non_top_row() -> None:
    items = parse_tree(fixture_json(TREE_49))
    by_code = {item.code: item for item in items}
    orphan = by_code["15.11.15"]
    assert orphan.parent_code is None
    assert orphan.level == 6
    assert orphan.attributes["parent_missing"] is True
    # A top row without a parent is normal and must not be flagged.
    regular = by_code["01"]
    assert regular.parent_code == "A"
    assert "parent_missing" not in regular.attributes


def test_parse_tree_empty_is_empty_error() -> None:
    with pytest.raises(ConnectorError) as excinfo:
        parse_tree([])
    assert excinfo.value.kind == EMPTY


def test_parse_tree_rejects_non_list() -> None:
    with pytest.raises(ConnectorError) as excinfo:
        parse_tree({"kod": "1"})
    assert excinfo.value.kind == FORMAT_CHANGED


# --- correspondences --------------------------------------------------------


def test_parse_correspondence_list() -> None:
    summaries = parse_correspondence_list(fixture_json(CORRESPONDENCE_LIST))
    assert len(summaries) == 15
    first = summaries[0]
    assert first.external_id == "274"
    assert first.name == "CPA 2.1-CPC 2.0"
    assert first.from_external_id == "924"
    assert first.to_external_id == "231"
    assert first.from_label.startswith("Faaliyete")


def test_parse_correspondence_detail_keeps_dash_markers() -> None:
    items = parse_correspondence_detail(fixture_json(CORRESPONDENCE_DETAIL))
    dash_rows = [item for item in items if item.from_code == "-"]
    assert dash_rows, "the '-' no-source-code rows must be kept verbatim"
    assert dash_rows[0].to_code == "46.89.00"
    assert any(item.from_code == "01.11.07" for item in items)


# --- pure upsert planning ---------------------------------------------------


@dataclass
class _Existing:
    id: int
    code: str
    parent_code: str | None
    level: int
    label: str
    label_en: str | None
    attributes: dict


def _row(
    code: str,
    parent: str | None,
    level: int,
    label: str,
    attributes: dict | None = None,
) -> ClassificationItemRow:
    return ClassificationItemRow(
        code=code,
        parent_code=parent,
        level=level,
        label=label,
        label_en=None,
        attributes=attributes or {},
    )


def test_plan_item_changes_insert_update_unchanged_removed() -> None:
    incoming = [
        _row("A", None, 1, "Alpha"),
        _row("B", "A", 2, "Beta", {"detay1": "x"}),
        _row("C", "A", 2, "Gamma"),
    ]
    existing = [
        _Existing(2, "B", "A", 2, "Beta", None, {"detay1": "x"}),
        _Existing(4, "D", "A", 2, "Delta", None, {}),
    ]
    rows, inserted, updated, unchanged, removed = _plan_item_changes(5, incoming, existing)

    assert inserted == 2
    assert updated == 0
    assert unchanged == 1
    assert [row.code for row in removed] == ["D"]
    assert {row["code"] for row in rows} == {"A", "C"}


def test_plan_item_changes_clears_a_removed_marker() -> None:
    incoming = [_row("B", "A", 2, "Beta", {"detay1": "x"})]
    existing = [
        _Existing(
            2,
            "B",
            "A",
            2,
            "Beta",
            None,
            {"detay1": "x", "removed_at": "2026-01-01T00:00:00+00:00"},
        )
    ]
    rows, inserted, updated, unchanged, removed = _plan_item_changes(5, incoming, existing)

    assert (inserted, updated, unchanged, removed) == (0, 1, 0, [])
    assert rows[0]["attributes"] == {"detay1": "x"}


def test_plan_item_changes_keeps_a_code_under_two_parents() -> None:
    incoming = [
        _row("001", "1112", 2, "Fransa"),
        _row("001", "2019", 2, "Fransa"),
    ]
    rows, inserted, updated, unchanged, removed = _plan_item_changes(5, incoming, [])

    assert (inserted, updated, unchanged) == (2, 0, 0)
    assert removed == []
    assert {row["parent_code"] for row in rows} == {"1112", "2019"}


def test_plan_item_changes_keeps_two_labels_under_one_code_and_parent() -> None:
    incoming = [
        _row("01.01", "SINIF01", 2, "UNLU MAMULLER"),
        _row("01.01", "SINIF01", 2, "BİSKÜVİLER"),
    ]
    rows, inserted, updated, unchanged, removed = _plan_item_changes(5, incoming, [])

    assert (inserted, updated, unchanged) == (2, 0, 0)
    assert removed == []
    assert {row["label"] for row in rows} == {"UNLU MAMULLER", "BİSKÜVİLER"}


def test_plan_item_changes_collapses_an_identical_repeated_row() -> None:
    repeated = _row("A", None, 1, "Alpha")
    rows, inserted, updated, unchanged, removed = _plan_item_changes(5, [repeated, repeated], [])

    assert (inserted, updated, unchanged) == (1, 0, 0)
    assert len(rows) == 1


def test_plan_item_changes_marks_removal_by_triple_not_code() -> None:
    incoming = [_row("B", "A", 2, "Gamma")]
    existing = [_Existing(2, "B", "A", 2, "Beta", None, {})]
    rows, inserted, updated, unchanged, removed = _plan_item_changes(5, incoming, existing)

    assert (inserted, updated, unchanged) == (1, 0, 0)
    assert [row.label for row in removed] == ["Beta"]
    assert rows[0]["label"] == "Gamma"


# --- dimension linking rule -------------------------------------------------


def test_is_aggregate_code() -> None:
    assert is_aggregate_code("_T")
    assert is_aggregate_code("_Z")
    assert is_aggregate_code("TOTAL")
    assert is_aggregate_code("_X")
    assert is_aggregate_code("_ANYTHING")
    assert not is_aggregate_code("01")
    assert not is_aggregate_code("TR")


def _class_maps(
    versions: dict[int, list[tuple[str, str | None, str | None]]],
) -> tuple[dict[int, dict[str, set[str]]], dict[str, set[int]]]:
    class_labels: dict[int, dict[str, set[str]]] = {}
    inverted: dict[str, set[int]] = {}
    for classification_id, items in versions.items():
        labels = class_labels.setdefault(classification_id, {})
        for code, label, label_en in items:
            normalized = normalize_classification_code(code)
            bucket = labels.setdefault(normalized, set())
            for value in (label_en, label):
                normalized_label = normalize_label(value)
                if normalized_label:
                    bucket.add(normalized_label)
            inverted.setdefault(normalized, set()).add(classification_id)
    return class_labels, inverted


def _qualified(matches) -> list:
    return [match for match in matches if match.qualified]


def test_normalize_classification_code() -> None:
    assert normalize_classification_code("01") == "01"
    assert normalize_classification_code("01.1.1") == "0111"
    assert normalize_classification_code("0111121") == "0111121"
    assert normalize_classification_code("B") == "B"
    assert normalize_classification_code("B05") == "05"
    assert normalize_classification_code("B0510") == "0510"
    assert normalize_classification_code("B-C-D") == "B-C-D"
    assert normalize_classification_code(" 01.1 ") == "011"


def test_normalize_label_strips_accents_and_punctuation() -> None:
    assert normalize_label("Cereals") == "cereals"
    assert normalize_label("Süt & Süt Ürünleri") == "sut sut urunleri"
    assert normalize_label("Tahıllar") == "tahillar"
    assert normalize_label(None) == ""


def test_codes_with_dots_match() -> None:
    class_labels, inverted = _class_maps(
        {
            10: [
                ("01", None, "Food"),
                ("01.1", None, "Meat"),
                ("01.1.1", None, "Cereals"),
            ]
        }
    )
    matches = dimension_classification_matches(
        ["01", "011", "0111"],
        {"01": "Food", "011": "Meat", "0111": "Cereals"},
        class_labels=class_labels,
        inverted=inverted,
    )
    assert len(matches) == 1
    match = matches[0]
    assert match.classification_id == 10
    assert (match.total, match.found) == (3, 3)
    assert match.qualified
    assert match.coverage == 1.0
    assert match.label_agreement == 1.0


def test_section_prefixed_codes_match() -> None:
    class_labels, inverted = _class_maps(
        {20: [("B", "Madencilik", "Mining"), ("05", None, "Coal"), ("06", None, "Oil")]}
    )
    matches = dimension_classification_matches(
        ["B", "B05", "B06"],
        {"B": "Mining", "B05": "Coal", "B06": "Oil"},
        class_labels=class_labels,
        inverted=inverted,
    )
    assert len(matches) == 1
    assert matches[0].qualified
    assert matches[0].coverage == 1.0


def test_small_numeric_list_does_not_link() -> None:
    class_labels, inverted = _class_maps({30: [("1", None, "One"), ("2", None, "Two")]})
    matches = dimension_classification_matches(
        ["1", "2"],
        {"1": "One", "2": "Two"},
        class_labels=class_labels,
        inverted=inverted,
    )
    assert matches and not matches[0].qualified
    assert _qualified(matches) == []


def test_label_disagreement_blocks_a_code_only_match() -> None:
    class_labels, inverted = _class_maps(
        {40: [("A", None, "Alpha"), ("B", None, "Bravo"), ("C", None, "Charlie")]}
    )
    matches = dimension_classification_matches(
        ["A", "B", "C"],
        {"A": "Alpha", "B": "Wrong", "C": "Wrong"},
        class_labels=class_labels,
        inverted=inverted,
    )
    assert matches[0].coverage == 1.0
    assert matches[0].label_agreement == 1 / 3
    assert not matches[0].qualified


def test_multiple_qualifying_versions_are_kept_in_score_order() -> None:
    codes = ["A", "B", "C", "D", "E"]
    labels = {code: f"Label {code}" for code in codes}
    class_labels, inverted = _class_maps(
        {
            50: [(code, None, f"Label {code}") for code in codes],
            60: [
                *[(code, None, f"Label {code}") for code in codes[:-1]],
                ("E", None, "Wrong"),
            ],
        }
    )
    matches = dimension_classification_matches(
        codes, labels, class_labels=class_labels, inverted=inverted
    )
    assert [match.classification_id for match in matches] == [50, 60]
    assert all(match.qualified for match in matches)
    assert matches[0].label_agreement == 1.0
    assert matches[1].label_agreement == 0.8


def test_stale_link_keys_are_those_not_relinked() -> None:
    existing = {(1, 10): object(), (1, 20): object(), (2, 30): object()}
    linked = {(1, 10): object(), (2, 30): object()}
    assert stale_link_keys(existing, linked) == [(1, 20)]


def test_looks_like_classification() -> None:
    assert looks_like_classification("COICOP_2018")
    assert looks_like_classification("nace2")
    assert not looks_like_classification("REF_AREA")


# --- bi.tuik per-dimension rules -------------------------------------------


def test_union_rule_scores_against_the_group_union() -> None:
    class_labels, inverted = _class_maps(
        {
            1: [("01", None, "Live animals"), ("02", None, "Meat")],
            2: [("01", None, "Live animals"), ("03", None, "Fish")],
            3: [("01", None, "Wrong"), ("02", None, "Wrong"), ("03", None, "Wrong")],
        }
    )
    short_names = {1: "GTİP 2026", 2: "GTİP 2013", 3: "SITC Rev.4"}
    codes = {"01": "Live animals", "02": "Meat", "03": "Fish"}

    union = dimension_union_matches(
        codes, codes, group_ids={1, 2}, class_labels=class_labels, inverted=inverted
    )
    assert (union.total, union.found) == (3, 3)
    assert union.coverage == 1.0
    assert union.label_agreement == 1.0

    candidates = bi_dimension_candidates(
        "PRODUCT_HS",
        codes,
        class_labels=class_labels,
        inverted=inverted,
        short_names=short_names,
    )
    by_version = {candidate.classification_id: candidate for candidate in candidates}
    assert set(by_version) == {1, 2}
    assert by_version[1].attributes == {
        "rule": "union",
        "group": "GTİP",
        "union_coverage": 1.0,
        "version_coverage": 2 / 3,
    }
    assert by_version[1].matched_codes == 2 and by_version[1].total_codes == 3
    assert all(candidate.qualified for candidate in candidates)


def test_union_rule_does_not_qualify_below_the_coverage_threshold() -> None:
    class_labels, inverted = _class_maps({1: [("01", None, "Live animals")]})
    codes = {"01": "Live animals", **{f"{index:02d}": f"Label {index}" for index in range(2, 21)}}
    candidates = bi_dimension_candidates(
        "PRODUCT_HS",
        codes,
        class_labels=class_labels,
        inverted=inverted,
        short_names={1: "GTİP 2026"},
    )
    assert candidates == []


def test_code_only_rule_ignores_label_agreement() -> None:
    class_labels, inverted = _class_maps({4: [("01", None, "Wrong"), ("02", None, "Wrong")]})
    codes = {"01": "Live animals", "02": "Meat"}
    candidates = bi_dimension_candidates(
        "PRODUCT_SITC",
        codes,
        class_labels=class_labels,
        inverted=inverted,
        short_names={4: "SITC Rev.4"},
    )
    assert len(candidates) == 1
    assert candidates[0].coverage == 1.0
    assert candidates[0].label_agreement == 0.0
    assert candidates[0].attributes == {"rule": "code_only", "normalization": NORMALIZATION}
    assert candidates[0].qualified


def test_code_only_rule_keeps_only_the_best_version() -> None:
    class_labels, inverted = _class_maps(
        {
            4: [("01", None, "A"), ("02", None, "B")],
            5: [("01", None, "A")],
        }
    )
    codes = {"01": "A", "02": "B"}
    candidates = bi_dimension_candidates(
        "PRODUCT_SITC",
        codes,
        class_labels=class_labels,
        inverted=inverted,
        short_names={4: "SITC Rev.4", 5: "SITC Rev.3"},
    )
    assert [candidate.classification_id for candidate in candidates] == [4]


def test_normal_rule_candidates_use_the_normal_attributes() -> None:
    class_labels, inverted = _class_maps(
        {7: [("A", None, "Alpha"), ("B", None, "Bravo"), ("C", None, "Charlie")]}
    )
    codes = {"A": "Alpha", "B": "Bravo", "C": "Charlie"}
    candidates = bi_dimension_candidates(
        "PRODUCT_ISIC",
        codes,
        class_labels=class_labels,
        inverted=inverted,
        short_names={7: "ISIC Rev.4"},
    )
    assert [candidate.classification_id for candidate in candidates] == [7]
    assert candidates[0].qualified
    assert candidates[0].attributes == {"rule": "normal", "normalization": NORMALIZATION}


def test_bi_dimension_candidates_skip_unknown_dimensions() -> None:
    class_labels, inverted = _class_maps({1: [("01", None, "A")]})
    assert (
        bi_dimension_candidates(
            "PARTNER",
            {"01": "A"},
            class_labels=class_labels,
            inverted=inverted,
            short_names={1: "GTİP 2026"},
        )
        == []
    )
    assert set(BI_DIMENSION_RULES) == {
        "PRODUCT_HS",
        "PRODUCT_SITC",
        "PRODUCT_ISIC",
        "PRODUCT_BEC",
    }


# --- client -----------------------------------------------------------------


def test_version_list_sends_headers_and_stores_raw() -> None:
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["tur"] = request.url.params.get("tur")
        seen["searchname"] = request.url.params.get("searchname")
        seen["xjr"] = request.headers.get("X-Requested-With")
        seen["ua"] = request.headers.get("User-Agent")
        return json_response(VERSION_LIST)

    store = InMemoryObjectStore()
    client = build_client(handler, store)
    response = client.version_list(1)

    assert seen["tur"] == "1"
    assert seen["searchname"] == "null"
    assert seen["xjr"] == "XMLHttpRequest"
    assert seen["ua"] and "Mozilla" in seen["ua"]
    assert len(parse_version_list(response.json(), type_code="1")) == 14
    key = next(iter(store.objects))
    assert key.startswith("sources/tuik/")
    assert "/siniflama/siniflama-versions-1-" in key
    client.close()


def test_tree_request_shape_and_raw_key() -> None:
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["surumId"] = request.url.params.get("surumId")
        seen["seviye"] = request.url.params.get("seviye")
        seen["kod"] = request.url.params.get("kod")
        return json_response(TREE_62)

    store = InMemoryObjectStore()
    client = build_client(handler, store)
    response = client.tree("62")

    assert seen == {"surumId": "62", "seviye": "1", "kod": "0"}
    assert len(parse_tree(response.json())) == 10
    key = next(iter(store.objects))
    assert "/siniflama/62-" in key
    client.close()


def test_storeless_client_keeps_nothing() -> None:
    # A dry run builds the client with ``store=None``; nothing reaches MinIO.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = SiniflamaClient(http_client=http_client, store=None, sleeper=lambda _: None)
    versions, failures = discover_versions(client, turs=[0, 1])
    assert versions == []
    assert failures == []
    client.close()


# --- CLI parser -------------------------------------------------------------


def test_siniflama_parser_flags() -> None:
    args = build_parser().parse_args(
        ["siniflama", "--dry-run", "--only-id", "62", "--no-correspondences"]
    )
    assert args.command == "siniflama"
    assert args.dry_run is True
    assert args.only_id == "62"
    assert args.no_correspondences is True

    link = build_parser().parse_args(
        ["siniflama-link-dimensions", "--dry-run", "--report", "out.tsv"]
    )
    assert link.command == "siniflama-link-dimensions"
    assert link.dry_run is True
    assert link.report == "out.tsv"


def test_siniflama_routes_to_run_siniflama(monkeypatch) -> None:
    calls: list[str] = []

    def fake_run(args, dry_run):
        calls.append(args.command)
        return 0

    monkeypatch.setattr(tuik_cli, "_run_siniflama", fake_run)
    assert tuik_cli.main(["siniflama", "--dry-run", "--no-correspondences"]) == 0
    assert calls == ["siniflama"]
