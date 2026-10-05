"""Unit tests for the TÜİK nsiws SDMX backup channel (no network)."""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.connectors.base import (
    EMPTY,
    FORMAT_CHANGED,
    NOT_FOUND,
    SOURCE_ERROR,
    THROTTLED,
    TIMEOUT,
    ConnectorError,
    FetchResult,
    InMemoryObjectStore,
)
from app.connectors.tuik.connector import TuikConnector
from app.connectors.tuik.nsiws import (
    NsiwsClient,
    NsiwsTokenManager,
    build_nsiws_client,
    compare_points,
    parse_generic_data,
    period_param,
)
from app.connectors.tuik.parsers import DataflowInfo, parse_sdmx_csv_points

FIXTURES = Path(__file__).parent / "fixtures" / "tuik"
NSIWS = FIXTURES / "nsiws"

TUFE_META = DataflowInfo(
    dataflow_id="DF_TUFE_SDMX_TT10",
    version="1.0",
    agency="TR",
    title="CPI",
    description=None,
    source_category=None,
)
GSYH_META = DataflowInfo(
    dataflow_id="UH_BH_GSYH_CARI",
    version="1.0",
    agency="TR",
    title="Regional GDP",
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

TUFE_DIMENSIONS = [
    "REF_AREA",
    "FREQ",
    "SINIFLAMA_DUZEYI",
    "DEGISIM",
    "OZEL_KAPSAM_TUFE",
    "BASE_PER",
    "YAYIM_DONEMI",
    "COICOP_1999",
    "COICOP_2018",
    "INDICATOR",
]
GSYH_DIMENSIONS = [
    "REF_AREA",
    "FREQ",
    "INDICATOR",
    "DUZEY_SEVIYE",
    "KIRILIM_SEVIYE",
    "FAALIYET_KOD",
    "UNIT_MEASURE",
    "ENDEKS_DEGISIM",
    "YAYIN_TUIK",
    "BAZ_YILI",
    "YAYIM_DONEMI",
]


def _fixture(path: Path, name: str) -> bytes:
    return (path / name).read_bytes()


def _json_response(name: str) -> httpx.Response:
    return httpx.Response(
        200, content=_fixture(NSIWS, name), headers={"content-type": "application/json"}
    )


def _xml_response(name: str) -> httpx.Response:
    return httpx.Response(
        200,
        content=_fixture(NSIWS, name),
        headers={"content-type": "application/vnd.sdmx.genericdata+xml"},
    )


class _Clock:
    def __init__(self, value: float = 1000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def make_client(
    handler: Any,
    *,
    api_key: str | None = "test-key",
    store: InMemoryObjectStore | None = None,
    clock: _Clock | None = None,
) -> NsiwsClient:
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    return NsiwsClient(
        base_url="https://nsiws.example/rest",
        token_url="https://token.example/token",
        client_id="nsi-ws-consumer",
        api_key=api_key,
        http_client=http_client,
        store=store,
        sleeper=lambda _: None,
        clock=clock or _Clock(),
    )


def metadata_handler(extra: Any = None) -> Any:
    """Serve the dataflow + DSD metadata fixtures, delegating everything else."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/token":
            return _json_response("token-response.raw.json")
        if path.endswith("/DF_TUFE_SDMX_TT10/1.0"):
            return _xml_response("df-tufe.dataflow.raw.xml")
        if path.endswith("/DSD_TUFE/1.12"):
            return _xml_response("dsd-tufe.raw.xml")
        if path.endswith("/UH_BH_GSYH_CARI/1.0"):
            return _xml_response("df-gsyh.dataflow.raw.xml")
        if path.endswith("/DSD_BOLGESEL_GSYH/1.1"):
            return _xml_response("dsd-gsyh.raw.xml")
        if extra is not None:
            return extra(request)
        raise AssertionError(f"unexpected request: {path}")

    return handler


# --- token manager -------------------------------------------------------


def test_token_is_fetched_then_cached_and_refreshed_before_expiry() -> None:
    clock = _Clock()
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return _json_response("token-response.raw.json")

    manager = NsiwsTokenManager(
        token_url="https://token.example/token",
        api_key="test-key",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        refresh_skew_s=60.0,
        sleeper=lambda _: None,
        clock=clock,
    )
    assert manager.token() == "test-access-token"
    assert manager.token() == "test-access-token"
    assert calls["count"] == 1
    # expires_in=300 with a 60 s skew -> valid for 240 s.
    clock.value += 239
    assert manager.token() == "test-access-token"
    assert calls["count"] == 1
    clock.value += 2
    assert manager.token() == "test-access-token"
    assert calls["count"] == 2


def test_token_invalidate_forces_a_new_fetch() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return _json_response("token-response.raw.json")

    manager = NsiwsTokenManager(
        token_url="https://token.example/token",
        api_key="test-key",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=lambda _: None,
    )
    manager.token()
    manager.invalidate()
    manager.token()
    assert calls["count"] == 2


def test_token_manager_without_key_is_not_configured() -> None:
    manager = NsiwsTokenManager(token_url="https://token.example/token", api_key="")
    assert manager.configured is False
    assert manager.token() is None


# --- metadata discovery --------------------------------------------------


def test_dimension_order_follows_the_dsd_positions() -> None:
    client = make_client(metadata_handler())
    assert client.dimension_order("DF_TUFE_SDMX_TT10", "1.0") == TUFE_DIMENSIONS
    assert client.dimension_order("UH_BH_GSYH_CARI", "1.0") == GSYH_DIMENSIONS


def test_dimension_order_is_cached() -> None:
    calls = {"count": 0}

    def counting(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return metadata_handler()(request)

    client = make_client(counting)
    client.dimension_order("DF_TUFE_SDMX_TT10", "1.0")
    client.dimension_order("DF_TUFE_SDMX_TT10", "1.0")
    # token + dataflow + DSD once each.
    assert calls["count"] == 3


def test_build_key_wildcards_hidden_dimensions() -> None:
    client = make_client(metadata_handler())
    key = client.build_key(TUFE_DIMENSIONS, TUFE_CODES)
    assert key == "TR.M.TUFE.1.%20.2025.%20.%20.0.F_TFE"
    gdp = client.build_key(GSYH_DIMENSIONS, GSYH_CODES)
    assert gdp == "TR.A.%20.%20.A10.B1GQ.TTRY.%20.%20.2009.%20"


def test_build_key_uses_codes_for_every_catalogued_dimension() -> None:
    client = make_client(metadata_handler())
    codes = {dimension: f"C{index}" for index, dimension in enumerate(TUFE_DIMENSIONS)}
    codes["REF_AREA"] = "TR"
    key = client.build_key(TUFE_DIMENSIONS, codes)
    assert "%20" not in key
    assert key == ".".join(codes[dimension] for dimension in TUFE_DIMENSIONS)


def test_build_key_rejects_unknown_dimension() -> None:
    client = make_client(metadata_handler())
    with pytest.raises(ConnectorError) as excinfo:
        client.fetch_series("DF_TUFE_SDMX_TT10", {**TUFE_CODES, "NOPE": "1"}, version="1.0")
    assert excinfo.value.kind == FORMAT_CHANGED


# --- XML parsing ---------------------------------------------------------


def test_parse_generic_data_monthly_tufe() -> None:
    points = parse_generic_data(_fixture(NSIWS, "tufe-tt10.data.raw.xml"), TUFE_CODES)
    assert len(points) == 260
    assert points[0] == (date(2005, 1, 1), Decimal("3.596523"))
    assert points[-1][0] == date(2026, 8, 1)


def test_parse_generic_data_annual_gdp() -> None:
    points = parse_generic_data(_fixture(NSIWS, "uh-bh-gsyh-cari.data.raw.xml"), GSYH_CODES)
    assert len(points) == 25
    assert points[0] == (date(2000, 1, 1), Decimal("171777959.413194"))
    assert points[-1] == (date(2024, 1, 1), Decimal("44587225440.1984"))


def test_parse_generic_data_rejects_non_xml() -> None:
    with pytest.raises(ConnectorError) as excinfo:
        parse_generic_data(b"not xml", TUFE_CODES)
    assert excinfo.value.kind == FORMAT_CHANGED


def test_parse_generic_data_without_matching_series_is_empty() -> None:
    body = _fixture(NSIWS, "tufe-tt10.data.raw.xml")
    with pytest.raises(ConnectorError) as excinfo:
        parse_generic_data(body, {**TUFE_CODES, "DEGISIM": "999"})
    assert excinfo.value.kind == EMPTY


# --- HTTP error mapping --------------------------------------------------


def test_fetch_series_maps_404_to_not_found() -> None:
    def extra(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404, content=b"NoRecordsFound", headers={"content-type": "text/plain"}
        )

    client = make_client(metadata_handler(extra))
    with pytest.raises(ConnectorError) as excinfo:
        client.fetch_series("DF_TUFE_SDMX_TT10", TUFE_CODES, version="1.0")
    assert excinfo.value.kind == NOT_FOUND


def test_fetch_series_maps_422_to_format_changed() -> None:
    def extra(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            content=b"Semantic Error - Invalid Date Format `abc`",
            headers={"content-type": "text/plain"},
        )

    client = make_client(metadata_handler(extra))
    with pytest.raises(ConnectorError) as excinfo:
        client.fetch_series("DF_TUFE_SDMX_TT10", TUFE_CODES, version="1.0")
    assert excinfo.value.kind == FORMAT_CHANGED


def test_fetch_series_retries_timeouts_then_succeeds() -> None:
    state = {"n": 0}

    def extra(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] <= 2:
            raise httpx.ReadTimeout("boom", request=request)
        return _xml_response("tufe-tt10.data.raw.xml")

    store = InMemoryObjectStore()
    client = make_client(metadata_handler(extra), store=store)
    result = client.fetch_series("DF_TUFE_SDMX_TT10", TUFE_CODES, version="1.0")
    assert result.channel == "nsiws"
    assert len(result.points) == 260
    assert state["n"] == 3
    assert result.raw_object_keys and result.raw_object_keys[0] in store.objects


def test_fetch_series_exhausted_timeouts_is_timeout() -> None:
    def extra(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("boom", request=request)

    client = make_client(metadata_handler(extra))
    with pytest.raises(ConnectorError) as excinfo:
        client.fetch_series("DF_TUFE_SDMX_TT10", TUFE_CODES, version="1.0")
    assert excinfo.value.kind == TIMEOUT


def test_fetch_series_500_is_source_error_after_retries() -> None:
    def extra(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom", headers={"content-type": "text/plain"})

    client = make_client(metadata_handler(extra))
    with pytest.raises(ConnectorError) as excinfo:
        client.fetch_series("DF_TUFE_SDMX_TT10", TUFE_CODES, version="1.0")
    assert excinfo.value.kind == SOURCE_ERROR


def test_fetch_series_refreshes_token_once_on_401() -> None:
    state = {"token_calls": 0, "data_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/token":
            state["token_calls"] += 1
            return _json_response("token-response.raw.json")
        if path.endswith("/1.0"):
            return _xml_response("df-tufe.dataflow.raw.xml")
        if path.endswith("/DSD_TUFE/1.12"):
            return _xml_response("dsd-tufe.raw.xml")
        state["data_calls"] += 1
        if state["data_calls"] == 1:
            return httpx.Response(
                401, content=b'{"status":401}', headers={"content-type": "application/json"}
            )
        return _xml_response("tufe-tt10.data.raw.xml")

    client = make_client(handler)
    result = client.fetch_series("DF_TUFE_SDMX_TT10", TUFE_CODES, version="1.0")
    assert len(result.points) == 260
    assert state["token_calls"] == 2
    assert state["data_calls"] == 2


# --- value parity helper -------------------------------------------------


def test_compare_points_reports_mismatches() -> None:
    left = [(date(2024, 1, 1), Decimal("1")), (date(2024, 2, 1), Decimal("2"))]
    right = [(date(2024, 1, 1), Decimal("1")), (date(2024, 3, 1), Decimal("3"))]
    report = compare_points(left, right)
    assert not report.matching
    assert report.left_only == (date(2024, 2, 1),)
    assert report.right_only == (date(2024, 3, 1),)
    assert report.value_mismatches == ()
    assert compare_points(left, left).matching


def test_nsiws_matches_databrowser2_for_tufe_and_gdp() -> None:
    tufe_nsiws = parse_generic_data(_fixture(NSIWS, "tufe-tt10.data.raw.xml"), TUFE_CODES)
    tufe_db2 = parse_sdmx_csv_points(
        _fixture(FIXTURES, "tufe-tt10.databrowser2-download.raw.csv"), TUFE_CODES
    )
    assert compare_points(tufe_nsiws, tufe_db2).matching

    gdp_nsiws = parse_generic_data(_fixture(NSIWS, "uh-bh-gsyh-cari.data.raw.xml"), GSYH_CODES)
    gdp_db2 = parse_sdmx_csv_points(
        _fixture(FIXTURES, "uh-bh-gsyh-cari.databrowser2-download.raw.csv"), GSYH_CODES
    )
    assert compare_points(gdp_nsiws, gdp_db2).matching


# --- period param --------------------------------------------------------


def test_period_param_formats_by_frequency() -> None:
    assert period_param("monthly", date(2025, 3, 1)) == "2025-03"
    assert period_param("quarterly", date(2025, 7, 1)) == "2025-Q3"
    assert period_param("annual", date(2009, 1, 1)) == "2009"
    assert period_param("biennial", date(2020, 1, 1)) == "2020"
    assert period_param("daily", date(2025, 3, 4)) == "2025-03-04"
    assert period_param(None, date(2025, 3, 1)) == "2025-03"


# --- channel disabled ----------------------------------------------------


def test_build_nsiws_client_disabled_without_key(caplog) -> None:
    with caplog.at_level(logging.INFO):
        client = build_nsiws_client(settings_obj=Settings(tuik_api_key=None))
    assert client is None
    assert "nsiws backup disabled" in caplog.text


def test_build_nsiws_client_enabled_with_key() -> None:
    client = build_nsiws_client(settings_obj=Settings(tuik_api_key="test-key"))
    assert client is not None
    assert client.configured is True


# --- connector fallback --------------------------------------------------


class FakeNsiws:
    def __init__(self, result: FetchResult | None = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[Any, ...]] = []
        self.closed = False

    def fetch_series(self, dataset_code, codes, *, version, order=None, start=date(2000, 1, 1)):
        self.calls.append((dataset_code, codes, version, order, start))
        if self.error is not None:
            raise self.error
        return self.result

    def close(self) -> None:
        self.closed = True


def _fallback_connector(kind: str, fake: FakeNsiws) -> TuikConnector:
    connector = TuikConnector(client=_StubClient(), nsiws=fake)
    info = TUFE_META

    def bombe(*args, **kwargs):
        raise ConnectorError(kind, "databrowser2 down")

    connector._fetch_databrowser2 = bombe  # type: ignore[method-assign]
    connector.resolve = lambda code: info  # type: ignore[method-assign]
    return connector


class _StubClient:
    max_concurrency = 2
    throttled = False

    def close(self) -> None:
        pass


@pytest.mark.parametrize("kind", [TIMEOUT, SOURCE_ERROR, THROTTLED, NOT_FOUND])
def test_fallback_triggers_each_kind(kind: str, caplog) -> None:
    result = FetchResult(
        external_code="DF_TUFE_SDMX_TT10:TR.M.TUFE.1.2025.0.F_TFE",
        points=[(date(2025, 1, 1), Decimal("88.578291"))],
        raw_object_keys=["sources/tuik/2026/09/30/DF_TUFE_SDMX_TT10/nsiws-data-x.xml"],
        channel="nsiws",
    )
    fake = FakeNsiws(result=result)
    connector = _fallback_connector(kind, fake)
    with caplog.at_level(logging.WARNING):
        fetched = connector.fetch_series("DF_TUFE_SDMX_TT10", TUFE_CODES)
    assert fetched.channel == "nsiws"
    assert fake.calls and fake.calls[0][0] == "DF_TUFE_SDMX_TT10"
    assert fake.calls[0][2] == "1.0"
    assert (
        f"tuik databrowser2 failed ({kind}), falling back to nsiws: DF_TUFE_SDMX_TT10"
        in caplog.text
    )


@pytest.mark.parametrize("kind", [FORMAT_CHANGED, EMPTY])
def test_no_fallback_for_non_trigger_kinds(kind: str) -> None:
    fake = FakeNsiws(result=FetchResult("x", [], [], "nsiws"))
    connector = _fallback_connector(kind, fake)
    with pytest.raises(ConnectorError) as excinfo:
        connector.fetch_series("DF_TUFE_SDMX_TT10", TUFE_CODES)
    assert excinfo.value.kind == kind
    assert fake.calls == []


def test_no_fallback_without_nsiws_channel() -> None:
    connector = TuikConnector(client=_StubClient())

    def bombe(*args, **kwargs):
        raise ConnectorError(TIMEOUT, "down")

    connector._fetch_databrowser2 = bombe  # type: ignore[method-assign]
    with pytest.raises(ConnectorError) as excinfo:
        connector.fetch_series("DF_TUFE_SDMX_TT10", TUFE_CODES)
    assert excinfo.value.kind == TIMEOUT


def test_fallback_result_is_the_nsiws_result() -> None:
    result = FetchResult(
        external_code="x",
        points=[(date(2025, 1, 1), Decimal("1"))],
        raw_object_keys=[],
        channel="nsiws",
    )
    fake = FakeNsiws(result=result)
    connector = _fallback_connector(TIMEOUT, fake)
    assert connector.fetch_series("DF_TUFE_SDMX_TT10", TUFE_CODES) is result


def test_ambiguous_wildcard_match_raises_instead_of_guessing() -> None:
    from app.connectors.base import FORMAT_CHANGED, ConnectorError
    from app.connectors.tuik.nsiws import parse_generic_data

    def series(extra: str, value: str) -> str:
        return (
            "<generic:Series><generic:SeriesKey>"
            '<generic:Value id="REF_AREA" value="TR"/>'
            f'<generic:Value id="HIDDEN" value="{extra}"/>'
            "</generic:SeriesKey>"
            '<generic:Obs><generic:ObsDimension value="2024"/>'
            f'<generic:ObsValue value="{value}"/></generic:Obs>'
            "</generic:Series>"
        )

    xml = (
        '<message:GenericData xmlns:message="m" xmlns:generic="g">'
        "<message:DataSet>" + series("A", "1") + series("B", "2") + "</message:DataSet>"
        "</message:GenericData>"
    ).encode()
    with pytest.raises(ConnectorError) as excinfo:
        parse_generic_data(xml, {"REF_AREA": "TR"})
    assert excinfo.value.kind == FORMAT_CHANGED
    assert "ambiguous" in excinfo.value.message
