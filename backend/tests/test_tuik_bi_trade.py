"""Unit tests for the bi.tuik Qlik foreign-trade connector (fake engine/HTTP)."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.connectors.base import FORMAT_CHANGED, ConnectorError, DatasetMeta
from app.connectors.tuik.bi_trade import (
    HEADLINE_BY_NAME,
    TOTAL_CODE,
    BiSystem,
    BiTradeClient,
    BiTradeConnector,
    complete_codes,
    derive_parents,
    dimension_codes,
    dimension_specs,
    external_code,
    get_system,
    headline_code,
    month_period,
    product_field,
    selection_for,
    split_headline,
    to_catalog_code,
    to_engine_value,
    validate_codes,
)
from app.data.errors import SeriesDefinitionError

GTS = get_system("gts")
OTS = get_system("ots")


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


# --- pure logic -----------------------------------------------------------


def test_get_system_accepts_key_and_dataset_code() -> None:
    assert get_system("gts").key == "gts"
    assert get_system("TUIK_BI_OTS").key == "ots"
    with pytest.raises(ConnectorError) as exc:
        get_system("nope")
    assert exc.value.kind == "not_found"


def test_dimension_specs_gts_has_more_dimensions_than_ots() -> None:
    gts = [spec.code for spec in dimension_specs(GTS)]
    ots = [spec.code for spec in dimension_specs(OTS)]
    assert "CUSTOMS" in gts and "INVOICE_CURRENCY" in gts
    assert all(dimension not in ots for dimension in ("CUSTOMS", "TRANSPORT", "PAYMENT"))
    assert gts[-1] == "MEASURE" and ots[-1] == "MEASURE"
    measure = dimension_specs(GTS)[-1]
    assert measure.total is False


def test_complete_codes_fills_totals_and_requires_a_measure() -> None:
    codes = complete_codes(OTS, {"FLOW": "X", "MEASURE": "USD"})
    assert codes["FLOW"] == "X"
    assert codes["MEASURE"] == "USD"
    assert codes["PARTNER"] == TOTAL_CODE
    assert codes["PRODUCT_HS"] == TOTAL_CODE
    with pytest.raises(SeriesDefinitionError):
        complete_codes(OTS, {"FLOW": "X"})


def test_validate_codes_rejects_several_product_dimensions() -> None:
    codes = complete_codes(OTS, {"MEASURE": "USD", "PRODUCT_HS": "94", "PRODUCT_BEC": "1"})
    with pytest.raises(SeriesDefinitionError):
        validate_codes(OTS, codes)


def test_validate_codes_rejects_partner_and_partner_group() -> None:
    codes = complete_codes(OTS, {"MEASURE": "USD", "PARTNER": "81", "PARTNER_GROUP": "CG:5490"})
    with pytest.raises(SeriesDefinitionError):
        validate_codes(OTS, codes)


def test_validate_codes_rejects_quantity_without_a_product() -> None:
    codes = complete_codes(OTS, {"MEASURE": "QTY1"})
    with pytest.raises(SeriesDefinitionError):
        validate_codes(OTS, codes)
    with_product = complete_codes(OTS, {"MEASURE": "QTY1", "PRODUCT_HS": "94033011"})
    validate_codes(OTS, with_product)


def test_validate_codes_accepts_a_plain_slice() -> None:
    validate_codes(OTS, complete_codes(OTS, {"MEASURE": "USD", "FLOW": "M"}))


def test_product_field_maps_hs_levels_by_length() -> None:
    assert product_field("PRODUCT_HS", "94") == "FASIL"
    assert product_field("PRODUCT_HS", "9403") == "TARIFE4"
    assert product_field("PRODUCT_HS", "940330") == "TARIFE6"
    assert product_field("PRODUCT_HS", "94033011") == "TARIFE8"
    assert product_field("PRODUCT_HS", "940330110000") == "ISTPOZ"
    with pytest.raises(SeriesDefinitionError):
        product_field("PRODUCT_HS", "940")


def test_hs_codes_pad_to_their_canonical_width_both_directions() -> None:
    assert to_catalog_code("PRODUCT_HS", "1") == "01"
    assert to_catalog_code("PRODUCT_HS", "9") == "09"
    assert to_catalog_code("PRODUCT_HS", "94") == "94"
    assert to_catalog_code("PRODUCT_HS", "704") == "0704"
    assert to_catalog_code("PRODUCT_HS", "70410") == "070410"
    assert to_catalog_code("PRODUCT_HS", "7049010") == "07049010"
    assert to_catalog_code("PRODUCT_HS", "70490101234") == "070490101234"
    # Already canonical values are unchanged (idempotent).
    assert to_catalog_code("PRODUCT_HS", "0101") == "0101"
    assert to_catalog_code("PRODUCT_HS", "94033011") == "94033011"
    assert to_engine_value("PRODUCT_HS", "01") == "1"
    assert to_engine_value("PRODUCT_HS", "09") == "9"
    assert to_engine_value("PRODUCT_HS", "94") == "94"
    assert to_engine_value("PRODUCT_HS", "0704") == "704"
    assert to_engine_value("PRODUCT_HS", "070410") == "70410"
    assert to_engine_value("PRODUCT_HS", "94033011") == "94033011"
    # Other dimensions are untouched.
    assert to_catalog_code("PRODUCT_SITC", "1") == "1"
    assert to_engine_value("PRODUCT_SITC", "01") == "01"


def test_selection_for_maps_hs_levels_back_to_the_engine_value() -> None:
    assert product_field("PRODUCT_HS", "01") == "FASIL"
    assert product_field("PRODUCT_HS", "0704") == "TARIFE4"
    assert selection_for("PRODUCT_HS", "01") == ("FASIL", "1")
    assert selection_for("PRODUCT_HS", "09") == ("FASIL", "9")
    assert selection_for("PRODUCT_HS", "94") == ("FASIL", "94")
    assert selection_for("PRODUCT_HS", "0704") == ("TARIFE4", "704")
    assert selection_for("PRODUCT_HS", "070410") == ("TARIFE6", "70410")


def test_product_field_maps_other_classifications() -> None:
    assert product_field("PRODUCT_SITC", "3XXXX") == "SITC4_5"
    assert product_field("PRODUCT_ISIC", "A") == "ISIC4_1"
    assert product_field("PRODUCT_BEC", "1") == "BEC_1"
    assert product_field("PRODUCT_BEC", "21") == "BEC"
    assert product_field("PRODUCT_BEC", "7") == "BEC"


def test_selection_for_maps_flow_groups_and_products() -> None:
    assert selection_for("FLOW", "X") == ("IHRITH", "İhracat")
    assert selection_for("FLOW", "M") == ("IHRITH", "İthalat")
    assert selection_for("PARTNER", "81") == ("ULKE_KODU", "81")
    assert selection_for("PARTNER_GROUP", "CG:5490") == ("ULKE_COGRAFI_GRUBU", "5490")
    assert selection_for("PARTNER_GROUP", "EG:1113") == ("ULKE_EKONOMI_GRUBU", "1113")
    assert selection_for("PRODUCT_HS", "94033011") == ("TARIFE8", "94033011")
    assert selection_for("PROVINCE", "41") == ("IL_KODU", "41")
    assert selection_for("PARTNER", TOTAL_CODE) is None
    with pytest.raises(SeriesDefinitionError):
        selection_for("NOPE", "1")


def test_derive_parents_uses_the_longest_present_prefix() -> None:
    parents = derive_parents(["94", "9403", "940330", "94033011", "4011"])
    assert parents["94033011"] == "940330"
    assert parents["940330"] == "9403"
    assert parents["9403"] == "94"
    assert parents["94"] is None
    assert parents["4011"] is None


def test_month_period_validates_the_pair() -> None:
    assert month_period("2026", "7") == date(2026, 7, 1)
    assert month_period("2026", "13") is None
    assert month_period("", "7") is None
    assert month_period("2026", "-") is None


def test_split_headline_groups_rows_by_engine_values() -> None:
    spec = HEADLINE_BY_NAME["partner"]
    rows = [
        ["2026", "7", "İhracat", "81", "1000"],
        ["2026", "6", "İhracat", "81", "900"],
        ["2026", "7", "İthalat", "81", "2000"],
        ["2026", "7", "İhracat", "284", "500"],
        ["2026", "7", "İhracat", "-", "777"],
        ["bad", "7", "İhracat", "81", "1"],
    ]
    split = split_headline(rows, spec)
    assert split[("İhracat", "81")] == [
        (date(2026, 7, 1), Decimal("1000")),
        (date(2026, 6, 1), Decimal("900")),
    ]
    assert split[("İthalat", "81")] == [(date(2026, 7, 1), Decimal("2000"))]
    assert split[("İhracat", "284")] == [(date(2026, 7, 1), Decimal("500"))]


def test_headline_code_maps_flow_names() -> None:
    assert headline_code("FLOW", "İhracat") == "X"
    assert headline_code("FLOW", "İthalat") == "M"
    assert headline_code("PROVINCE", "41") == "41"


def test_external_code_uses_dimension_order() -> None:
    codes = complete_codes(OTS, {"MEASURE": "USD", "FLOW": "X", "PARTNER": "81"})
    key = external_code(OTS, codes)
    assert key.startswith("TUIK_BI_OTS:")
    parts = key.split(":")[1].split(".")
    assert len(parts) == len(dimension_codes(OTS))
    assert parts[dimension_codes(OTS).index("FLOW")] == "X"
    assert parts[dimension_codes(OTS).index("MEASURE")] == "USD"


# --- engine client with a fake websocket ----------------------------------


class FakeConnection:
    """Scripted stand-in for a websockets sync connection."""

    def __init__(self, responder) -> None:
        self._responder = responder
        self.sent: list[dict[str, Any]] = []
        self._inbox: list[str] = []
        self.closed = False

    def send(self, message: str) -> None:
        msg = json.loads(message)
        self.sent.append(msg)
        for response in self._responder(msg):
            payload = dict(response)
            payload.setdefault("id", msg["id"])
            self._inbox.append(json.dumps({**payload}))

    def recv(self, timeout: float | None = None) -> str:
        if not self._inbox:
            raise TimeoutError
        return self._inbox.pop(0)

    def close(self) -> None:
        self.closed = True


def _http_client(csrf: str = "tok-1") -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if "csrftoken" in str(request.url):
            return httpx.Response(204, headers={"qlik-csrf-token": csrf})
        return httpx.Response(
            200,
            headers=[
                ("set-cookie", "X-Qlik-Session-Anon=anon; Path=/"),
                ("set-cookie", "NSC_ESNS=esns; Path=/"),
            ],
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


def _open_responder(msg: dict[str, Any]):
    method = msg["method"]
    if method == "OpenDoc":
        return [{"result": {"qReturn": {"qHandle": 7}}}]
    if method == "ClearAll":
        return [{"result": {}}]
    if method == "GetField":
        return [{"result": {"qReturn": {"qHandle": 11}}}]
    if method == "SelectValues":
        return [{"result": {"qReturn": True}}]
    if method == "EvaluateEx":
        return [{"result": {"qValue": {"qText": "1", "qIsNumeric": True, "qNumber": 1}}}]
    if method == "CreateSessionObject":
        return [{"result": {"qReturn": {"qHandle": 21}}}]
    if method == "GetLayout":
        return [{"result": {"qLayout": {"qHyperCube": {"qSize": {"qcx": 2, "qcy": 0}}}}}]
    if method == "DestroySessionObject":
        return [{"result": {}}]
    return [{"result": {}}]


def _client(responder, *, factory=None, **settings: Any) -> BiTradeClient:
    base = _settings(**settings)
    return BiTradeClient(
        settings_obj=base,
        store=None,
        http_client=_http_client(),
        connection_factory=factory or (lambda url, headers: FakeConnection(responder)),
        sleeper=lambda _seconds: None,
    )


def test_bootstrap_reads_the_csrf_header_and_cookies() -> None:
    client = _client(_open_responder)
    try:
        session = client.bootstrap(1)
        assert session.csrf_token == "tok-1"
        assert session.cookies == "X-Qlik-Session-Anon=anon; NSC_ESNS=esns"
        assert len(session.xrf_key) == 16
    finally:
        client.close()


def test_bootstrap_errors_without_a_csrf_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    client = BiTradeClient(
        settings_obj=_settings(),
        store=None,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        connection_factory=lambda url, headers: FakeConnection(_open_responder),
    )
    with pytest.raises(ConnectorError) as exc:
        client.bootstrap(1)
    assert exc.value.kind == "source_error"
    client.close()


def test_jsonrpc_matching_skips_an_unrelated_id() -> None:
    def responder(msg: dict[str, Any]):
        if msg["method"] == "OpenDoc":
            return [
                {"id": 999, "result": {"ignore": True}},
                {"result": {"qReturn": {"qHandle": 7}}},
            ]
        return _open_responder(msg)

    client = _client(responder)
    try:
        client.open("gts")
        assert client._doc == 7  # noqa: SLF001 - asserting the negotiated handle
    finally:
        client.close()


def test_cube_rows_pages_at_the_configured_cell_budget() -> None:
    pages: list[int] = []

    def responder(msg: dict[str, Any]):
        if msg["method"] == "GetLayout":
            return [{"result": {"qLayout": {"qHyperCube": {"qSize": {"qcx": 3, "qcy": 5}}}}}]
        if msg["method"] == "GetHyperCubeData":
            area = msg["params"][1][0]
            pages.append(area["qTop"])
            rows = [[str(area["qTop"] + i), "1", "10"] for i in range(area["qHeight"])]
            matrix = [[{"qText": cell} for cell in row] for row in rows]
            return [{"result": {"qDataPages": [{"qMatrix": matrix}]}}]
        return _open_responder(msg)

    client = _client(responder, tuik_bi_page_cells=2)
    try:
        client.open("gts")
        rows = client.cube_rows(["YIL", "AY"], "Sum(DOLAR)", dataset="TUIK_BI_GTS")
    finally:
        client.close()
    assert len(rows) == 5
    assert pages == [0, 1, 2, 3, 4]


def test_select_field_uses_numeric_only_for_yil_and_ay() -> None:
    selections: list[dict[str, Any]] = []

    def responder(msg: dict[str, Any]):
        if msg["method"] == "SelectValues":
            selections.append(msg["params"][0][0])
            return [{"result": {"qReturn": True}}]
        return _open_responder(msg)

    client = _client(responder)
    try:
        client.open("gts")
        client.select_field("YIL", "2026")
        client.select_field("PARTNER", "81")
    finally:
        client.close()
    assert selections[0] == {"qText": "2026", "qIsNumeric": True, "qNumber": 2026.0}
    assert selections[1] == {"qText": "81", "qIsNumeric": False}


def test_select_field_raises_not_found_when_the_engine_selects_nothing() -> None:
    def responder(msg: dict[str, Any]):
        if msg["method"] == "EvaluateEx":
            return [{"result": {"qValue": {"qText": "0", "qIsNumeric": True, "qNumber": 0}}}]
        return _open_responder(msg)

    client = _client(responder)
    try:
        client.open("gts")
        with pytest.raises(ConnectorError) as exc:
            client.select_field("FASIL", "ZZZZ")
    finally:
        client.close()
    assert exc.value.kind == "not_found"
    assert "FASIL" in exc.value.message and "ZZZZ" in exc.value.message


def test_selected_count_reads_the_engine_expression() -> None:
    expressions: list[str] = []

    def responder(msg: dict[str, Any]):
        if msg["method"] == "EvaluateEx":
            expressions.append(msg["params"][0])
            return [{"result": {"qValue": {"qText": "3", "qIsNumeric": True, "qNumber": 3}}}]
        return _open_responder(msg)

    client = _client(responder)
    try:
        client.open("gts")
        assert client.selected_count("PARTNER") == 3
    finally:
        client.close()
    assert expressions == ["GetSelectedCount([PARTNER])"]


def test_call_reconnects_and_reapplies_selections_after_a_dropped_socket() -> None:
    created: list[FakeConnection] = []

    def responder(msg: dict[str, Any]):
        if msg["method"] == "GetTablesAndKeys":
            return [{"result": {"qtr": [{"qName": "DT_GENEL", "qFields": [{"qName": "YIL"}]}]}}]
        return _open_responder(msg)

    class DroppingConnection(FakeConnection):
        def __init__(self) -> None:
            super().__init__(responder)
            self._dropped = False

        def recv(self, timeout: float | None = None) -> str:
            if self.sent[-1]["method"] == "GetTablesAndKeys" and not self._dropped:
                self._dropped = True
                raise _Dropped()
            return super().recv(timeout)

    def factory(url: str, headers: Any) -> FakeConnection:
        connection = DroppingConnection() if not created else FakeConnection(responder)
        created.append(connection)
        return connection

    client = _client(responder, factory=factory)
    try:
        client.open("gts")
        client.select_field("PARTNER", "81")
        fields = client.fields("TUIK_BI_GTS")
    finally:
        client.close()
    assert fields == ["YIL"]
    assert len(created) == 2
    # The selection was re-applied on the new connection.
    second = created[1]
    assert any(message["method"] == "SelectValues" for message in second.sent)
    assert created[0].closed is True


class _Dropped(Exception):
    """Raised by the fake connection to simulate a closed socket."""


# --- connector with a fake client -----------------------------------------


class FakeClient:
    """Minimal engine double for connector-level tests."""

    def __init__(self, *, pairs=None, cubes=None, tables=None, missing=()) -> None:
        self._pairs = pairs or {}
        self._cubes = cubes or {}
        self._tables = tables or []
        self._missing = set(missing)
        self.selected: list[tuple[str, str]] = []
        self.cleared = 0
        self.opened: list[str] = []

    def open(self, system) -> None:
        self.opened.append(system.key if isinstance(system, BiSystem) else str(system))

    def close(self) -> None:
        return None

    def tables(self, dataset: str):
        return [
            {
                "qName": "DT_GENEL",
                "qNoOfRows": 100,
                "qFields": [{"qName": name} for name in self._tables if name not in self._missing],
            }
        ]

    def fields(self, dataset: str):
        return [field["qName"] for field in self.tables(dataset)[0]["qFields"]]

    def row_count(self, dataset: str):
        return 100

    def list_field_pairs(self, code_field: str, name_field: str | None, *, dataset: str):
        return self._pairs.get(code_field, [])

    def cube_rows(self, dimensions, measure, *, dataset, suppress_zero=True):
        key = (tuple(dimensions), measure)
        return self._cubes.get(key, [])

    def select_field(self, name: str, value: str) -> None:
        self.selected.append((name, value))

    def clear_all(self) -> None:
        self.cleared += 1

    def take_raw_keys(self):
        return []


def _field_names(gts: bool = True) -> list[str]:
    names = ["YIL", "AY", "IHRITH", "ULKE_KODU", "ULKE_ADI"]
    for spec in dimension_specs(GTS if gts else OTS):
        for source in spec.sources:
            names.append(source.code_field)
            if source.name_field:
                names.append(source.name_field)
    return names


def test_dataset_meta_builds_codes_totals_and_parents() -> None:
    pairs = {
        "IHRITH": [("İhracat", "İhracat", 1), ("İthalat", "İthalat", 1)],
        "ULKE_KODU": [("81", "Özbekistan", 5)],
        "ULKE_COGRAFI_GRUBU": [("5490", "Diğer Asya", 5)],
        "ULKE_EKONOMI_GRUBU": [("1113", "AB 27", 5)],
        "FASIL": [("94", "Mobilyalar", 5)],
        "TARIFE4": [("9403", "Diğer mobilyalar", 5)],
        "TARIFE6": [("940330", "Ahşap ofis mobilyaları", 5)],
        "TARIFE8": [("94033011", "Ofis masaları", 5)],
        "ISTPOZ": [("940330110000", "Ofis masaları", 5)],
        "SITC4_1": [("3", "Enerji", 5)],
        "SITC4_2": [("3X", "Enerji", 5)],
        "ISIC4_1": [("C", "İmalat", 5)],
        "ISIC4_2": [("31", "Mobilya", 5)],
        "BEC_1": [("1", "Yatırım", 5)],
        "BEC": [("41", "Sermaye", 5)],
        "IL_KODU": [("41", "KOCAELİ", 5)],
        "YIL": [("2026", "2026", 5)],
    }
    client = FakeClient(pairs=pairs, tables=_field_names())
    connector = BiTradeConnector(client=client)
    meta = connector.dataset_meta(GTS)
    assert meta.external_code == "TUIK_BI_GTS"
    assert meta.attributes["channel"] == "bi_qlik"
    assert meta.attributes["default_frequency"] == "monthly"
    assert meta.obs_count == 100
    assert (meta.coverage_start, meta.coverage_end) == (date(2026, 1, 1), date(2026, 12, 1))
    dims = {dimension.code: dimension for dimension in meta.dimensions}
    flow = dims["FLOW"]
    assert {code.code for code in flow.codes} == {TOTAL_CODE, "X", "M"}
    assert {code.code: code.label for code in flow.codes}[TOTAL_CODE] == "Toplam"
    group = dims["PARTNER_GROUP"]
    assert {"CG:5490", "EG:1113"} <= {code.code for code in group.codes}
    hs = dims["PRODUCT_HS"]
    hs_codes = {code.code: code for code in hs.codes}
    assert hs_codes["94033011"].parent_code == "940330"
    assert hs_codes["94033011"].attributes["level"] == 4
    assert hs_codes["94033011"].attributes["field"] == "TARIFE8"
    assert dims["MEASURE"].codes[0].code == "USD"
    assert TOTAL_CODE not in {code.code for code in dims["MEASURE"].codes}
    time_dimension = dims["TIME_PERIOD"]
    assert time_dimension.role == "time"


def test_dataset_meta_pads_hs_chapters_and_parents_four_digit_codes() -> None:
    pairs = {
        "FASIL": [("1", "Canlı hayvanlar", 5)],
        "TARIFE4": [("101", "Atlar", 5)],
        "YIL": [("2026", "2026", 5)],
    }
    client = FakeClient(pairs=pairs, tables=_field_names())
    meta = BiTradeConnector(client=client).dataset_meta(OTS)
    hs = {dimension.code: dimension for dimension in meta.dimensions}["PRODUCT_HS"]
    by_code = {code.code: code for code in hs.codes}
    assert "01" in by_code and "1" not in by_code
    assert "0101" in by_code and "101" not in by_code
    assert by_code["01"].label == "Canlı hayvanlar"
    assert by_code["01"].attributes["field"] == "FASIL"
    assert by_code["0101"].parent_code == "01"


def test_dataset_meta_flags_a_changed_app() -> None:
    client = FakeClient(pairs={}, tables=_field_names(), missing={"IHRITH"})
    connector = BiTradeConnector(client=client)
    with pytest.raises(ConnectorError) as exc:
        connector.dataset_meta(GTS)
    assert exc.value.kind == FORMAT_CHANGED


def test_fetch_series_selects_only_non_total_dimensions() -> None:
    def cube(dimensions, measure, *, dataset, suppress_zero=True):
        assert tuple(dimensions) == ("YIL", "AY")
        return [["2026", "7", "25620124967"], ["2026", "6", "24883042090"]]

    client = FakeClient(cubes={})
    client.cube_rows = cube  # type: ignore[method-assign]
    connector = BiTradeConnector(client=client)
    result = connector.fetch_series(
        "TUIK_BI_GTS", {"FLOW": "X", "MEASURE": "USD", "PRODUCT_HS": "94033011"}
    )
    assert ("IHRITH", "İhracat") in client.selected
    assert ("TARIFE8", "94033011") in client.selected
    assert all(field != "ULKE_KODU" for field, _ in client.selected)
    assert client.cleared >= 2
    assert result.points == [
        (date(2026, 6, 1), Decimal("24883042090")),
        (date(2026, 7, 1), Decimal("25620124967")),
    ]
    assert result.channel == "bi_qlik"


def test_fetch_series_raises_empty_when_the_selection_has_no_rows() -> None:
    client = FakeClient()
    client.cube_rows = lambda *args, **kwargs: []  # type: ignore[method-assign]
    connector = BiTradeConnector(client=client)
    with pytest.raises(ConnectorError) as exc:
        connector.fetch_series("TUIK_BI_GTS", {"FLOW": "X", "MEASURE": "USD"})
    assert exc.value.kind == "empty"


def test_headline_series_splits_and_maps_codes() -> None:
    def cube(dimensions, measure, *, dataset, suppress_zero=True):
        assert tuple(dimensions) == ("YIL", "AY", "IHRITH", "ULKE_KODU")
        return [
            ["2026", "7", "İhracat", "81", "1000"],
            ["2026", "7", "İthalat", "81", "2000"],
        ]

    client = FakeClient()
    client.cube_rows = cube  # type: ignore[method-assign]
    connector = BiTradeConnector(client=client)
    series = connector.headline_series(GTS, only=["partner"])
    by_flow = {entry.codes["FLOW"]: entry for entry in series}
    assert set(by_flow) == {"X", "M"}
    assert by_flow["X"].codes["PARTNER"] == "81"
    assert by_flow["X"].codes["MEASURE"] == "USD"
    assert by_flow["X"].codes["PRODUCT_HS"] == TOTAL_CODE
    assert by_flow["M"].points == [(date(2026, 7, 1), Decimal("2000"))]


def test_headline_series_pads_hs_chapter_codes() -> None:
    def cube(dimensions, measure, *, dataset, suppress_zero=True):
        assert tuple(dimensions) == ("YIL", "AY", "IHRITH", "FASIL")
        return [["2026", "7", "İhracat", "1", "1000"]]

    client = FakeClient()
    client.cube_rows = cube  # type: ignore[method-assign]
    series = BiTradeConnector(client=client).headline_series(GTS, only=["product_hs"])
    assert [entry.codes["PRODUCT_HS"] for entry in series] == ["01"]


def test_list_field_pairs_dedupes_keeping_the_richest_label() -> None:
    def responder(msg: dict[str, Any]):
        if msg["method"] == "GetLayout":
            return [{"result": {"qLayout": {"qHyperCube": {"qSize": {"qcx": 3, "qcy": 2}}}}}]
        if msg["method"] == "GetHyperCubeData":
            return [
                {
                    "result": {
                        "qDataPages": [
                            {
                                "qMatrix": [
                                    [{"qText": "11"}, {"qText": "short"}, {"qText": "3"}],
                                    [{"qText": "11"}, {"qText": "long name"}, {"qText": "9"}],
                                ]
                            }
                        ]
                    }
                }
            ]
        return _open_responder(msg)

    client = _client(responder)
    try:
        client.open("gts")
        pairs = client.list_field_pairs("SOZLESME_KODU", "SOZLESME_ADI", dataset="TUIK_BI_GTS")
    finally:
        client.close()
    assert pairs == [("11", "long name", 9)]


def test_dataset_meta_is_a_dataset_meta() -> None:
    client = FakeClient(
        pairs={"YIL": [("2026", "2026", 1)]},
        tables=_field_names(),
    )
    meta = BiTradeConnector(client=client).dataset_meta(OTS)
    assert isinstance(meta, DatasetMeta)
    assert meta.attributes["system"] == "ots"
