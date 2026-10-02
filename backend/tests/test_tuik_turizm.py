"""Unit tests for the turizmapp tourism connector (no network; fixtures)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.connectors.base import ROLE_GEO, ROLE_OTHER, ROLE_TIME, ConnectorError
from app.connectors.tuik import __main__ as tuik_cli
from app.connectors.tuik.__main__ import build_parser
from app.connectors.tuik.turizm import (
    _INTERPRETED_CODES,
    CATALOG_FAMILY_BY_CODE,
    FORM_PATH_BY_CODE,
    FORM_PATHS,
    HEADLINES,
    CatalogFamily,
    FormAxis,
    TurizmConnector,
    WalkResult,
    _newest_years,
    _sorted_points,
    _years_from,
    distinct_series_count,
    find_period_header,
    find_variable_header,
    interpret_monthly,
    interpret_variable_report,
    normalize_code_value,
    parse_unit,
    split_gate_label,
    walk_family,
)
from app.connectors.tuik.zk import ListItem, parse_report_grid

FIXTURES = Path(__file__).parent / "fixtures" / "tuik" / "turizm"
REPORT = (FIXTURES / "sinir_nationality.report.html").read_bytes()
INCOME_REPORT = (FIXTURES / "cikis_income.report.html").read_bytes()

MILLIYET = FORM_PATH_BY_CODE["TUIK_TURIZM_SINIR_MILLIYET"]
VATANDAS = FORM_PATH_BY_CODE["TUIK_TURIZM_SINIR_VATANDAS"]


def _valid() -> dict[str, dict[str, str]]:
    return {
        "MILLIYET": {"A.B.D": "A.B.D", "_T": "_T"},
        "IL": {"Ağrı": "Ağrı", "Ankara": "Ankara", "_T": "_T"},
        "KAPI": {"Gürbulak": "Gürbulak", "Esenboğa": "Esenboğa", "_T": "_T"},
        "YOL": {"Karayolu": "Karayolu", "Havayolu": "Havayolu", "_T": "_T"},
    }


def test_split_gate_label_reads_province_gate_and_road() -> None:
    assert split_gate_label("Adana - Botaş (Denizyolu)") == ("Adana", "Botaş", "Denizyolu")
    assert split_gate_label("Ankara  - Esenboğa (Havayolu)") == ("Ankara", "Esenboğa", "Havayolu")
    assert split_gate_label("Standalone") == (None, "Standalone", None)
    assert split_gate_label("<< Tüm kapılar >>") == (None, "<< Tüm kapılar >>", None)


def test_normalize_code_value_strips_a_province_prefix() -> None:
    assert normalize_code_value("Adana,Şakirpaşa") == "Şakirpaşa"
    assert normalize_code_value("Ankara ,Esenboğa") == "Esenboğa"
    assert normalize_code_value("Esenboğa") == "Esenboğa"


def test_find_period_header_reads_months_and_year() -> None:
    header = find_period_header(parse_report_grid(REPORT))
    assert header is not None
    index, periods, year = header
    assert year == 2025
    assert len(periods) == 12
    assert sorted(periods.values()) == list(range(1, 13))


def test_interpret_monthly_grand_total_and_month_points() -> None:
    series, uninterpreted = interpret_monthly(
        parse_report_grid(REPORT), MILLIYET.measures, _valid()
    )
    grand = next(entry for entry in series if entry.codes == {d: "_T" for d in _valid()})
    assert len(grand.points) == 12
    assert grand.points[0] == (date(2025, 1, 1), Decimal("2171118"))
    assert sum((value or Decimal(0)) for _, value in grand.points) == Decimal("52775055")
    assert uninterpreted == []


def test_interpret_monthly_blank_carry_and_total_labels() -> None:
    html = (
        "<html><table>"
        "<tr><td>x</td><td>2025</td></tr>"
        "<tr><td>M</td><td>K</td><td>Toplam</td><td>Ocak</td><td>Şubat</td>"
        "<td>Mart</td><td>Nisan</td></tr>"
        "<tr><td>A</td><td>Genel Toplam</td><td>10</td><td>1</td><td>2</td><td>3</td>"
        "<td>4</td></tr>"
        "<tr><td></td><td>g1</td><td>6</td><td>1</td><td>2</td><td>3</td><td></td></tr>"
        "</table></html>"
    )
    grid = parse_report_grid(html.encode("utf-8"), encoding="utf-8")
    measures = (
        MILLIYET.measures[0],
        MILLIYET.measures[2],
    )
    valid = {"MILLIYET": {"A": "A", "_T": "_T"}, "KAPI": {"g1": "g1", "_T": "_T"}}
    series, _ = interpret_monthly(grid, measures, valid)
    code_sets = {frozenset(entry.codes.items()) for entry in series}
    assert frozenset({("MILLIYET", "A"), ("KAPI", "_T")}) in code_sets
    assert frozenset({("MILLIYET", "A"), ("KAPI", "g1")}) in code_sets


def test_interpret_monthly_rejects_a_report_without_months() -> None:
    grid = parse_report_grid(b"<html><table><tr><td>only</td></tr></table></html>")
    with pytest.raises(ConnectorError):
        interpret_monthly(grid, MILLIYET.measures, _valid())


def test_dataset_meta_for_citizens_adds_direction() -> None:
    result = WalkResult(
        codes={
            "IL": {"Adana": "Adana", "_T": "_T"},
            "KAPI": {"Şakirpaşa": "Şakirpaşa", "_T": "_T"},
            "YOL": {"Havayolu": "Havayolu", "_T": "_T"},
        },
        labels={
            "IL": {"Adana": "Adana"},
            "KAPI": {"Şakirpaşa": "Şakirpaşa"},
            "YOL": {"Havayolu": "Havayolu"},
        },
        years=["2025", "2024"],
    )
    meta = TurizmConnector(store=None)._dataset_meta(VATANDAS, result)
    assert meta.external_code == "TUIK_TURIZM_SINIR_VATANDAS"
    assert meta.attributes["channel"] == "turizmapp"
    assert meta.attributes["default_frequency"] == "monthly"
    dimensions = {dimension.code: dimension for dimension in meta.dimensions}
    assert set(dimensions) == {"DIRECTION", "IL", "KAPI", "YOL", "TIME_PERIOD"}
    assert dimensions["DIRECTION"].codes[0].code == "GIRIS"
    assert dimensions["DIRECTION"].codes[1].code == "CIKIS"
    assert dimensions["DIRECTION"].role == ROLE_OTHER
    assert dimensions["IL"].role == ROLE_GEO
    assert dimensions["TIME_PERIOD"].role == ROLE_TIME
    assert dimensions["KAPI"].codes[0].code == "_T"
    assert (meta.coverage_start, meta.coverage_end) == (date(2024, 1, 1), date(2025, 12, 1))


def test_every_report_path_is_interpreted() -> None:
    assert set(FORM_PATH_BY_CODE) <= _INTERPRETED_CODES
    assert all(path.code in FORM_PATH_BY_CODE for path in FORM_PATHS)
    assert set(CATALOG_FAMILY_BY_CODE).isdisjoint(FORM_PATH_BY_CODE)


def test_parse_unit_reads_the_scale() -> None:
    assert parse_unit("(Bin $)") == ("Bin $", 1_000)
    assert parse_unit(" (Milyon $) ") == ("Milyon $", 1_000_000)
    assert parse_unit("(Bin ₺)") == ("Bin ₺", 1_000)
    assert parse_unit("2012") is None
    assert parse_unit("(Yıl)") is None


def test_find_variable_header_reads_quarterly_layout() -> None:
    report = find_variable_header(parse_report_grid(INCOME_REPORT), "quarterly")
    assert report is not None
    assert report.year == 2012
    assert report.periods == {1: 1, 2: 2, 3: 3, 4: 4}
    assert report.unit == "Bin $"
    assert report.scale == 1_000


def test_interpret_variable_report_quarterly_income() -> None:
    series, uninterpreted = interpret_variable_report(
        parse_report_grid(INCOME_REPORT), variable="Milliyet", frequency="quarterly"
    )
    assert uninterpreted == []
    almanya = next(entry for entry in series if entry.codes["KATEGORI"] == "Almanya")
    assert almanya.codes == {"VARIABLE": "Milliyet", "KATEGORI": "Almanya"}
    assert [period for period, _ in almanya.points] == [
        date(2012, 1, 1),
        date(2012, 4, 1),
        date(2012, 7, 1),
        date(2012, 10, 1),
    ]
    assert sum(value or Decimal(0) for _, value in almanya.points) == Decimal("2810344")
    assert (almanya.unit, almanya.scale) == ("Bin $", 1_000)


def test_interpret_variable_report_annual_uses_the_total_column() -> None:
    html = (
        "<html><table>"
        "<tr><td>2012</td><td>(Bin $)</td></tr>"
        "<tr><td>&nbsp;</td><td>Toplam ($)</td></tr>"
        "<tr><td>Almanya</td><td>2.810.345</td></tr>"
        "</table></html>"
    )
    report = find_variable_header(parse_report_grid(html.encode("utf-8")), "annual")
    assert report is not None
    assert report.periods == {1: None}
    series, _ = interpret_variable_report(
        parse_report_grid(html.encode("utf-8")), variable="Milliyet", frequency="annual"
    )
    almanya = next(entry for entry in series if entry.codes["KATEGORI"] == "Almanya")
    assert almanya.points == [(date(2012, 1, 1), Decimal("2810345"))]


def test_interpret_variable_report_carries_a_split_category_label() -> None:
    # Legacy reports split a category label and its numbers across two physical
    # rows: the label row has no values, the next row has values but an empty
    # label. The previous category must be carried, not skipped as "".
    html = (
        "<html><table>"
        "<tr><td>2012</td><td>(Bin $)</td></tr>"
        "<tr><td></td><td>1.Dönem ($)</td><td>2.Dönem ($)</td><td>3.Dönem ($)</td>"
        "<td>4.Dönem ($)</td></tr>"
        "<tr><td>Almanya</td><td></td><td></td><td></td><td></td></tr>"
        "<tr><td></td><td>1.000</td><td>2.000</td><td>3.000</td><td>4.000</td></tr>"
        "</table></html>"
    )
    grid = parse_report_grid(html.encode("utf-8"), encoding="utf-8")
    series, uninterpreted = interpret_variable_report(
        grid, variable="Milliyet", frequency="quarterly"
    )
    assert uninterpreted == []
    almanya = next(entry for entry in series if entry.codes["KATEGORI"] == "Almanya")
    assert [value for _, value in almanya.points] == [
        Decimal("1000"),
        Decimal("2000"),
        Decimal("3000"),
        Decimal("4000"),
    ]


def test_interpret_variable_report_drops_a_leading_blank_row() -> None:
    # A values row with no preceding label is a spacer/separator, not data.
    html = (
        "<html><table>"
        "<tr><td>2012</td><td>(Bin $)</td></tr>"
        "<tr><td></td><td>1.Dönem ($)</td><td>2.Dönem ($)</td><td>3.Dönem ($)</td>"
        "<td>4.Dönem ($)</td></tr>"
        "<tr><td></td><td>1.000</td><td>2.000</td><td>3.000</td><td>4.000</td></tr>"
        "</table></html>"
    )
    grid = parse_report_grid(html.encode("utf-8"), encoding="utf-8")
    series, uninterpreted = interpret_variable_report(
        grid, variable="Milliyet", frequency="quarterly"
    )
    assert series == []
    assert uninterpreted == []


def test_sorted_points_merges_duplicate_dates_without_comparing_none() -> None:
    points = [
        (date(2012, 1, 1), None),
        (date(2012, 1, 1), Decimal("5")),
        (date(2012, 4, 1), Decimal("2")),
    ]
    assert _sorted_points(points) == [
        (date(2012, 1, 1), Decimal("5")),
        (date(2012, 4, 1), Decimal("2")),
    ]


def test_distinct_series_count_collapses_year_chunks() -> None:
    chunk_a, _ = interpret_variable_report(
        parse_report_grid(INCOME_REPORT), variable="Milliyet", frequency="quarterly"
    )
    distinct = distinct_series_count(chunk_a)
    assert distinct == 4
    assert distinct_series_count([chunk_a[0], chunk_a[0]]) == 1
    other = replace(chunk_a[0], dataset="TUIK_OTHER")
    assert distinct_series_count([chunk_a[0], other]) == 2


class ScriptedClient:
    """A fake ZK client whose list boxes answer on scripted radio terms."""

    def bootstrap(self) -> None:
        self.checked: set[str] = set()

    def close(self) -> None:
        pass

    def take_raw_keys(self) -> list[str]:
        return []

    def check_radio(self, label: str) -> None:
        self.checked.add(label)

    def visible_checkboxes(self) -> list[str]:
        return []

    def visible_listboxes(self) -> list[str]:
        boxes = []
        if "Kapı-Milliyet seçimi" in self.checked:
            boxes.append("m")
        if "Kapı seçimi" in self.checked:
            boxes.append("g")
        return boxes

    def list_header(self, box_id: str) -> str:
        return {"m": "Milliyet Seçimi", "g": "Kapı-Yol Seçimi"}[box_id]

    def list_items(self, box_id: str) -> list[ListItem]:
        if box_id == "m":
            return [ListItem(id="i1", label="Almanya"), ListItem(id="i2", label="Fransa")]
        return [ListItem(id="j1", label="Adana - Botaş (Denizyolu)")]

    def select_items(self, box_id: str, ids: list[str]) -> None:
        pass

    def generate_report(self) -> tuple[bytes, str]:
        return b"", ""


def test_walk_family_merges_scripted_list_codes() -> None:
    family = CatalogFamily(
        code="TUIK_TURIZM_TEST_CATALOG",
        page="sinir",
        name="test",
        description="test",
        axes=(FormAxis("KIRILIM", "Kırılım", ("Kapı seçimi", "Kapı-Milliyet seçimi")),),
        explorations=(
            ("Yabancı", "Aylık", "Giriş", "Kapı-Milliyet seçimi"),
            ("Yabancı", "Aylık", "Giriş", "Kapı seçimi"),
        ),
    )
    result = walk_family(ScriptedClient(), family)
    assert set(result.codes["MILLIYET"]) == {"Almanya", "Fransa"}
    assert set(result.codes["KAPI"]) == {"Botaş"}
    assert set(result.codes["YOL"]) == {"Denizyolu"}


def test_headlines_reference_known_datasets() -> None:
    for codes in HEADLINES.values():
        assert all(code in FORM_PATH_BY_CODE for code in codes)


def test_year_helpers() -> None:
    assert _newest_years(["2025", "2024", "2023"], 2) == ["2025", "2024"]
    assert _newest_years(["2025", "2024"], None) == ["2025", "2024"]
    assert _years_from(date(2024, 1, 1), ["2025", "2024", "2023"]) == ["2025", "2024"]


# --- CLI ------------------------------------------------------------------


def test_turizm_parser_flags() -> None:
    args = build_parser().parse_args(["turizm-headlines", "--dry-run", "--years", "2"])
    assert args.command == "turizm-headlines"
    assert args.dry_run is True
    assert args.years == 2
    catalog = build_parser().parse_args(["turizm-catalog", "--dry-run", "--page", "sinir"])
    assert catalog.page == "sinir"
    discover = build_parser().parse_args(["turizm-discover", "--dry-run", "--page", "cikis"])
    assert discover.command == "turizm-discover"
    assert discover.dry_run is True
    assert discover.page == "cikis"


def test_turizm_routes_to_run_turizm(monkeypatch) -> None:
    calls: list[str] = []

    def fake_run(args, dry_run):
        calls.append(args.command)
        return 0

    monkeypatch.setattr(tuik_cli, "_run_turizm", fake_run)
    assert tuik_cli.main(["turizm-catalog", "--dry-run"]) == 0
    assert tuik_cli.main(["fetch", "--dataset", "TUIK_TURIZM_SINIR_KAPI", "--dry-run"]) == 0
    assert calls == ["turizm-catalog", "fetch"]


def test_record_report_registers_every_series_codes(monkeypatch) -> None:
    """Each series' codes are registered; merging kept only the last category."""
    from types import SimpleNamespace

    from app.connectors.tuik import __main__ as cli

    calls: list[dict[str, str]] = []

    def fake_upsert(session, dataset, codes):
        calls.append(dict(codes))
        return {"KATEGORI": 1}

    monkeypatch.setattr(cli, "upsert_discovered_codes", fake_upsert)
    monkeypatch.setattr(cli, "_load_dataset", lambda *a, **k: SimpleNamespace(id=1))
    monkeypatch.setattr(cli, "_record_headline", lambda *a, **k: 0)
    series = [
        SimpleNamespace(codes={"KATEGORI": "Okur yazar değil"}),
        SimpleNamespace(codes={"KATEGORI": "İlkokul"}),
    ]
    plan = SimpleNamespace(dataset_code="TUIK_TURIZM_CIKIS_GELIR_Q")
    _, added = cli._record_turizm_report(None, None, plan, series, 1)
    assert [c["KATEGORI"] for c in calls] == ["Okur yazar değil", "İlkokul"]
    assert added == {"KATEGORI": 2}
