"""Unit tests for the TÜİK databrowser2 parsers (recorded fixtures, no network)."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.connectors.base import FORMAT_CHANGED, ConnectorError
from app.connectors.tuik.parsers import (
    frequency_for_code,
    frequency_from_period,
    is_dataflow_not_found,
    is_empty_dataset,
    is_throttle_response,
    jsonstat_time_dimension,
    parse_catalog,
    parse_dimension_codelist,
    parse_hidden_dimensions,
    parse_period_start,
    parse_sdmx_csv_points,
    parse_structure,
    parse_time_coverage,
    parse_value,
    points_from_jsonstat,
    selectable_codes,
    series_metas_from_jsonstat,
    series_metas_from_sdmx_json,
)

FIXTURES = Path(__file__).parent / "fixtures" / "tuik"

TUFE_DATA = "tufe-tt10.databrowser2-data.raw.json"
TUFE_CSV = "tufe-tt10.databrowser2-download.raw.csv"
GSYH_DATA = "uh-bh-gsyh-cari.databrowser2-data.raw.json"
GSYH_CSV = "uh-bh-gsyh-cari.databrowser2-download.raw.csv"
QS_DATA = "isgucu-ceyrek-tamamlayici.databrowser2-data.raw.json"
QS_CSV = "isgucu-ceyrek-tamamlayici.databrowser2-download.raw.csv"
KSH_CSV = "kurumsal-sektor-cari.databrowser2-download.raw.csv"
FIIL_DATA = "isgucu-fiil-calisma-sure.databrowser2-data.raw.json"
ADNKS_DATA = "adnks-t28.databrowser2-data.raw.json"
BOLGESEL_FIYAT_DATA = "bolgesel-fiyat-duzeyi.databrowser2-data.raw.json"
EMPTY_JSON = "ibbs3-09-13.empty-refarea.raw.json"
CODELIST_JSON = "ibbs3-09-13.refarea-codelist.raw.json"
TR100_JSON = "ibbs3-09-13.tr100.databrowser2-data.raw.json"
CATALOG_JSON = "databrowser2-catalog.raw.json"
THROTTLE_HTML = "databrowser2-throttle.raw.html"
DNF_JSON = "tufe-tt10.dataflow-not-found.raw.json"

TUFE_KEY = {
    "REF_AREA": "TR",
    "FREQ": "M",
    "SINIFLAMA_DUZEYI": "TUFE",
    "DEGISIM": "1",
    "BASE_PER": "2025",
    "COICOP_2018": "0",
    "INDICATOR": "F_TFE",
}
GSYH_KEY = {
    "REF_AREA": "TR",
    "FREQ": "A",
    "KIRILIM_SEVIYE": "A10",
    "FAALIYET_KOD": "B1GQ",
    "UNIT_MEASURE": "TTRY",
    "BAZ_YILI": "2009",
}
QS_KEY = {
    "REF_AREA": "TR",
    "INDICATOR": "ISGI_ITG",
    "TEMEL_YAS": "Y_GE15",
    "SEASONAL_ADJUST": "N",
    "ISGUCU_GOSTERGE": "10",
    "FREQ": "Q",
    "UNIT_MEASURE": "RO",
    "SEX": "_T",
}


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def fixture_json(name: str) -> dict:
    return json.loads(fixture_bytes(name).decode("utf-8"))


@pytest.mark.parametrize(
    ("raw", "expected", "frequency"),
    [
        ("2025-01", date(2025, 1, 1), "monthly"),
        ("2024", date(2024, 1, 1), "annual"),
        ("2024-Q4", date(2024, 10, 1), "quarterly"),
        ("2024-S2", date(2024, 7, 1), "semiannual"),
        ("2025-W03", date.fromisocalendar(2025, 3, 1), "weekly"),
        ("2025-01-15", date(2025, 1, 15), "daily"),
    ],
)
def test_parse_period_start_every_frequency(raw: str, expected: date, frequency: str) -> None:
    assert parse_period_start(raw) == expected
    assert frequency_from_period(raw) == frequency


def test_parse_period_start_rejects_unknown_text() -> None:
    with pytest.raises(ConnectorError) as excinfo:
        parse_period_start("not-a-period")
    assert excinfo.value.kind == FORMAT_CHANGED


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("3.596523", Decimal("3.596523")),
        (" 12 ", Decimal("12")),
        ("", None),
        (".", None),
        (None, None),
        (5, Decimal("5")),
        ("1.234,5", Decimal("1234.5")),
    ],
)
def test_parse_value(raw: object, expected: Decimal | None) -> None:
    assert parse_value(raw) == expected


def test_parse_value_rejects_garbage() -> None:
    with pytest.raises(ConnectorError) as excinfo:
        parse_value("hello")
    assert excinfo.value.kind == FORMAT_CHANGED


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("M", "monthly"),
        ("Q", "quarterly"),
        ("A", "annual"),
        ("A2", "biennial"),
        ("S", "semiannual"),
    ],
)
def test_frequency_for_code(code: str, expected: str) -> None:
    assert frequency_for_code(code) == expected


def test_csv_points_monthly() -> None:
    points = parse_sdmx_csv_points(fixture_bytes(TUFE_CSV), TUFE_KEY)
    assert len(points) == 260
    assert points[0] == (date(2005, 1, 1), Decimal("3.596523"))
    assert points[-1] == (date(2026, 8, 1), Decimal("134.75"))


def test_csv_points_annual() -> None:
    points = parse_sdmx_csv_points(fixture_bytes(GSYH_CSV), GSYH_KEY)
    assert len(points) == 25
    assert points[0] == (date(2000, 1, 1), Decimal("171777959.413194"))
    assert points[-1] == (date(2024, 1, 1), Decimal("44587225440.1984"))


def test_csv_points_quarterly() -> None:
    points = parse_sdmx_csv_points(fixture_bytes(QS_CSV), QS_KEY)
    assert len(points) == 50
    assert points[0] == (date(2014, 1, 1), Decimal("16.3"))
    assert points[-1] == (date(2026, 4, 1), Decimal("19.5"))


def test_csv_missing_obs_value_is_none_never_zero() -> None:
    key = {
        "INDICATOR": "UH_KSH_CF",
        "UNIT_MEASURE": "TTRY",
        "KAYNAKLAR_KULLANIMLAR": "C",
        "HESAPLAR": "1",
        "REF_SEKTOR": "S1",
    }
    points = parse_sdmx_csv_points(fixture_bytes(KSH_CSV), key)
    assert len(points) == 16
    assert all(value is None for _, value in points)


def test_jsonstat_points_match_csv_for_the_same_series() -> None:
    from_csv = parse_sdmx_csv_points(fixture_bytes(TUFE_CSV), TUFE_KEY)
    from_json = points_from_jsonstat(fixture_json(TUFE_DATA), TUFE_KEY)
    assert from_json == from_csv


def test_jsonstat_points_include_null_for_empty_values() -> None:
    key = {
        "INDICATOR": "UH_KSH_CF",
        "UNIT_MEASURE": "TTRY",
        "KAYNAKLAR_KULLANIMLAR": "C",
        "HESAPLAR": "1",
        "REF_SEKTOR": "S1",
    }
    points = points_from_jsonstat(
        fixture_json("kurumsal-sektor-cari.databrowser2-data.raw.json"), key
    )
    assert points and all(value is None for _, value in points)


def test_series_metas_monthly_tufe() -> None:
    payload = fixture_json(TUFE_DATA)
    metas = series_metas_from_jsonstat(
        payload,
        dataflow_id="DF_TUFE_SDMX_TT10",
        dataflow_version="1.0",
        title="Index numbers and rate of changes in the consumer price index",
        source_category="Price Statistics / Consumer Price Index (CPI)",
    )
    assert len(metas) == 5
    codes = {meta.external_code for meta in metas}
    assert "DF_TUFE_SDMX_TT10:TR.M.TUFE.1.2025.0.F_TFE" in codes
    assert all(meta.frequency == "monthly" for meta in metas)
    assert all(meta.coverage_start == date(2005, 1, 1) for meta in metas)
    assert all(meta.coverage_end == date(2026, 8, 1) for meta in metas)
    assert all(meta.unit is None for meta in metas)
    index_meta = next(m for m in metas if m.external_code.endswith(".1.2025.0.F_TFE"))
    assert index_meta.breakdown["BASE_PER"]["code"] == "2025"
    assert index_meta.breakdown["DEGISIM"]["label"] == "Index"
    assert "Türkiye" in index_meta.name
    assert index_meta.attributes["dataflow_version"] == "1.0"
    assert index_meta.attributes["channel"] == "databrowser2"
    assert index_meta.source_category == "Price Statistics / Consumer Price Index (CPI)"


def test_series_metas_annual_gdp_current_prices() -> None:
    metas = series_metas_from_jsonstat(
        fixture_json(GSYH_DATA),
        dataflow_id="UH_BH_GSYH_CARI",
        dataflow_version="1.0",
        title="Regional gross domestic product (at current prices)",
    )
    assert len(metas) == 40
    target = next(m for m in metas if m.external_code == "UH_BH_GSYH_CARI:TR.A.A10.B1GQ.TTRY.2009")
    assert target.frequency == "annual"
    assert target.unit == "Thousand TRY"
    assert target.coverage_start == date(2000, 1, 1)
    assert target.coverage_end == date(2024, 1, 1)
    assert target.attributes["value_count"] == 25


def test_series_metas_quarterly_infers_frequency_from_freq_dimension() -> None:
    metas = series_metas_from_jsonstat(
        fixture_json(QS_DATA),
        dataflow_id="DF_ISGUCU_CEYREK_TAMAMLAYICI_GOSTERGE_C",
        dataflow_version="1.0",
        title="Supplementary indicators",
    )
    assert metas
    assert all(meta.frequency == "quarterly" for meta in metas)
    assert any(m.external_code.endswith("TR.ISGI_ITG.Y_GE15.N.10.Q.RO._T") for m in metas)


def test_series_metas_for_empty_response_is_empty() -> None:
    payload = fixture_json(EMPTY_JSON)
    assert is_empty_dataset(payload)
    metas = series_metas_from_jsonstat(
        payload,
        dataflow_id="DF_BR_FAALIYET_BUYUKLUK_GIRISIM_IBBS3_09_13_V1",
        dataflow_version="1.0",
        title="Number of Enterprises",
    )
    assert metas == []


def test_ref_area_code_discovery() -> None:
    payload = fixture_json(CODELIST_JSON)
    codes = selectable_codes(payload, "REF_AREA")
    assert len(codes) == 81
    assert "TR100" in codes
    assert "TR" not in codes


def test_series_metas_from_regional_response_includes_ref_area_in_code() -> None:
    metas = series_metas_from_jsonstat(
        fixture_json(TR100_JSON),
        dataflow_id="DF_BR_FAALIYET_BUYUKLUK_GIRISIM_IBBS3_09_13_V1",
        dataflow_version="1.0",
        title="Number of Enterprises",
    )
    assert len(metas) == 2101
    assert all("TR100" in meta.external_code for meta in metas)
    assert all(meta.frequency == "annual" for meta in metas)
    assert all(meta.coverage_start == date(2013, 1, 1) for meta in metas)


def test_catalog_parsing_uses_live_versions_and_category_paths() -> None:
    catalog = parse_catalog(fixture_json(CATALOG_JSON))
    assert len(catalog.dataflows) == 467
    tufe = catalog.get("DF_TUFE_SDMX_TT10")
    assert tufe is not None
    assert tufe.version == "1.0"
    assert tufe.dataset_identifier == "TR,DF_TUFE_SDMX_TT10,1.0"
    assert tufe.source_category == (
        "Classification of Statistical Activities - Rev.1.0 / Price Statistics / "
        "Consumer Price Index (CPI)"
    )
    assert catalog.get("DF_BD_HAYATTA_KALMA").version == "1.2"


def test_throttle_page_detection() -> None:
    html = fixture_bytes(THROTTLE_HTML)
    assert is_throttle_response("text/html", html)
    assert is_throttle_response(None, html)
    assert not is_throttle_response("application/json", b'{"id": []}')


def test_dataflow_not_found_detection() -> None:
    assert is_dataflow_not_found(fixture_bytes(DNF_JSON))
    assert not is_dataflow_not_found(b'{"id": []}')
    assert not is_dataflow_not_found(b"<html>")


def _small_payload(**overrides: object) -> dict:
    payload: dict = {
        "id": ["REF_AREA", "FREQ", "TIME_PERIOD"],
        "size": [1, 1, 2],
        "role": {"time": ["TIME_PERIOD"], "geo": ["REF_AREA"]},
        "dimension": {
            "REF_AREA": {
                "label": "Reference area",
                "category": {"index": {"TR": 0}, "label": {"TR": "Türkiye"}},
            },
            "FREQ": {
                "label": "Frequency of observation",
                "category": {"index": {"A": 0}, "label": {"A": "Annual"}},
            },
            "TIME_PERIOD": {
                "label": "Time period",
                "category": {"index": {"2023": 0, "2024": 1}},
            },
        },
        "value": {"0": "1.5", "1": "2.5"},
    }
    payload.update(overrides)
    return payload


def test_jsonstat_series_metas_without_time_dimension_in_id() -> None:
    payload = fixture_json(FIIL_DATA)
    assert jsonstat_time_dimension(payload) is None
    metas = series_metas_from_jsonstat(
        payload,
        dataflow_id="DF_ISGUCU_CEYREK_FIIL_CALISMA_SURE_C",
        dataflow_version="1.0",
        title="Actual working hours of employed",
    )
    assert len(metas) == 24
    target = next(
        meta
        for meta in metas
        if meta.external_code
        == "DF_ISGUCU_CEYREK_FIIL_CALISMA_SURE_C:TR.ISGI_ISFCS.Y_GE15.N.Q._T.1"
    )
    assert target.frequency == "quarterly"
    assert target.coverage_start is None
    assert target.coverage_end is None
    assert target.attributes["time_dimension"] is None
    assert target.attributes["value_count"] == 1


def test_jsonstat_points_reject_response_without_time_dimension() -> None:
    with pytest.raises(ConnectorError) as excinfo:
        points_from_jsonstat(fixture_json(FIIL_DATA), {"REF_AREA": "TR"})
    assert excinfo.value.kind == FORMAT_CHANGED


def test_jsonstat_time_dimension_ignores_role_entry_absent_from_id() -> None:
    payload = _small_payload(value={"0": "1.5"})
    payload["id"] = ["REF_AREA", "FREQ"]
    payload["size"] = [1, 1]
    payload["dimension"].pop("TIME_PERIOD")
    assert jsonstat_time_dimension(payload) is None
    metas = series_metas_from_jsonstat(
        payload, dataflow_id="DF_X", dataflow_version="1.0", title="X"
    )
    assert [meta.external_code for meta in metas] == ["DF_X:TR.A"]
    assert metas[0].coverage_start is None


def test_jsonstat_dense_value_list_is_supported() -> None:
    points = points_from_jsonstat(
        _small_payload(value=["1.5", None]), {"REF_AREA": "TR", "FREQ": "A"}
    )
    assert points == [(date(2023, 1, 1), Decimal("1.5")), (date(2024, 1, 1), None)]

    metas = series_metas_from_jsonstat(
        _small_payload(value=["1.5", "2.5"]),
        dataflow_id="DF_X",
        dataflow_version="1.0",
        title="X",
    )
    assert len(metas) == 1
    assert metas[0].coverage_start == date(2023, 1, 1)
    assert metas[0].coverage_end == date(2024, 1, 1)
    assert metas[0].attributes["value_count"] == 2


def test_jsonstat_null_cells_do_not_extend_coverage() -> None:
    metas = series_metas_from_jsonstat(
        _small_payload(value={"0": "1.5", "1": None}),
        dataflow_id="DF_X",
        dataflow_version="1.0",
        title="X",
    )
    assert len(metas) == 1
    assert metas[0].coverage_start == date(2023, 1, 1)
    assert metas[0].coverage_end == date(2023, 1, 1)
    assert metas[0].attributes["value_count"] == 1


def test_jsonstat_all_null_values_produce_no_series() -> None:
    metas = series_metas_from_jsonstat(
        _small_payload(value={"0": None, "1": ""}),
        dataflow_id="DF_X",
        dataflow_version="1.0",
        title="X",
    )
    assert metas == []


def test_jsonstat_empty_value_container() -> None:
    payload = _small_payload(value={})
    assert points_from_jsonstat(payload, {"REF_AREA": "TR", "FREQ": "A"}) == []
    metas = series_metas_from_jsonstat(
        payload, dataflow_id="DF_X", dataflow_version="1.0", title="X"
    )
    assert metas == []


def test_jsonstat_missing_labels_falls_back_to_codes() -> None:
    payload = _small_payload()
    assert "label" not in payload["dimension"]["TIME_PERIOD"]["category"]
    points = points_from_jsonstat(payload, {"REF_AREA": "TR", "FREQ": "A"})
    assert points[0] == (date(2023, 1, 1), Decimal("1.5"))


def test_jsonstat_size_mismatch_raises_format_changed() -> None:
    payload = _small_payload(size=[1, 1])
    with pytest.raises(ConnectorError) as excinfo:
        series_metas_from_jsonstat(payload, dataflow_id="DF_X", dataflow_version="1.0", title="X")
    assert excinfo.value.kind == FORMAT_CHANGED


def test_jsonstat_missing_category_codes_raises_format_changed() -> None:
    payload = _small_payload()
    payload["dimension"]["FREQ"] = {"label": "Frequency of observation"}
    with pytest.raises(ConnectorError) as excinfo:
        series_metas_from_jsonstat(payload, dataflow_id="DF_X", dataflow_version="1.0", title="X")
    assert excinfo.value.kind == FORMAT_CHANGED


def test_jsonstat_non_object_payload_raises_format_changed() -> None:
    with pytest.raises(ConnectorError):
        series_metas_from_jsonstat([], dataflow_id="DF_X", dataflow_version="1.0", title="X")
    with pytest.raises(ConnectorError):
        points_from_jsonstat([], {"REF_AREA": "TR"})


def test_jsonstat_unsupported_value_type_raises_format_changed() -> None:
    payload = _small_payload(value="not-a-value-container")
    with pytest.raises(ConnectorError) as excinfo:
        points_from_jsonstat(payload, {"REF_AREA": "TR", "FREQ": "A"})
    assert excinfo.value.kind == FORMAT_CHANGED


def test_jsonstat_bad_value_index_raises_format_changed() -> None:
    payload = _small_payload(value={"first": "1.5"})
    with pytest.raises(ConnectorError) as excinfo:
        points_from_jsonstat(payload, {"REF_AREA": "TR", "FREQ": "A"})
    assert excinfo.value.kind == FORMAT_CHANGED


def test_series_metas_irregular_freq_falls_back_to_time_period_shape() -> None:
    metas = series_metas_from_jsonstat(
        fixture_json(ADNKS_DATA),
        dataflow_id="DF_ADNKS_T28",
        dataflow_version="1.1",
        title="Median age by years and sex",
    )
    assert metas
    irregular = [meta for meta in metas if meta.attributes["frequency_code"] == "I"]
    assert irregular
    assert all(meta.frequency == "annual" for meta in irregular)
    assert all(meta.coverage_start is not None for meta in irregular)
    annual = [meta for meta in metas if meta.attributes["frequency_code"] == "A"]
    assert annual and all(meta.frequency == "annual" for meta in annual)


def test_series_metas_occasional_annual_freq_falls_back_to_annual() -> None:
    metas = series_metas_from_jsonstat(
        fixture_json(BOLGESEL_FIYAT_DATA),
        dataflow_id="DF_BOLGESEL_FIYAT_DUZEYI_TUKETIM_ANA_GRUPLAR_V1",
        dataflow_version="1.0",
        title="Regional price levels",
    )
    assert metas
    assert all(meta.frequency == "annual" for meta in metas)
    assert all(meta.attributes["frequency_code"] == "OA" for meta in metas)


MERKEZI_DOWNLOAD = "merkezi-yonetim-butcesi.download-json.raw.json"
MERKEZI_ORDER = ["ULUSLARASI_ARGE_PROGRAM", "INDICATOR", "FREQ", "REF_AREA"]


def test_sdmx_series_metas_are_complete_and_carry_labels() -> None:
    payload = fixture_json(MERKEZI_DOWNLOAD)
    metas, observed = series_metas_from_sdmx_json(
        payload,
        dataflow_id="DF_MERKEZI_YONETIM_BUTCESI",
        dataflow_version="1.0",
        title="National public funding",
        order=MERKEZI_ORDER,
    )
    assert observed == 12
    assert len(metas) == 4
    target = next(
        meta
        for meta in metas
        if meta.external_code == "DF_MERKEZI_YONETIM_BUTCESI:_T.BTY_ARGEFAKF.A.TR"
    )
    assert target.frequency == "annual"
    assert target.coverage_start == date(2024, 1, 1)
    assert target.coverage_end == date(2026, 1, 1)
    assert target.breakdown["ULUSLARASI_ARGE_PROGRAM"]["label"] == "Total"
    assert target.attributes["value_count"] == 3
    assert all(meta.attributes["value_count"] == 3 for meta in metas)


def test_sdmx_series_metas_reject_missing_order_dimension() -> None:
    payload = fixture_json(MERKEZI_DOWNLOAD)
    with pytest.raises(ConnectorError) as excinfo:
        series_metas_from_sdmx_json(
            payload,
            dataflow_id="DF_X",
            dataflow_version="1.0",
            title="X",
            order=[*MERKEZI_ORDER, "NOT_A_DIMENSION"],
        )
    assert excinfo.value.kind == FORMAT_CHANGED


def test_sdmx_series_metas_reject_non_object_payload() -> None:
    with pytest.raises(ConnectorError) as excinfo:
        series_metas_from_sdmx_json(
            [], dataflow_id="DF_X", dataflow_version="1.0", title="X", order=[]
        )
    assert excinfo.value.kind == FORMAT_CHANGED


MERKEZI_STRUCTURE = "merkezi-yonetim-butcesi.structure.raw.json"
MERKEZI_PARTIAL = "merkezi-yonetim-butcesi.partial-ULUSLARASI_ARGE_PROGRAM.raw.json"
IBBS_CODELIST = "ibbs3-09-13.refarea-codelist.raw.json"


def test_parse_structure_lists_visible_dimensions_and_hidden_separately() -> None:
    dimensions, time_dim = parse_structure(fixture_json(MERKEZI_STRUCTURE))
    assert time_dim == "TIME_PERIOD"
    codes = [dimension.code for dimension in dimensions]
    assert codes == [
        "ULUSLARASI_ARGE_PROGRAM",
        "INDICATOR",
        "FREQ",
        "REF_AREA",
        "TIME_PERIOD",
    ]
    assert parse_hidden_dimensions(fixture_json(MERKEZI_STRUCTURE)) == ["SOSYO_EKO_HEDEF"]
    tufe_dims, tufe_time = parse_structure(
        {
            "timeDimension": "TIME_PERIOD",
            "criteria": [
                {
                    "id": "REF_AREA",
                    "label": "Reference area",
                    "extra": {"DataStructureRef": "TR+CL_IBBS+1.2"},
                },
                {"id": "TIME_PERIOD", "label": "Time period"},
            ],
        }
    )
    assert tufe_time == "TIME_PERIOD"
    assert tufe_dims[0].dsd_ref == "TR+CL_IBBS+1.2"
    assert tufe_dims[1].position == 1


def test_parse_dimension_codelist_reads_labels_and_defaults() -> None:
    data = parse_dimension_codelist(fixture_json(MERKEZI_PARTIAL), "ULUSLARASI_ARGE_PROGRAM")
    assert data.obs_count == 12
    assert data.dimension_label
    assert [entry.code for entry in data.entries] == ["_T", "1", "2", "3"]
    assert data.entries[0].label == "Total"
    assert data.entries[0].is_selectable is True
    assert all(entry.parent_code is None for entry in data.entries)


def test_parse_dimension_codelist_reads_source_parent() -> None:
    data = parse_dimension_codelist(fixture_json(IBBS_CODELIST), "REF_AREA")
    by_code = {entry.code: entry for entry in data.entries}
    assert by_code["TR100"].parent_code == "TR10"
    assert by_code["TR100"].is_selectable is True
    assert by_code["TR"].parent_code is None


def test_parse_time_coverage_reads_start_and_end() -> None:
    payload = {
        "criteria": [
            {
                "id": "TIME_PERIOD",
                "label": "Time Dimension Start and End periods",
                "values": [
                    {"id": "2005-01-01", "name": "Start Time period", "isSelectable": False},
                    {"id": "2026-08-31", "name": "End Time period", "isSelectable": False},
                ],
            }
        ]
    }
    start, end = parse_time_coverage(payload)
    assert start == date(2005, 1, 1)
    assert end == date(2026, 8, 31)
