"""Unit tests for the TCMB EVDS3 pure parsers (fixtures, no network)."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.connectors.base import FORMAT_CHANGED, ConnectorError
from app.connectors.tcmb.parsers import (
    frequency_for_label,
    parse_bounds,
    parse_catalog,
    parse_data,
    parse_period,
    parse_serie_list,
)
from app.data.periods import ANNUAL, DAILY, IRREGULAR, MONTHLY, QUARTERLY, WEEKLY

FIXTURES = Path(__file__).parent / "fixtures" / "tcmb"


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


CATALOG = _load("catalog.raw.json")
CLI2 = _load("serielist-bie_cli2.raw.json")
DKEF = _load("serielist-bie_dkefkytl.raw.json")
BOUNDS_USD = _load("bounds-usd.raw.json")
BOUNDS_CLI2 = _load("bounds-cli2.raw.json")
BOUNDS_EMPTY = _load("bounds-empty.raw.json")
FE_USD = _load("fe-usd.raw.json")
FE_CLI2 = _load("fe-cli2.raw.json")


def _serie_row(code: str, **overrides) -> dict:
    row = {
        "SERIE_CODE": code,
        "DATAGROUP_CODE": "g",
        "SERIE_NAME": "Label",
        "SERIE_NAME_ENG": "Label EN",
        "FREQUENCY_STR": "AYLIK",
        "DEFAULT_AGG_METHOD": "avg",
        "SCREEN_ORDER": 1,
        "AVGABLE": 1,
        "FIRSTABLE": 1,
        "LASTABLE": 1,
        "MAXABLE": 1,
        "MINABLE": 1,
        "SUMABLE": 0,
        "SEVIYE": 1,
        "UST_SERIE_CODE": "-1",
    }
    row.update(overrides)
    return row


# --- catalog ---------------------------------------------------------------


def test_parse_catalog_keeps_only_exact_cbrt_groups() -> None:
    groups = parse_catalog(CATALOG)
    assert [group.code for group in groups] == ["bie_cli2", "bie_dkefkytl"]
    assert all(group.data_source_en == "CBRT" for group in groups)


def test_parse_catalog_strips_trailing_tab_and_keeps_order() -> None:
    groups = parse_catalog(CATALOG)
    assert groups[0].category_title == "BİLEŞİK ÖNCÜ GÖSTERGELER ENDEKSİ (TCMB)"
    assert groups[0].screen_order == 10
    assert groups[1].screen_order == 20
    assert groups[1].unit == "Türk lirası"
    assert groups[1].source_frequency == "GÜNLÜK"


def test_parse_catalog_rejects_missing_category_key() -> None:
    with pytest.raises(ConnectorError) as excinfo:
        parse_catalog([{"DATAGROUPS": []}])
    assert excinfo.value.kind == FORMAT_CHANGED


def test_parse_catalog_rejects_non_array() -> None:
    with pytest.raises(ConnectorError) as excinfo:
        parse_catalog({"CATEGORY_ID": 1})
    assert excinfo.value.kind == FORMAT_CHANGED


HMB_SOURCE = "Ministry of Treasury and Finance"


def _category(cat_id, *, level=1, parent=-1, tr="Cat", en=None, groups=()) -> dict:
    return {
        "CATEGORY_ID": cat_id,
        "SEVIYE": level,
        "UST_CATEGORY_ID": parent,
        "TOPIC_TITLE_TR": tr,
        "TOPIC_TITLE_ENG": en,
        "DATAGROUPS": list(groups),
    }


def _group(code: str, *, source: str = "CBRT", **overrides) -> dict:
    row = {"DATAGROUP_CODE": code, "DATAGROUP_TYPE": code, "DATASOURCE_ENG": source}
    row.update(overrides)
    return row


def test_parse_catalog_dbdisborc_category_path() -> None:
    (group,) = parse_catalog(CATALOG, data_source=HMB_SOURCE)
    assert group.code == "bie_dbdisborc"
    path = group.category_path
    assert [entry.id for entry in path] == [0, 99970, 9997007]
    assert [entry.level for entry in path] == [1, 2, 3]
    assert path[0].title == "ARŞİV"
    assert path[0].title_en == "ARCHIVE"
    assert path[-1].title == "TÜRKİYE BRÜT DIŞ BORÇ STOKU (HMB) (ARŞİV)"


def test_parse_catalog_cli2_category_path() -> None:
    groups = parse_catalog(CATALOG)
    cli2 = next(group for group in groups if group.code == "bie_cli2")
    assert [(entry.id, entry.level) for entry in cli2.category_path] == [(15, 1), (1504, 2)]
    assert cli2.category_path[1].title == "BİLEŞİK ÖNCÜ GÖSTERGELER ENDEKSİ (TCMB)"


def test_parse_catalog_excludes_mixed_group_for_both_sources() -> None:
    for source in ("CBRT", HMB_SOURCE):
        codes = [group.code for group in parse_catalog(CATALOG, data_source=source)]
        assert "bie_yiesgyazdeg" not in codes
    assert [group.code for group in parse_catalog(CATALOG, data_source=HMB_SOURCE)] == [
        "bie_dbdisborc"
    ]


def test_parse_catalog_root_level_group_has_single_entry_path() -> None:
    payload = [_category(7, level=1, parent=-1, tr="ROOT", en="ROOT EN", groups=[_group("g")])]
    (group,) = parse_catalog(payload)
    assert [(entry.id, entry.level, entry.title) for entry in group.category_path] == [
        (7, 1, "ROOT")
    ]


def test_parse_catalog_category_cycle_is_format_changed() -> None:
    payload = [
        _category(1, level=2, parent=2, tr="A", groups=[_group("g")]),
        _category(2, level=2, parent=1, tr="B"),
    ]
    with pytest.raises(ConnectorError) as excinfo:
        parse_catalog(payload)
    assert excinfo.value.kind == FORMAT_CHANGED


def test_parse_catalog_missing_parent_ends_the_walk() -> None:
    payload = [_category(5, level=3, parent=999, tr="CHILD", groups=[_group("g")])]
    (group,) = parse_catalog(payload)
    assert [(entry.id, entry.title) for entry in group.category_path] == [(5, "CHILD")]


def test_parse_catalog_non_int_level_is_format_changed() -> None:
    payload = [_category(1, level="x", parent=-1, tr="A")]
    with pytest.raises(ConnectorError) as excinfo:
        parse_catalog(payload)
    assert excinfo.value.kind == FORMAT_CHANGED


# --- serie list ------------------------------------------------------------


def test_parse_serie_list_cli2_shape() -> None:
    series = parse_serie_list(CLI2, "bie_cli2")
    assert [serie.code for serie in series] == ["TP.CLI2.A01", "TP.CLI2.A02", "TP.CLI2.A03"]
    first = series[0]
    assert first.name == "MBONCU_SUE (Trend Kapsayan)"
    assert first.name_en == "MBONCU_SUE (Trend Restored)"
    assert first.frequency == MONTHLY
    assert first.source_frequency == "AYLIK"
    assert first.aggregation == "avg"
    assert first.aggregations == ["avg", "first", "last", "max", "min"]
    assert first.parent_code is None
    assert first.level == 1


def test_parse_serie_list_usd_is_daily() -> None:
    series = parse_serie_list(DKEF, "bie_dkefkytl")
    assert len(series) == 78
    assert series[0].code == "TP.DK.USD.A.EF.YTL"
    assert series[0].frequency == DAILY


def test_parse_serie_list_collapses_whitespace() -> None:
    payload = [_serie_row("X", SERIE_NAME="  A   B  ", SERIE_NAME_ENG="C\t\tD")]
    (serie,) = parse_serie_list(payload, "g")
    assert serie.name == "A B"
    assert serie.name_en == "C D"


def test_parse_serie_list_parent_minus_one_is_none() -> None:
    payload = [
        _serie_row("child", UST_SERIE_CODE="parent"),
        _serie_row("parent", UST_SERIE_CODE="-1"),
    ]
    series = {serie.code: serie for serie in parse_serie_list(payload, "g")}
    assert series["child"].parent_code == "parent"
    assert series["parent"].parent_code is None


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("GÜNLÜK", DAILY),
        ("İŞ GÜNÜ", DAILY),
        ("HAFTALIK(CUMA)", WEEKLY),
        ("AYDA İKİ KEZ", IRREGULAR),
        ("AYLIK", MONTHLY),
        ("ÜÇ AYLIK", QUARTERLY),
        ("YILLIK", ANNUAL),
    ],
)
def test_frequency_for_label_turkish(label: str, expected: str) -> None:
    assert frequency_for_label(label) == expected


def test_parse_serie_list_unknown_frequency_is_format_changed() -> None:
    with pytest.raises(ConnectorError) as excinfo:
        parse_serie_list([_serie_row("X", FREQUENCY_STR="SAATLİK")], "g")
    assert excinfo.value.kind == FORMAT_CHANGED


def test_parse_serie_list_unknown_aggregation_is_format_changed() -> None:
    with pytest.raises(ConnectorError) as excinfo:
        parse_serie_list([_serie_row("X", DEFAULT_AGG_METHOD="median")], "g")
    assert excinfo.value.kind == FORMAT_CHANGED


def test_parse_serie_list_missing_key_is_format_changed() -> None:
    row = _serie_row("X")
    del row["SERIE_NAME"]
    with pytest.raises(ConnectorError) as excinfo:
        parse_serie_list([row], "g")
    assert excinfo.value.kind == FORMAT_CHANGED


# --- bounds ----------------------------------------------------------------


def test_parse_bounds_usd() -> None:
    bounds = parse_bounds(BOUNDS_USD)
    assert bounds.start_date == date(2026, 5, 8)
    assert bounds.max_start_date == date(1990, 1, 2)
    assert bounds.min_end_date == date(2026, 10, 5)
    assert bounds.frequency_code == "1"


def test_parse_bounds_cli2_frequency() -> None:
    assert parse_bounds(BOUNDS_CLI2).frequency_code == "5"


def test_parse_bounds_empty_dates_are_none() -> None:
    bounds = parse_bounds(BOUNDS_EMPTY)
    assert bounds.max_start_date is None
    assert bounds.min_end_date is None
    assert bounds.frequency_code == "2"


def test_parse_bounds_unknown_frequency_is_format_changed() -> None:
    with pytest.raises(ConnectorError) as excinfo:
        parse_bounds({**BOUNDS_USD, "frequency": "99"})
    assert excinfo.value.kind == FORMAT_CHANGED


# --- data ------------------------------------------------------------------


def test_parse_data_usd_skips_nulls_and_gaps() -> None:
    points = parse_data(FE_USD, "TP.DK.USD.A.EF.YTL", "1")
    values = dict(points)
    assert len(points) == 10
    # 08/09/10 Jan 2000 are null (a weekend gap) and must be absent, never zero.
    assert date(2000, 1, 8) not in values
    assert date(2000, 1, 9) not in values
    assert date(2000, 1, 10) not in values
    assert values[date(2000, 1, 3)] == Decimal("0.5397")
    assert values[date(2026, 10, 1)] == Decimal("48.8960")
    assert values[date(2026, 10, 5)] == Decimal("48.9357")
    assert points == sorted(points)


def test_parse_data_monthly_period_labels() -> None:
    points = parse_data(FE_CLI2, "TP.CLI2.A01", "5")
    assert points[0] == (date(1987, 12, 1), Decimal("79.4000"))
    assert points[-1] == (date(2026, 8, 1), Decimal("408.1000"))


def test_parse_period_quarterly_and_yearly() -> None:
    assert parse_period("2000-1Ç", "6") == date(2000, 1, 1)
    assert parse_period("2000-2Ç", "6") == date(2000, 4, 1)
    assert parse_period("2000-3Ç", "6") == date(2000, 7, 1)
    assert parse_period("2000-4Ç", "6") == date(2000, 10, 1)
    assert parse_period("2000", "8") == date(2000, 1, 1)


def test_parse_data_quarterly_and_yearly_synthetic() -> None:
    quarterly = [
        {"Tarih": "2000-1Ç", "S_X": "1.0"},
        {"Tarih": "2000-2Ç", "S_X": "2.0"},
        {"Tarih": "2000-3Ç", "S_X": None},
        {"Tarih": "2000-4Ç", "S_X": "4.0"},
    ]
    assert parse_data({"items": quarterly}, "S.X", "6") == [
        (date(2000, 1, 1), Decimal("1.0")),
        (date(2000, 4, 1), Decimal("2.0")),
        (date(2000, 10, 1), Decimal("4.0")),
    ]
    yearly = [{"Tarih": "2000", "S_X": "9.0"}, {"Tarih": "2001", "S_X": "10.0"}]
    assert parse_data({"items": yearly}, "S.X", "8") == [
        (date(2000, 1, 1), Decimal("9.0")),
        (date(2001, 1, 1), Decimal("10.0")),
    ]


def test_parse_data_unknown_key_is_format_changed() -> None:
    items = [{"Tarih": "2000-1Ç", "S_X": "1.0", "OTHER": "x"}]
    with pytest.raises(ConnectorError) as excinfo:
        parse_data({"items": items}, "S.X", "6")
    assert excinfo.value.kind == FORMAT_CHANGED


def test_parse_data_missing_column_is_format_changed() -> None:
    with pytest.raises(ConnectorError) as excinfo:
        parse_data({"items": [{"Tarih": "2000-1Ç"}]}, "S.X", "6")
    assert excinfo.value.kind == FORMAT_CHANGED


def test_parse_data_bad_value_is_format_changed() -> None:
    items = [{"Tarih": "2000-1Ç", "S_X": "1,5"}]
    with pytest.raises(ConnectorError) as excinfo:
        parse_data({"items": items}, "S.X", "6")
    assert excinfo.value.kind == FORMAT_CHANGED


def test_parse_data_duplicate_period_is_format_changed() -> None:
    items = [{"Tarih": "2000", "S_X": "1.0"}, {"Tarih": "2000", "S_X": "2.0"}]
    with pytest.raises(ConnectorError) as excinfo:
        parse_data({"items": items}, "S.X", "8")
    assert excinfo.value.kind == FORMAT_CHANGED
