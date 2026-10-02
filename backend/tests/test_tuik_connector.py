"""Unit tests for the databrowser2 client and TÜİK connector (no network)."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

import app.connectors.tuik.connector as connector_module
from app.connectors.base import (
    EMPTY,
    FORMAT_CHANGED,
    NOT_FOUND,
    THROTTLED,
    ConnectorError,
    InMemoryObjectStore,
)
from app.connectors.tuik.client import Databrowser2Client
from app.connectors.tuik.connector import TuikConnector, ref_area_criteria
from app.connectors.tuik.parsers import DataflowInfo

FIXTURES = Path(__file__).parent / "fixtures" / "tuik"

CATALOG = "databrowser2-catalog.raw.json"
TUFE_DATA = "tufe-tt10.databrowser2-data.raw.json"
TUFE_CSV = "tufe-tt10.databrowser2-download.raw.csv"
GSYH_DATA = "uh-bh-gsyh-cari.databrowser2-data.raw.json"
GSYH_CSV = "uh-bh-gsyh-cari.databrowser2-download.raw.csv"
QS_DATA = "isgucu-ceyrek-tamamlayici.databrowser2-data.raw.json"
QS_CSV = "isgucu-ceyrek-tamamlayici.databrowser2-download.raw.csv"
FIIL_DATA = "isgucu-fiil-calisma-sure.databrowser2-data.raw.json"
FIIL_CSV = "isgucu-fiil-calisma-sure.databrowser2-download.raw.csv"
EMPTY_JSON = "ibbs3-09-13.empty-refarea.raw.json"
CODELIST_JSON = "ibbs3-09-13.refarea-codelist.raw.json"
TR100_JSON = "ibbs3-09-13.tr100.databrowser2-data.raw.json"
THROTTLE_HTML = "databrowser2-throttle.raw.html"
DNF_JSON = "tufe-tt10.dataflow-not-found.raw.json"
MERKEZI_PREFIX = "merkezi-yonetim-butcesi"
SUT_PREFIX = "sut-urunler-yillik-v2-2023"

TUFE_ID = "TR,DF_TUFE_SDMX_TT10,1.0"
GSYH_ID = "TR,UH_BH_GSYH_CARI,1.0"
QS_ID = "TR,DF_ISGUCU_CEYREK_TAMAMLAYICI_GOSTERGE_C,1.0"
IBBS_ID = "TR,DF_BR_FAALIYET_BUYUKLUK_GIRISIM_IBBS3_09_13_V1,1.0"

TUFE_META = DataflowInfo(
    dataflow_id="DF_TUFE_SDMX_TT10",
    version="1.0",
    agency="TR",
    title="Index numbers and rate of changes in the consumer price index",
    description=None,
    source_category="Price Statistics / Consumer Price Index (CPI)",
)
GSYH_META = DataflowInfo(
    dataflow_id="UH_BH_GSYH_CARI",
    version="1.0",
    agency="TR",
    title="Regional gross domestic product (at current prices)",
    description=None,
    source_category=None,
)
QS_META = DataflowInfo(
    dataflow_id="DF_ISGUCU_CEYREK_TAMAMLAYICI_GOSTERGE_C",
    version="1.0",
    agency="TR",
    title="Supplementary indicators for labour force",
    description=None,
    source_category=None,
)
IBBS_META = DataflowInfo(
    dataflow_id="DF_BR_FAALIYET_BUYUKLUK_GIRISIM_IBBS3_09_13_V1",
    version="1.0",
    agency="TR",
    title="Number of Enterprises by Economic Activity and Size Group (NUTS 3 Level)",
    description=None,
    source_category=None,
)
MERKEZI_META = DataflowInfo(
    dataflow_id="DF_MERKEZI_YONETIM_BUTCESI",
    version="1.0",
    agency="TR",
    title="National public funding to transnationally coordinated R&D",
    description=None,
    source_category=None,
)
SUT_META = DataflowInfo(
    dataflow_id="DF_SUT_URUNLER_YILLK_V2_2023",
    version="1.0",
    agency="TR",
    title="Milk and Dairy Products - Product Details (2023 and earlier)",
    description=None,
    source_category=None,
)

TUFE_CODES = {
    "REF_AREA": "TR",
    "FREQ": "M",
    "SINIFLAMA_DUZEYI": "TUFE",
    "DEGISIM": "1",
    "BASE_PER": "2025",
    "COICOP_2018": "0",
    "INDICATOR": "F_TFE",
}
GSYH_CODES = {
    "REF_AREA": "TR",
    "FREQ": "A",
    "KIRILIM_SEVIYE": "A10",
    "FAALIYET_KOD": "B1GQ",
    "UNIT_MEASURE": "TTRY",
    "BAZ_YILI": "2009",
}
QS_CODES = {
    "REF_AREA": "TR",
    "INDICATOR": "ISGI_ITG",
    "TEMEL_YAS": "Y_GE15",
    "SEASONAL_ADJUST": "N",
    "ISGUCU_GOSTERGE": "10",
    "FREQ": "Q",
    "UNIT_MEASURE": "RO",
    "SEX": "_T",
}
FIIL_CODES = {
    "REF_AREA": "TR",
    "INDICATOR": "ISGI_ISFCS",
    "TEMEL_YAS": "Y_GE15",
    "SEASONAL_ADJUST": "N",
    "FREQ": "Q",
    "SEX": "_T",
    "FIIL_CALISMA_SURE": "1",
}


def catalog_fixture_handler(prefix: str):
    """MockTransport handler serving one captured dataflow's catalog fixtures."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = path_of(request)
        if path.endswith("/structure"):
            return json_response(f"{prefix}.structure.raw.json")
        if "/PartialCodelists/" in path:
            dimension = path.rsplit("/", 1)[1]
            return json_response(f"{prefix}.partial-{dimension}.raw.json")
        if path.endswith("/download/json"):
            return json_response(f"{prefix}.download-json.raw.json")
        if path.endswith("/data"):
            return json_response(f"{prefix}.default-data.raw.json")
        raise AssertionError(f"unexpected path {path}")

    return handler


# A tiny synthetic dataflow: A (4 codes) x B (2 codes) x 2 annual periods.
SYNTHETIC_A = ["a1", "a2", "a3", "a4"]
SYNTHETIC_B = ["b1", "b2"]


def synthetic_sdmx_payload(a_codes: list[str], b_codes: list[str]) -> dict[str, Any]:
    series = {
        f"{i}:{j}": {"observations": {"0": ["1"], "1": ["2"]}}
        for i in range(len(a_codes))
        for j in range(len(b_codes))
    }
    return {
        "data": {
            "dataSets": [{"series": series}],
            "structure": {
                "dimensions": {
                    "series": [
                        {
                            "id": "A",
                            "keyPosition": 0,
                            "values": [{"id": code, "name": code} for code in a_codes],
                        },
                        {
                            "id": "B",
                            "keyPosition": 1,
                            "values": [{"id": code, "name": code} for code in b_codes],
                        },
                    ],
                    "observation": [
                        {
                            "id": "TIME_PERIOD",
                            "keyPosition": 2,
                            "values": [
                                {"id": "2020", "start": "2020-01-01T00:00:00"},
                                {"id": "2021", "start": "2021-01-01T00:00:00"},
                            ],
                        }
                    ],
                }
            },
        }
    }


def _selected(request: httpx.Request) -> tuple[list[str], list[str]]:
    if not request.content:
        return list(SYNTHETIC_A), list(SYNTHETIC_B)
    body = request_body(request)
    if not isinstance(body, list):
        return list(SYNTHETIC_A), list(SYNTHETIC_B)
    by_dim = {c["id"]: c["filterValues"] for c in body if isinstance(c, dict)}
    return by_dim.get("A", list(SYNTHETIC_A)), by_dim.get("B", list(SYNTHETIC_B))


def synthetic_handler(*, truncate: bool = False):
    """Handler for the synthetic dataflow; ``truncate`` drops one series always."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = path_of(request)
        a_codes, b_codes = _selected(request)
        if path.endswith("/structure"):
            return httpx.Response(
                200,
                json={
                    "timeDimension": "TIME_PERIOD",
                    "territorialDimension": "REF_AREA",
                    "criteria": [{"id": "A"}, {"id": "B"}, {"id": "TIME_PERIOD"}],
                    "template": {"hiddenDimensions": []},
                },
                headers={"content-type": "application/json"},
            )
        if path.endswith("/data"):
            return httpx.Response(
                200,
                json={"id": ["A", "B", "TIME_PERIOD"], "size": [1, 1, 1], "value": {"0": "1"}},
                headers={"content-type": "application/json"},
            )
        if "/PartialCodelists/" in path:
            dimension = path.rsplit("/", 1)[1]
            if dimension == "A":
                values = SYNTHETIC_A
            elif dimension == "B":
                values = SYNTHETIC_B
            else:
                values = []
            body = request_body(request)
            if isinstance(body, list) and body:
                by_dim = {c["id"]: c["filterValues"] for c in body if isinstance(c, dict)}
                expected = len(by_dim.get("A", SYNTHETIC_A)) * len(by_dim.get("B", SYNTHETIC_B)) * 2
            else:
                expected = len(SYNTHETIC_A) * len(SYNTHETIC_B) * 2
            return httpx.Response(
                200,
                json={
                    "criteria": [
                        {
                            "id": dimension,
                            "values": [{"id": code, "isSelectable": True} for code in values],
                        }
                    ],
                    "obsCount": expected,
                },
                headers={"content-type": "application/json"},
            )
        if path.endswith("/download/json"):
            payload = synthetic_sdmx_payload(a_codes, b_codes)
            if truncate:
                series = payload["data"]["dataSets"][0]["series"]
                if series:
                    series.pop(next(iter(series)))
            return httpx.Response(200, json=payload, headers={"content-type": "application/json"})
        raise AssertionError(f"unexpected path {path}")

    return handler


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def json_response(name: str) -> httpx.Response:
    return httpx.Response(
        200, content=fixture_bytes(name), headers={"content-type": "application/json"}
    )


def csv_response(name: str) -> httpx.Response:
    return httpx.Response(
        200, content=fixture_bytes(name), headers={"content-type": "application/vnd.sdmx.data+csv"}
    )


def build_connector(
    handler: Callable[[httpx.Request], httpx.Response], store: InMemoryObjectStore
) -> TuikConnector:
    def routed(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/catalog"):
            return json_response(CATALOG)
        return handler(request)

    http_client = httpx.Client(transport=httpx.MockTransport(routed))
    client = Databrowser2Client(http_client=http_client, store=store, sleeper=lambda _: None)
    return TuikConnector(client=client)


def request_body(request: httpx.Request) -> Any:
    return json.loads(request.content)


def path_of(request: httpx.Request) -> str:
    return request.url.path


def test_catalog_lists_live_versions() -> None:
    store = InMemoryObjectStore()

    def handler(request: httpx.Request) -> httpx.Response:
        assert path_of(request).endswith("/catalog")
        return json_response(CATALOG)

    connector = build_connector(handler, store)
    dataflows = connector.dataflows()
    assert len(dataflows) == 467
    info = connector.resolve("DF_TUFE_SDMX_TT10")
    assert info.version == "1.0"
    assert info.dataset_identifier == TUFE_ID
    assert connector.catalog_raw_object_key is not None
    assert connector.catalog_raw_object_key in store.objects


def test_catalog_dataflow_requests_every_selectable_code() -> None:
    store = InMemoryObjectStore()
    seen_download: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if path_of(request).endswith("/download/json"):
            seen_download.append(request_body(request))
            return json_response(f"{MERKEZI_PREFIX}.download-json.raw.json")
        return catalog_fixture_handler(MERKEZI_PREFIX)(request)

    connector = build_connector(handler, store)
    result = connector.catalog_dataflow(MERKEZI_META)
    assert result.obs_expected == 12
    assert result.obs_received == 12
    assert result.series_found == 4
    assert result.complete
    by_dim = {c["id"]: c["filterValues"] for c in seen_download[0]}
    assert sorted(by_dim["ULUSLARASI_ARGE_PROGRAM"]) == ["1", "2", "3", "_T"]
    assert by_dim["REF_AREA"] == ["TR"]
    assert "TIME_PERIOD" not in by_dim


def test_catalog_dataflow_expands_a_defaulted_non_ref_area_dimension() -> None:
    store = InMemoryObjectStore()
    connector = build_connector(catalog_fixture_handler(SUT_PREFIX), store)
    result = connector.catalog_dataflow(SUT_META)
    assert result.obs_expected == 385
    assert result.obs_received == 385
    assert result.series_found == 35
    assert result.complete


def _synthetic_meta() -> DataflowInfo:
    return DataflowInfo(
        dataflow_id="DF_SYNTH",
        version="1.0",
        agency="TR",
        title="Synthetic",
        description=None,
        source_category=None,
    )


def test_catalog_dataflow_partitions_and_merges(monkeypatch) -> None:
    monkeypatch.setattr(connector_module, "MAX_CHUNK_OBSERVATIONS", 4)
    store = InMemoryObjectStore()
    connector = build_connector(synthetic_handler(), store)
    result = connector.catalog_dataflow(_synthetic_meta())
    assert result.obs_expected == 16
    assert result.obs_received == 16
    assert result.series_found == 8
    assert result.complete
    assert {meta.external_code for meta in result.metas} == {
        f"DF_SYNTH:{a}.{b}" for a in SYNTHETIC_A for b in SYNTHETIC_B
    }


def test_catalog_dataflow_reports_incomplete_after_chunking(monkeypatch) -> None:
    monkeypatch.setattr(connector_module, "MAX_CHUNK_OBSERVATIONS", 4)
    store = InMemoryObjectStore()
    connector = build_connector(synthetic_handler(truncate=True), store)
    result = connector.catalog_dataflow(_synthetic_meta())
    assert result.obs_expected == 16
    assert result.obs_received < result.obs_expected
    assert not result.complete


def test_catalog_dataflow_tolerates_empty_dataset() -> None:
    store = InMemoryObjectStore()

    def handler(request: httpx.Request) -> httpx.Response:
        path = path_of(request)
        json_headers = {"content-type": "application/json"}
        if path.endswith("/structure"):
            return httpx.Response(
                200,
                json={
                    "timeDimension": "TIME_PERIOD",
                    "criteria": [{"id": "A"}],
                    "template": {"hiddenDimensions": []},
                },
                headers=json_headers,
            )
        if "/PartialCodelists/" in path:
            return httpx.Response(
                200,
                json={
                    "criteria": [{"id": "A", "values": [{"id": "a1", "isSelectable": True}]}],
                    "obsCount": 0,
                },
                headers=json_headers,
            )
        if path.endswith("/download/json"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "dataSets": [],
                        "structure": {
                            "dimensions": {
                                "series": [{"id": "A", "keyPosition": 0, "values": [{"id": "a1"}]}],
                                "observation": [],
                            }
                        },
                    }
                },
                headers=json_headers,
            )
        if path.endswith("/data"):
            return httpx.Response(200, json={"id": ["A"], "value": {}}, headers=json_headers)
        raise AssertionError(path)

    connector = build_connector(handler, store)
    result = connector.catalog_dataflow(_synthetic_meta())
    assert result.obs_received == 0
    assert result.series_found == 0


def test_fetch_series_csv_monthly_and_start_filter() -> None:
    store = InMemoryObjectStore()

    def handler(request: httpx.Request) -> httpx.Response:
        path = path_of(request)
        if path.endswith("/download/csv"):
            return csv_response(TUFE_CSV)
        assert path.endswith("/data")
        return json_response(TUFE_DATA)

    connector = build_connector(handler, store)
    result = connector.fetch_series("DF_TUFE_SDMX_TT10", TUFE_CODES)
    assert result.channel == "databrowser2"
    assert len(result.points) == 260
    assert result.points[0] == (date(2005, 1, 1), Decimal("3.596523"))
    assert result.raw_object_keys
    assert result.raw_object_keys[0] in store.objects

    recent = connector.fetch_series("DF_TUFE_SDMX_TT10", TUFE_CODES, start=date(2024, 1, 1))
    assert len(recent.points) == 32
    assert recent.points[0][0] == date(2024, 1, 1)


def test_fetch_series_csv_annual_gdp() -> None:
    store = InMemoryObjectStore()

    def handler(request: httpx.Request) -> httpx.Response:
        if path_of(request).endswith("/download/csv"):
            return csv_response(GSYH_CSV)
        return json_response(GSYH_DATA)

    connector = build_connector(handler, store)
    result = connector.fetch_series("UH_BH_GSYH_CARI", GSYH_CODES)
    assert len(result.points) == 25
    assert result.points[-1] == (date(2024, 1, 1), Decimal("44587225440.1984"))


def test_fetch_series_csv_quarterly() -> None:
    store = InMemoryObjectStore()

    def handler(request: httpx.Request) -> httpx.Response:
        if path_of(request).endswith("/download/csv"):
            return csv_response(QS_CSV)
        return json_response(QS_DATA)

    connector = build_connector(handler, store)
    result = connector.fetch_series("DF_ISGUCU_CEYREK_TAMAMLAYICI_GOSTERGE_C", QS_CODES)
    assert len(result.points) == 50
    assert result.points[0] == (date(2014, 1, 1), Decimal("16.3"))
    assert result.points[-1] == (date(2026, 4, 1), Decimal("19.5"))


def test_fetch_series_falls_back_to_jsonstat_when_csv_breaks() -> None:
    store = InMemoryObjectStore()

    def handler(request: httpx.Request) -> httpx.Response:
        path = path_of(request)
        if path.endswith("/download/csv"):
            return httpx.Response(
                200,
                content=b"header,without,observation,columns\n1,2,3,4\n",
                headers={"content-type": "text/csv"},
            )
        assert path.endswith("/data")
        return json_response(TUFE_DATA)

    connector = build_connector(handler, store)
    result = connector.fetch_series("DF_TUFE_SDMX_TT10", TUFE_CODES)
    assert result.points[0] == (date(2005, 1, 1), Decimal("3.596523"))
    assert len(result.raw_object_keys) == 2
    assert result.raw_object_keys[0] in store.objects


def test_fetch_series_both_formats_fail_is_format_changed() -> None:
    store = InMemoryObjectStore()

    def handler(request: httpx.Request) -> httpx.Response:
        path = path_of(request)
        if path.endswith("/download/csv"):
            return httpx.Response(200, content=b"nope", headers={"content-type": "text/csv"})
        if path.endswith("/data") and len(request_body(request)) > 1:
            return httpx.Response(
                200, content=b"not json at all", headers={"content-type": "application/json"}
            )
        return json_response(TUFE_DATA)

    connector = build_connector(handler, store)
    with pytest.raises(ConnectorError) as excinfo:
        connector.fetch_series("DF_TUFE_SDMX_TT10", TUFE_CODES)
    assert excinfo.value.kind == FORMAT_CHANGED


def test_fetch_series_with_unknown_series_key_is_empty() -> None:
    store = InMemoryObjectStore()

    def handler(request: httpx.Request) -> httpx.Response:
        if path_of(request).endswith("/download/csv"):
            return csv_response(TUFE_CSV)
        return json_response(TUFE_DATA)

    connector = build_connector(handler, store)
    with pytest.raises(ConnectorError) as excinfo:
        connector.fetch_series("DF_TUFE_SDMX_TT10", {**TUFE_CODES, "DEGISIM": "9"})
    assert excinfo.value.kind == EMPTY


def test_fetch_series_rejects_malformed_external_code() -> None:
    store = InMemoryObjectStore()
    connector = build_connector(lambda request: json_response(CATALOG), store)
    with pytest.raises(ConnectorError) as excinfo:
        connector.fetch_series("DF_TUFE_SDMX_TT10", {})
    assert excinfo.value.kind == FORMAT_CHANGED


def test_dataset_meta_maps_dimensions_codes_and_roles() -> None:
    store = InMemoryObjectStore()

    def handler(request: httpx.Request) -> httpx.Response:
        path = path_of(request)
        json_headers = {"content-type": "application/json"}
        if path.endswith("/structure"):
            return json_response(f"{MERKEZI_PREFIX}.structure.raw.json")
        if "/PartialCodelists/" in path:
            dimension = path.rsplit("/", 1)[1]
            if dimension == "TIME_PERIOD":
                return httpx.Response(
                    200,
                    json={
                        "criteria": [
                            {
                                "id": "TIME_PERIOD",
                                "label": "Time",
                                "values": [
                                    {"id": "2024-01-01", "name": "Start Time period"},
                                    {"id": "2026-01-01", "name": "End Time period"},
                                ],
                            }
                        ],
                        "obsCount": 12,
                    },
                    headers=json_headers,
                )
            return json_response(f"{MERKEZI_PREFIX}.partial-{dimension}.raw.json")
        raise AssertionError(path)

    connector = build_connector(handler, store)
    meta = connector.dataset_meta(MERKEZI_META)
    assert meta.external_code == "DF_MERKEZI_YONETIM_BUTCESI"
    assert meta.obs_count == 12
    assert meta.coverage_start == date(2024, 1, 1)
    assert meta.coverage_end == date(2026, 1, 1)
    by_code = {dimension.code: dimension for dimension in meta.dimensions}
    assert by_code["FREQ"].role == "frequency"
    assert by_code["REF_AREA"].role == "geo"
    assert by_code["TIME_PERIOD"].role == "time"
    assert by_code["ULUSLARASI_ARGE_PROGRAM"].codes[0].code == "_T"
    assert by_code["ULUSLARASI_ARGE_PROGRAM"].codes[0].label == "Total"
    assert by_code["REF_AREA"].codes[0].parent_code is None


def test_dataset_meta_uses_source_parent_ids() -> None:
    store = InMemoryObjectStore()
    json_headers = {"content-type": "application/json"}

    def handler(request: httpx.Request) -> httpx.Response:
        path = path_of(request)
        if path.endswith("/structure"):
            return httpx.Response(
                200,
                json={
                    "timeDimension": "TIME_PERIOD",
                    "criteria": [
                        {
                            "id": "REF_AREA",
                            "label": "Reference area",
                            "extra": {"DataStructureRef": "TR+CL_IBBS+1.2"},
                        },
                        {"id": "TIME_PERIOD"},
                    ],
                },
                headers=json_headers,
            )
        if "/PartialCodelists/" in path:
            dimension = path.rsplit("/", 1)[1]
            if dimension == "TIME_PERIOD":
                return httpx.Response(
                    200,
                    json={"criteria": [{"id": "TIME_PERIOD", "label": "Time", "values": []}]},
                    headers=json_headers,
                )
            return json_response(CODELIST_JSON)
        raise AssertionError(path)

    connector = build_connector(handler, store)
    meta = connector.dataset_meta(IBBS_META)
    ref_area = next(dimension for dimension in meta.dimensions if dimension.code == "REF_AREA")
    assert ref_area.role == "geo"
    assert ref_area.attributes["hierarchy_source"] == "source"
    by_code = {code.code: code for code in ref_area.codes}
    assert by_code["TR100"].parent_code == "TR10"
    assert by_code["TR100"].is_default is False


def test_dataset_meta_tolerates_empty_codelists() -> None:
    store = InMemoryObjectStore()
    json_headers = {"content-type": "application/json"}

    def handler(request: httpx.Request) -> httpx.Response:
        path = path_of(request)
        if path.endswith("/structure"):
            return httpx.Response(
                200,
                json={
                    "timeDimension": "TIME_PERIOD",
                    "criteria": [{"id": "FREQ"}, {"id": "INDICATOR"}, {"id": "TIME_PERIOD"}],
                },
                headers=json_headers,
            )
        if "/PartialCodelists/" in path:
            dimension = path.rsplit("/", 1)[1]
            if dimension == "TIME_PERIOD":
                return httpx.Response(
                    200,
                    json={
                        "criteria": [
                            {
                                "id": "TIME_PERIOD",
                                "label": "Time",
                                "values": [
                                    {"id": "2017-01-01", "name": "Start Time period"},
                                    {"id": "2025-12-31", "name": "End Time period"},
                                ],
                            }
                        ]
                    },
                    headers=json_headers,
                )
            return httpx.Response(200, json={"criteria": []}, headers=json_headers)
        raise AssertionError(path)

    connector = build_connector(handler, store)
    meta = connector.dataset_meta(_synthetic_meta())
    by_code = {dimension.code: dimension for dimension in meta.dimensions}
    assert by_code["FREQ"].codes == []
    assert by_code["INDICATOR"].codes == []
    assert meta.source_incomplete is True
    assert "FREQ" in (meta.source_incomplete_note or "")
    assert "INDICATOR" in (meta.source_incomplete_note or "")
    assert meta.coverage_start == date(2017, 1, 1)
    assert meta.coverage_end == date(2025, 12, 31)


def test_fetch_series_retries_unfiltered_when_filtered_request_500s() -> None:
    store = InMemoryObjectStore()

    def handler(request: httpx.Request) -> httpx.Response:
        path = path_of(request)
        if path.endswith("/data"):
            return json_response(TUFE_DATA)
        if path.endswith("/download/csv"):
            body = request_body(request)
            if isinstance(body, list) and body:
                return httpx.Response(
                    500,
                    content=b'{"errorCode":"INTERNAL_ERROR_SERVER","message":""}',
                    headers={"content-type": "application/json"},
                )
            return csv_response(TUFE_CSV)
        raise AssertionError(path)

    connector = build_connector(handler, store)
    result = connector.fetch_series("DF_TUFE_SDMX_TT10", TUFE_CODES)
    assert len(result.points) == 260
    assert result.points[0] == (date(2005, 1, 1), Decimal("3.596523"))


def test_partial_content_206_is_accepted_like_200() -> None:
    store = InMemoryObjectStore()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            206, content=fixture_bytes(TUFE_DATA), headers={"content-type": "application/json"}
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = Databrowser2Client(http_client=http_client, store=store, sleeper=lambda _: None)
    response = client.dataset_data(TUFE_ID, ref_area_criteria("TR"))
    assert response.status_code == 206
    assert response.json()["id"]
    assert response.raw_object_key in store.objects


def test_dataflow_not_found_maps_to_not_found_with_raw_key() -> None:
    store = InMemoryObjectStore()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500, content=fixture_bytes(DNF_JSON), headers={"content-type": "application/json"}
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = Databrowser2Client(http_client=http_client, store=store, sleeper=lambda _: None)
    with pytest.raises(ConnectorError) as excinfo:
        client.dataset_data(TUFE_ID, ref_area_criteria("TR"))
    assert excinfo.value.kind == NOT_FOUND
    assert excinfo.value.raw_object_key is not None
    assert excinfo.value.raw_object_key in store.objects


def test_throttle_page_is_retried_and_marks_client() -> None:
    store = InMemoryObjectStore()
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(
                200, content=fixture_bytes(THROTTLE_HTML), headers={"content-type": "text/html"}
            )
        return json_response(TUFE_DATA)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = Databrowser2Client(http_client=http_client, store=store, sleeper=lambda _: None)
    response = client.dataset_data(TUFE_ID, ref_area_criteria("TR"))
    assert response.status_code == 200
    assert client.throttled is True
    assert client.max_concurrency == 1
    assert any("throttle" in key for key in store.objects)


def test_throttle_page_exhausts_retries_as_throttled() -> None:
    store = InMemoryObjectStore()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=fixture_bytes(THROTTLE_HTML), headers={"content-type": "text/html"}
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = Databrowser2Client(http_client=http_client, store=store, sleeper=lambda _: None)
    with pytest.raises(ConnectorError) as excinfo:
        client.dataset_data(TUFE_ID, ref_area_criteria("TR"))
    assert excinfo.value.kind == THROTTLED
    assert excinfo.value.raw_object_key is not None


FIIL_META = DataflowInfo(
    dataflow_id="DF_ISGUCU_CEYREK_FIIL_CALISMA_SURE_C",
    version="1.0",
    agency="TR",
    title="Actual working hours of employed",
    description=None,
    source_category=None,
)
FIIL_CODE = "DF_ISGUCU_CEYREK_FIIL_CALISMA_SURE_C:TR.ISGI_ISFCS.Y_GE15.N.Q._T.1"


def test_catalog_dataflow_rejects_a_missing_structure() -> None:
    store = InMemoryObjectStore()

    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(FIIL_DATA)

    connector = build_connector(handler, store)
    with pytest.raises(ConnectorError) as excinfo:
        connector.catalog_dataflow(FIIL_META)
    assert excinfo.value.kind == connector_module.SOURCE_ERROR


def test_fetch_series_when_default_view_has_no_time_dimension() -> None:
    store = InMemoryObjectStore()

    def handler(request: httpx.Request) -> httpx.Response:
        if path_of(request).endswith("/download/csv"):
            return csv_response(FIIL_CSV)
        return json_response(FIIL_DATA)

    connector = build_connector(handler, store)
    result = connector.fetch_series("DF_ISGUCU_CEYREK_FIIL_CALISMA_SURE_C", FIIL_CODES)
    assert len(result.points) == 50
    assert result.points[0] == (date(2014, 1, 1), Decimal("24802"))
    assert result.points[-1] == (date(2026, 4, 1), Decimal("32656"))


def test_fetch_series_fallback_without_time_dimension_is_format_changed() -> None:
    store = InMemoryObjectStore()

    def handler(request: httpx.Request) -> httpx.Response:
        path = path_of(request)
        if path.endswith("/download/csv"):
            return httpx.Response(
                200, content=b"bad,header\n1,2\n", headers={"content-type": "text/csv"}
            )
        return json_response(FIIL_DATA)

    connector = build_connector(handler, store)
    with pytest.raises(ConnectorError) as excinfo:
        connector.fetch_series("DF_ISGUCU_CEYREK_FIIL_CALISMA_SURE_C", FIIL_CODES)
    assert excinfo.value.kind == FORMAT_CHANGED


def test_catalog_regional_series_is_fetchable() -> None:
    store = InMemoryObjectStore()
    ref_areas = ["TR", "TR100"]
    csv_bodies: list[Any] = []

    def sdmx_payload(sel_ref: list[str], sel_ind: list[str]) -> dict[str, Any]:
        series = {
            f"{i}:{j}": {"observations": {"0": ["1"], "1": ["2"]}}
            for i in range(len(sel_ref))
            for j in range(len(sel_ind))
        }
        return {
            "data": {
                "dataSets": [{"series": series}],
                "structure": {
                    "dimensions": {
                        "series": [
                            {
                                "id": "REF_AREA",
                                "keyPosition": 0,
                                "values": [{"id": code, "name": code} for code in sel_ref],
                            },
                            {
                                "id": "INDICATOR",
                                "keyPosition": 1,
                                "values": [{"id": code, "name": code} for code in sel_ind],
                            },
                        ],
                        "observation": [
                            {
                                "id": "TIME_PERIOD",
                                "keyPosition": 2,
                                "values": [
                                    {"id": "2020", "start": "2020-01-01T00:00:00"},
                                    {"id": "2021", "start": "2021-01-01T00:00:00"},
                                ],
                            }
                        ],
                    }
                },
            }
        }

    def handler(request: httpx.Request) -> httpx.Response:
        path = path_of(request)
        headers = {"content-type": "application/json"}
        if path.endswith("/catalog"):
            return httpx.Response(
                200,
                json={
                    "datasetMap": {"TR,DF_REGION,1.0": {"title": "Regional"}},
                    "categoryGroups": [],
                },
                headers=headers,
            )
        if path.endswith("/structure"):
            return httpx.Response(
                200,
                json={
                    "timeDimension": "TIME_PERIOD",
                    "criteria": [{"id": "REF_AREA"}, {"id": "INDICATOR"}, {"id": "TIME_PERIOD"}],
                    "template": {"hiddenDimensions": []},
                },
                headers=headers,
            )
        if path.endswith("/data"):
            return httpx.Response(
                200,
                json={
                    "id": ["REF_AREA", "INDICATOR", "TIME_PERIOD"],
                    "size": [1, 1, 1],
                    "role": {"time": ["TIME_PERIOD"]},
                    "value": {"0": "1"},
                },
                headers=headers,
            )
        if path.endswith("/download/json"):
            body = request_body(request)
            by_dim = {c["id"]: c["filterValues"] for c in body} if isinstance(body, list) else {}
            return httpx.Response(
                200,
                json=sdmx_payload(
                    by_dim.get("REF_AREA", ref_areas), by_dim.get("INDICATOR", ["IND"])
                ),
                headers=headers,
            )
        if path.endswith("/download/csv"):
            csv_bodies.append(request_body(request))
            return httpx.Response(
                200,
                content=b"REF_AREA,INDICATOR,TIME_PERIOD,OBS_VALUE\nTR100,IND,2020,1\nTR100,IND,2021,2\n",
                headers={"content-type": "text/csv"},
            )
        if "/PartialCodelists/" in path:
            dimension = path.rsplit("/", 1)[1]
            values = ref_areas if dimension == "REF_AREA" else ["IND"]
            body = request_body(request)
            if isinstance(body, list) and body:
                by_dim = {c["id"]: c["filterValues"] for c in body}
                expected = len(by_dim.get("REF_AREA", ref_areas)) * len(
                    by_dim.get("INDICATOR", ["IND"])
                )
                expected *= 2
            else:
                expected = len(ref_areas) * 2
            return httpx.Response(
                200,
                json={
                    "criteria": [
                        {
                            "id": dimension,
                            "values": [{"id": v, "isSelectable": True} for v in values],
                        }
                    ],
                    "obsCount": expected,
                },
                headers=headers,
            )
        raise AssertionError(path)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = Databrowser2Client(http_client=http_client, store=store, sleeper=lambda _: None)
    connector = TuikConnector(client=client)

    result = connector.catalog_dataflow("DF_REGION")
    assert result.complete
    codes = {meta.external_code for meta in result.metas}
    assert "DF_REGION:TR100.IND" in codes

    fetched = connector.fetch_series("DF_REGION", {"REF_AREA": "TR100", "INDICATOR": "IND"})
    assert fetched.points[-1] == (date(2021, 1, 1), Decimal("2"))
    csv_by_dim = {c["id"]: c["filterValues"] for c in csv_bodies[0]}
    assert csv_by_dim["REF_AREA"] == ["TR100"]
