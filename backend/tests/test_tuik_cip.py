"""Unit tests for the TÜİK CİP connector (no network; captured fixtures)."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.connectors.base import (
    EMPTY,
    FORMAT_CHANGED,
    NOT_FOUND,
    TIMEOUT,
    ConnectorError,
    InMemoryObjectStore,
    build_series_definition,
)
from app.connectors.tuik.cip import (
    CipClient,
    CipConnector,
    frequency_code,
    is_error_payload,
    parse_geometry,
    parse_map_data,
    parse_side_menu,
    period_start,
    unit_hint,
)

FIXTURES = Path(__file__).parent / "fixtures" / "tuik" / "cip"

SIDE_MENU = "sideMenu.raw.json"
ADNKS_L1 = "ADNKS-GK137473-O29001.duzey1.raw.json"
ADNKS_L3 = "ADNKS-GK137473-O29001.duzey3.raw.json"
ADNKS_L4 = "ADNKS-GK137473-O29001.duzey4.raw.json"
ENR_L3 = "ENR-GK054-O0015.duzey3.raw.json"
ULS_L3 = "ULS-GK111403-O12001.duzey3.raw.json"
BTUR_L4 = "BTUR-GK4056122-O32001.duzey4.raw.json"
SES_ERROR = "ses123.error.raw.json"
NUTS2 = "nuts2.raw.json"
NUTS3 = "nuts3.raw.json"
NUTS4 = "nuts4.raw.json"

ADNKS = "ADNKS-GK137473-O29001"
ENR = "ENR-GK054-O0015"
ULS = "ULS-GK111403-O12001"
BTUR = "BTUR-GK4056122-O32001"
LEVEL_FIXTURES = {
    (ADNKS, 1): ADNKS_L1,
    (ADNKS, 3): ADNKS_L3,
    (ADNKS, 4): ADNKS_L4,
    (ENR, 3): ENR_L3,
    (ULS, 3): ULS_L3,
    (BTUR, 4): BTUR_L4,
}


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def fixture_json(name: str):
    return json.loads(fixture_bytes(name))


# --- pure parsers -----------------------------------------------------------


def test_parse_side_menu_entries_and_kaynak_counts() -> None:
    entries = parse_side_menu(fixture_json(SIDE_MENU))
    assert len(entries) == 80
    by_no = {entry.gosterge_no: entry for entry in entries}
    assert len(by_no) == 80
    # Level 5 (never served) is dropped from the advertised levels.
    assert all(5 not in entry.levels for entry in entries)
    counts = {}
    for entry in entries:
        counts[entry.kaynak] = counts.get(entry.kaynak, 0) + 1
    assert counts == {"medas": 50, "ilGostergeleri": 29, "json": 1}
    enr = by_no[ENR]
    assert enr.kaynak == "ilGostergeleri"
    assert enr.levels == (3,)
    assert enr.period == "yillik"
    assert enr.menu_name == "Çevre ve Enerji"


def test_parse_side_menu_rejects_non_success() -> None:
    with pytest.raises(ConnectorError) as excinfo:
        parse_side_menu({"status": "Error"})
    assert excinfo.value.kind == FORMAT_CHANGED


def test_parse_map_data_level3() -> None:
    data = parse_map_data(fixture_json(ADNKS_L3))
    assert data.gosterge_no == ADNKS
    assert data.period == "yillik"
    assert len(data.periods) == 19
    assert len(data.units) == 81
    kirklareli = next(unit for unit in data.units if unit.code == "39")
    assert kirklareli.values[0] == "379595"


def test_parse_map_data_error_body_raises_not_found() -> None:
    with pytest.raises(ConnectorError) as excinfo:
        parse_map_data(fixture_json(SES_ERROR))
    assert excinfo.value.kind == NOT_FOUND
    assert "Object reference" in excinfo.value.message


def test_is_error_payload() -> None:
    assert is_error_payload(fixture_json(SES_ERROR)) is True
    assert is_error_payload({"tarihler": [], "veriler": []}) is False


def test_parse_map_data_empty_units_is_empty() -> None:
    with pytest.raises(ConnectorError) as excinfo:
        parse_map_data({"gostergeNo": "X", "tarihler": ["2024"], "veriler": []})
    assert excinfo.value.kind == EMPTY


def test_period_start_annual_and_monthly() -> None:
    assert period_start("2025", "yillik") == date(2025, 1, 1)
    assert period_start("2026/8", "aylik") == date(2026, 8, 1)
    with pytest.raises(ConnectorError) as excinfo:
        period_start("2025/0", "aylik")
    assert excinfo.value.kind == FORMAT_CHANGED


def test_parse_geometry_exposes_labels_and_regions() -> None:
    nuts3 = parse_geometry(fixture_json(NUTS3))
    aydin = nuts3["9"]
    assert aydin.label == "AYDIN"
    assert aydin.region_code == "TR32"
    assert aydin.nuts_code == "TR321"
    nuts4 = parse_geometry(fixture_json(NUTS4))
    assert nuts4["1486"].label == "KOZAN"
    assert nuts4["1486"].region_code == "TR62"
    nuts2 = parse_geometry(fixture_json(NUTS2))
    assert len(nuts2) == 26


def test_frequency_code_and_unit_hint() -> None:
    assert frequency_code("yillik") == "A"
    assert frequency_code("aylik") == "M"
    assert frequency_code("unknown") == "A"
    assert unit_hint("Kişi Başına Elektrik Tüketimi (kWh)") == "kWh"
    assert unit_hint("Hastane sayısı") is None


# --- client -----------------------------------------------------------------


def _map_handler(name: str, *, status: int = 200, calls: list[int] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(1)
        return httpx.Response(
            status, content=fixture_bytes(name), headers={"content-type": "application/json"}
        )

    return handler


def test_client_map_data_parses_and_stores_raw() -> None:
    store = InMemoryObjectStore()
    http_client = httpx.Client(transport=httpx.MockTransport(_map_handler(ADNKS_L3)))
    client = CipClient(http_client=http_client, store=store, sleeper=lambda _: None)
    response = client.map_data(
        kaynak="medas", duzey=3, gosterge_no=ADNKS, kayit_sayisi=200, period="yillik"
    )
    assert response.status_code == 200
    assert parse_map_data(response.json()).gosterge_no == ADNKS
    assert response.raw_object_key in store.objects
    assert "/" + ADNKS + "/" in response.raw_object_key


def test_client_retries_server_error_then_succeeds() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503, content=b"busy")
        return httpx.Response(
            200, content=fixture_bytes(ADNKS_L3), headers={"content-type": "application/json"}
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = CipClient(http_client=http_client, store=None, sleeper=lambda _: None)
    response = client.map_data(
        kaynak="medas", duzey=3, gosterge_no=ADNKS, kayit_sayisi=200, period="yillik"
    )
    assert response.status_code == 200
    assert len(calls) == 2


def test_client_timeout_raises_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = CipClient(http_client=http_client, store=None, sleeper=lambda _: None)
    with pytest.raises(ConnectorError) as excinfo:
        client.map_data(
            kaynak="medas", duzey=3, gosterge_no=ADNKS, kayit_sayisi=200, period="yillik"
        )
    assert excinfo.value.kind == TIMEOUT


def test_client_http_404_is_not_found() -> None:
    http_client = httpx.Client(transport=httpx.MockTransport(_map_handler(ADNKS_L3, status=404)))
    client = CipClient(http_client=http_client, store=None, sleeper=lambda _: None)
    with pytest.raises(ConnectorError) as excinfo:
        client.map_data(
            kaynak="medas", duzey=3, gosterge_no=ADNKS, kayit_sayisi=200, period="yillik"
        )
    assert excinfo.value.kind == NOT_FOUND


# --- connector --------------------------------------------------------------


def _cip_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/sideMenu.json"):
        return httpx.Response(
            200, content=fixture_bytes(SIDE_MENU), headers={"content-type": "application/json"}
        )
    if path.endswith("nuts2.json"):
        return httpx.Response(
            200, content=fixture_bytes(NUTS2), headers={"content-type": "application/json"}
        )
    if path.endswith("nuts3.json"):
        return httpx.Response(
            200, content=fixture_bytes(NUTS3), headers={"content-type": "application/json"}
        )
    if path.endswith("nuts4.json"):
        return httpx.Response(
            200, content=fixture_bytes(NUTS4), headers={"content-type": "application/json"}
        )
    if path.endswith("/Home/GetMapData"):
        params = request.url.params
        level = int(params["duzey"])
        key = (params["gostergeNo"], level)
        if key in LEVEL_FIXTURES:
            return httpx.Response(
                200,
                content=fixture_bytes(LEVEL_FIXTURES[key]),
                headers={"content-type": "application/json"},
            )
        return httpx.Response(
            200, content=fixture_bytes(SES_ERROR), headers={"content-type": "application/json"}
        )
    raise AssertionError(f"unexpected path {path}")


def _connector() -> tuple[CipConnector, InMemoryObjectStore]:
    store = InMemoryObjectStore()
    http_client = httpx.Client(transport=httpx.MockTransport(_cip_handler))
    client = CipClient(http_client=http_client, store=store, sleeper=lambda _: None)
    return CipConnector(client=client), store


def test_dataset_meta_adnks_verifies_levels_and_hierarchy() -> None:
    connector, _ = _connector()
    meta = connector.dataset_meta(f"CIP_{ADNKS}")
    assert meta.external_code == f"CIP_{ADNKS}"
    assert meta.attributes["working_levels"] == [1, 3, 4]
    assert meta.attributes["kaynak"] == "medas"
    assert meta.attributes["default_frequency"] == "annual"
    dimensions = {dimension.code: dimension for dimension in meta.dimensions}
    assert [code.code for code in dimensions["LEVEL"].codes] == ["1", "3", "4"]
    ref_codes = {code.code: code for code in dimensions["REF_AREA"].codes}
    assert "TR1" in ref_codes and "TR1".startswith("TR")
    assert "39" in ref_codes and "1486" in ref_codes
    assert ref_codes["39"].label == "KIRKLARELİ"
    assert ref_codes["39"].parent_code == "TR21"
    assert ref_codes["1486"].parent_code == "TR62"
    assert dimensions["TIME_PERIOD"].role == "time"
    assert meta.coverage_start == date(2007, 1, 1)
    assert meta.coverage_end == date(2025, 1, 1)
    assert meta.obs_count > 0


def test_dataset_meta_ilgostergeleri_is_level_three_only() -> None:
    connector, _ = _connector()
    meta = connector.dataset_meta(f"CIP_{ENR}")
    dimensions = {dimension.code: dimension for dimension in meta.dimensions}
    assert [code.code for code in dimensions["LEVEL"].codes] == ["3"]
    assert len(dimensions["REF_AREA"].codes) == 81
    assert meta.attributes["kaynak"] == "ilGostergeleri"
    assert meta.attributes["unit_hint"] == "kWh"
    assert meta.source_incomplete is False


def test_dataset_meta_flags_indicator_with_no_data() -> None:
    connector, _ = _connector()
    meta = connector.dataset_meta("CIP_ses123")
    assert meta.dimensions == []
    assert meta.source_incomplete is True
    assert "error body" in (meta.source_incomplete_note or "")
    failures = connector.failures()
    assert [failure.gosterge_no for failure in failures] == ["ses123"]
    assert failures[0].kind == NOT_FOUND


def test_fetch_series_level3_returns_all_years() -> None:
    connector, _ = _connector()
    result = connector.fetch_series(f"CIP_{ADNKS}", {"LEVEL": "3", "REF_AREA": "39"})
    assert result.channel == "cip"
    assert len(result.points) == 19
    assert result.points[0][0] == date(2007, 1, 1)
    assert result.points[-1][0] == date(2025, 1, 1)
    assert result.points[-1][1] == Decimal("379595")
    assert result.raw_object_keys


def test_fetch_series_level1_returns_twelve_regions() -> None:
    connector, _ = _connector()
    result = connector.fetch_series(f"CIP_{ADNKS}", {"LEVEL": "1", "REF_AREA": "TR1"})
    assert len(result.points) == 19


def test_fetch_series_keeps_missing_values_as_none() -> None:
    connector, _ = _connector()
    raw = fixture_json(BTUR_L4)
    unit = next(row for row in raw["veriler"] if "" in row["veri"])
    expected_blanks = sum(1 for value in unit["veri"] if value == "")
    result = connector.fetch_series(
        f"CIP_{BTUR}", {"LEVEL": "4", "REF_AREA": str(unit["duzeyKodu"])}
    )
    assert sum(1 for _, value in result.points if value is None) == expected_blanks
    assert any(value is not None for _, value in result.points)


def test_fetch_series_monthly_skips_the_year_zero_marker() -> None:
    connector, _ = _connector()
    result = connector.fetch_series(f"CIP_{ULS}", {"LEVEL": "3", "REF_AREA": "39"})
    assert result.points
    assert all(period.month != 0 for period, _ in result.points)
    assert all(period.day == 1 for period, _ in result.points)
    raw = fixture_json(ULS_L3)
    valid = [label for label in raw["tarihler"] if not label.endswith("/0")]
    assert len(result.points) == len(valid)


def test_dataset_meta_monthly_indicator_is_catalogued() -> None:
    connector, _ = _connector()
    meta = connector.dataset_meta(f"CIP_{ULS}")
    assert meta.attributes["default_frequency"] == "monthly"
    assert "3" in [code.code for code in meta.dimensions[0].codes]
    assert meta.source_incomplete is False


def test_fetch_series_unknown_unit_raises_empty() -> None:
    connector, _ = _connector()
    with pytest.raises(ConnectorError) as excinfo:
        connector.fetch_series(f"CIP_{ADNKS}", {"LEVEL": "3", "REF_AREA": "99999"})
    assert excinfo.value.kind == EMPTY


def test_fetch_series_requires_level_and_area() -> None:
    connector, _ = _connector()
    with pytest.raises(ConnectorError) as excinfo:
        connector.fetch_series(f"CIP_{ADNKS}", {"LEVEL": "3"})
    assert excinfo.value.kind == FORMAT_CHANGED


def test_fetch_series_unknown_indicator_is_not_found() -> None:
    connector, _ = _connector()
    with pytest.raises(ConnectorError) as excinfo:
        connector.fetch_series("CIP_DOES-NOT-EXIST", {"LEVEL": "3", "REF_AREA": "39"})
    assert excinfo.value.kind == NOT_FOUND


def test_build_series_definition_uses_default_frequency() -> None:
    from app.data.models import Dataset, DatasetDimension, DimensionCode

    dataset = Dataset(institution_id=1, external_code=f"CIP_{ADNKS}", name="Toplam Nüfus")
    dataset.attributes = {"default_frequency": "annual"}
    dataset.dimensions = [
        DatasetDimension(
            code="LEVEL",
            label="Düzey",
            position=0,
            role="other",
            codes=[DimensionCode(code="3", label="İl (İBBS-3)")],
        ),
        DatasetDimension(
            code="REF_AREA",
            label="Yer",
            position=1,
            role="geo",
            codes=[DimensionCode(code="39", label="KIRKLARELİ")],
        ),
        DatasetDimension(code="TIME_PERIOD", label="Dönem", position=2, role="time", codes=[]),
    ]
    definition = build_series_definition(dataset, {"LEVEL": "3", "REF_AREA": "39"})
    assert definition.external_code == f"CIP_{ADNKS}:3.39"
    assert definition.frequency == "annual"


# --- CLI --------------------------------------------------------------------


class _StubCipConnector:
    institution_code = "tuik"
    institution_name = "Türkiye İstatistik Kurumu"

    class _Client:
        max_concurrency = 4

    def __init__(self, entries, results, failures) -> None:
        self.client = self._Client()
        self._entries = entries
        self._results = results
        self._failures = failures

    def entries(self):
        return list(self._entries)

    def dataset_meta(self, entry):
        result = self._results[entry.gosterge_no]
        if isinstance(result, Exception):
            raise result
        return result

    def failures(self):
        return list(self._failures)


def _healthy_meta(code: str):
    from app.connectors.base import DatasetMeta, DimensionCodeMeta, DimensionMeta

    return DatasetMeta(
        external_code=f"CIP_{code}",
        name=code,
        attributes={"working_levels": [3]},
        dimensions=[
            DimensionMeta(
                code="LEVEL",
                label="Düzey",
                position=0,
                role="other",
                codes=[DimensionCodeMeta(code="3", label="İl (İBBS-3)")],
            ),
            DimensionMeta(
                code="REF_AREA",
                label="Yer",
                position=1,
                role="geo",
                codes=[DimensionCodeMeta(code="39", label="KIRKLARELİ")],
            ),
            DimensionMeta(code="TIME_PERIOD", label="Dönem", position=2, role="time"),
        ],
    )


def test_cip_catalog_parser_flags() -> None:
    from app.connectors.tuik.__main__ import build_parser

    args = build_parser().parse_args(
        ["cip-catalog", "--dry-run", "--limit", "5", "--kaynak", "medas"]
    )
    assert args.command == "cip-catalog"
    assert args.dry_run is True
    assert args.limit == 5
    assert args.kaynak == "medas"


def test_is_cip_command_routes_cip_datasets_only() -> None:
    import argparse

    from app.connectors.tuik.__main__ import _is_cip_command

    assert _is_cip_command(argparse.Namespace(command="cip-catalog", dataset=None, series=None))
    assert _is_cip_command(argparse.Namespace(command="fetch", dataset="CIP_X", series=None))
    assert _is_cip_command(argparse.Namespace(command="fetch", dataset=None, series="CIP_X:3.39"))
    assert not _is_cip_command(argparse.Namespace(command="fetch", dataset="DF_X", series=None))
    assert not _is_cip_command(argparse.Namespace(command="catalog", dataset=None, series=None))


def test_cmd_cip_catalog_dry_run_reports_counts(capsys) -> None:
    import argparse

    from app.connectors.tuik.__main__ import _cmd_cip_catalog
    from app.connectors.tuik.cip import CipCatalogFailure, CipEntry

    good = CipEntry("GOOD", "Good", None, "medas", "yillik", "Nüfus", (3,))
    bad = CipEntry("BAD", "Bad", None, "medas", "yillik", "Nüfus", (3,))
    bad_meta = _healthy_meta("BAD")
    bad_meta.dimensions.clear()  # no working level -> failed, not counted as ok
    connector = _StubCipConnector(
        [good, bad],
        {"GOOD": _healthy_meta("GOOD"), "BAD": bad_meta},
        [CipCatalogFailure("BAD", NOT_FOUND, "CİP error body")],
    )
    args = argparse.Namespace(dry_run=True, limit=None, kaynak=None)

    assert _cmd_cip_catalog(None, connector, args) == 0

    out = capsys.readouterr().out
    assert "cip-catalog (dry-run): 1 datasets ok, 1 failed, 2 processed" in out
    assert "levels/dataset: 3=1" in out
    assert "failed by kind: not_found (1): CIP_BAD" in out
