"""Unit tests for the TÜİK Veri Portalı connector (no network; fixtures)."""

from __future__ import annotations

import json
import logging
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.connectors.base import (
    FORMAT_CHANGED,
    NOT_FOUND,
    SOURCE_ERROR,
    THROTTLED,
    ConnectorError,
    InMemoryObjectStore,
)
from app.connectors.tuik import __main__ as tuik_cli
from app.connectors.tuik.__main__ import build_parser
from app.connectors.tuik.connector import TuikConnector
from app.connectors.tuik.parsers import points_from_sdmx_json
from app.connectors.tuik.veriportali import (
    CalendarCrawl,
    VeriPortaliClient,
    covers_all_press_years,
    html_to_text,
    parse_calendar,
    parse_dataflow_records,
    parse_press_detail,
    parse_press_types,
    parse_statistical_table_url,
    press_id_from_link,
    select_dataflow,
    split_portal_id,
)

FIXTURES = Path(__file__).parent / "fixtures" / "tuik" / "veriportali"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)


def fixture_json(name: str):
    return json.loads((FIXTURES / name).read_text("utf-8"))


def json_response(content: bytes, content_type: str = "application/json") -> httpx.Response:
    return httpx.Response(200, content=content, headers={"content-type": content_type})


# --- parsing ----------------------------------------------------------------


def test_split_portal_id_drops_the_v() -> None:
    assert split_portal_id("DF_TUFE_SDMX_TT01+V1.0") == ("DF_TUFE_SDMX_TT01", "1.0")
    assert split_portal_id("DF_X") == ("DF_X", "")


def test_parse_dataflow_records_category_path_and_version() -> None:
    records = parse_dataflow_records(fixture_json("dataflows.raw.json"))
    by_code = {record.external_code: record for record in records}
    assert "DF_TUFE_SDMX_TT01" in by_code
    tt01 = by_code["DF_TUFE_SDMX_TT01"]
    assert tt01.version == "1.0"
    assert tt01.category_path == ["Fiyat İstatistikleri", "Tüketici Fiyat Endeksi"]
    assert tt01.downloadable is True
    # A record without updatedAt still parses; footnotes are kept verbatim.
    olum = by_code["DF_OLUM_BEBEK_ANNEBABA_EGT_C"]
    assert olum.updated_at is None
    assert olum.category_path == [
        "Nüfus ve Demografi",
        "Hayati İstatistikler",
        "Ölüm ve Ölüm Nedeni İstatistikleri",
    ]
    assert olum.footnotes and olum.downloadable is False


def test_press_id_from_link_parses_the_last_dash_segment() -> None:
    link = (
        "https://data.tuik.gov.tr/Bulten/Index?p=Dış-Ticaret-İstatistikleri-Kasım-2025-53908&dil=1"
    )
    assert press_id_from_link(link) == "53908"


def test_press_id_from_link_without_a_p_parameter_is_none() -> None:
    assert press_id_from_link("https://data.tuik.gov.tr/Bulten/Index?dil=1") is None


def test_parse_calendar_keeps_only_tuik_and_reports_rows_without_id() -> None:
    payload = fixture_json("calendar2025.raw.json")
    types = parse_press_types(fixture_json("press_list.raw.json"))
    items, skipped = parse_calendar(
        payload, base_url="https://veriportali.tuik.gov.tr", press_types=types
    )
    ids = {item.external_id for item in items}
    assert "53908" in ids  # TÜİK row with a valid id
    assert "57888" not in ids  # SPK row filtered out
    assert skipped == 1  # the TÜİK row with a link that has no p parameter
    assert len(items) == 6
    first = next(item for item in items if item.external_id == "53908")
    assert first.doc_type == "Haber Bülteni"
    assert first.published_at == date(2025, 12, 31)
    assert first.year == 2025
    assert first.url == "https://veriportali.tuik.gov.tr/tr/press/53908"
    assert first.attributes["period"] == "Kasım 2025"


def test_parse_press_detail_text_attributes_and_links() -> None:
    payload = fixture_json("press_58290.raw.json")
    detail = parse_press_detail(payload, base_url="https://veriportali.tuik.gov.tr")
    assert detail.title == "Tüketici Fiyat Endeksi"
    assert detail.period == "Ağustos 2026"
    assert detail.press_date == date(2026, 9, 3)
    # HTML tags dropped, entities decoded, <br> turned into newlines.
    assert "<span" not in detail.content_text
    assert "&amp;" not in detail.content_text
    assert "&" in detail.content_text
    assert "entity" in detail.content_text
    assert "Tüketici Fiyat Endeksi, Ağustos 2026" in detail.content_text.splitlines()[0]
    assert len(detail.content_text) > 100
    # Statistical tables -> dataset links with code and version.
    assert len(detail.links) == 10
    assert detail.links[0].dataset_code == "DF_TUFE_SDMX_TT01"
    assert detail.links[0].dataset_version == "1.0"
    assert detail.links[0].relation == "statistical_table"
    assert detail.attributes["number"] == 58290
    assert detail.attributes["contact_email"] == "sektorelfiyat@tuik.gov.tr"
    assert "metadatas" not in detail.attributes
    assert detail.attributes["previous_presses"][0]["id"] == "58297"
    assert detail.attributes["reports"][0]["url"].startswith("https://veriportali.tuik.gov.tr/")
    assert detail.attributes["tables"][0]["url"].startswith("https://veriportali.tuik.gov.tr/")


def test_parse_press_detail_is_error_is_not_found() -> None:
    with pytest.raises(ConnectorError) as excinfo:
        parse_press_detail({"isError": True, "message": "Bülten bulunamadı"}, base_url="x")
    assert excinfo.value.kind == NOT_FOUND


def test_parse_statistical_table_url() -> None:
    url = "https://databrowser2.tuik.gov.tr/#/tr/tuik/categories/6/6_5/TR,DF_TUFE_SDMX_TT01,1.0"
    assert parse_statistical_table_url(url) == ("DF_TUFE_SDMX_TT01", "1.0")
    assert parse_statistical_table_url("not a url") is None


def test_html_to_text_collapses_blank_runs() -> None:
    text = html_to_text("<p>one</p><br><br><br><p>two</p><script>x</script><p>three</p>")
    assert text == "one\n\ntwo\n\nthree"
    assert "x" not in text


# --- SDMX-JSON series selection --------------------------------------------


def test_points_from_sdmx_json_selects_one_series() -> None:
    payload = fixture_json("tt01.raw.json")
    key = {"REF_AREA": "TR", "FREQ": "M", "COICOP_2018": "01", "INDICATOR": "F_TFE"}
    points = points_from_sdmx_json(payload, key)
    assert len(points) == 6
    assert points[0] == (date(2026, 3, 1), Decimal("127.59"))
    assert points[-1] == (date(2026, 8, 1), Decimal("134.31"))


def test_points_from_sdmx_json_distinguishes_by_hidden_dimension() -> None:
    payload = fixture_json("tt01.raw.json")
    key = {"REF_AREA": "TR", "FREQ": "M", "COICOP_2018": "02", "INDICATOR": "F_TFE"}
    points = points_from_sdmx_json(payload, key)
    assert points[0][1] == Decimal("123.16")


def test_points_from_sdmx_json_no_match_is_empty() -> None:
    payload = {
        "data": {
            "dataSets": [
                {"series": {"0:0": {"observations": {"0": ["1"]}}}},
                {"series": {"1:1": {"observations": {"0": ["2"]}}}},
            ],
            "structure": {
                "dimensions": {
                    "series": [
                        {
                            "id": "A",
                            "keyPosition": 0,
                            "values": [{"id": "a0"}, {"id": "a1"}],
                        },
                        {
                            "id": "B",
                            "keyPosition": 1,
                            "values": [{"id": "b0"}, {"id": "b1"}],
                        },
                    ],
                    "observation": [
                        {
                            "id": "TIME_PERIOD",
                            "values": [{"id": "2025", "start": "2025-01-01"}],
                        }
                    ],
                }
            },
        }
    }
    # A valid code combination with no series (a0 paired with b1).
    assert points_from_sdmx_json(payload, {"A": "a0", "B": "b1"}) == []


def test_points_from_sdmx_json_unknown_code_is_format_changed() -> None:
    payload = fixture_json("tt01.raw.json")
    with pytest.raises(ConnectorError) as excinfo:
        points_from_sdmx_json(payload, {"INDICATOR": "NOPE"})
    assert excinfo.value.kind == FORMAT_CHANGED


# --- client headers and error classification --------------------------------


def _client_with(handler, store=None) -> VeriPortaliClient:
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    return VeriPortaliClient(
        http_client=http_client,
        store=store,
        sleeper=lambda _: None,
        settings_obj=_settings(),
    )


def _settings():
    from app.config import Settings

    return Settings(
        tuik_veriportali_base_url="https://veriportali.example",
        tuik_veriportali_press_base_url="https://press.example",
        tuik_veriportali_user_agent=UA,
        tuik_veriportali_max_retries=1,
    )


def test_every_request_carries_the_browser_headers() -> None:
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.headers))
        return json_response(b"[]")

    client = _client_with(handler)
    client.dataflows()
    client.close()
    assert seen
    assert seen[0]["user-agent"] == UA
    assert seen[0]["x-requested-with"] == "XMLHttpRequest"


def test_calendar_request_does_not_send_xhr() -> None:
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.headers))
        return json_response(b'{"yayindaOlanlarList": []}')

    client = _client_with(handler)
    client.calendar(2025)
    client.close()
    assert "x-requested-with" not in seen[0]
    assert seen[0]["user-agent"] == UA


def test_no_user_agent_403_is_source_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, content="Erişim engellendi".encode(), headers={"content-type": "text/plain"}
        )

    client = _client_with(handler)
    with pytest.raises(ConnectorError) as excinfo:
        client.dataflows()
    assert excinfo.value.kind == SOURCE_ERROR
    assert "WAF blocked" in excinfo.value.message
    client.close()


def test_text_plain_404_is_format_changed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404, content=b"Sayfa bulunamad", headers={"content-type": "text/plain"}
        )

    client = _client_with(handler)
    with pytest.raises(ConnectorError) as excinfo:
        client.dataflows()
    assert excinfo.value.kind == FORMAT_CHANGED
    client.close()


def test_throttle_page_is_retried_then_throttled() -> None:
    calls = {"n": 0}
    html = b"<html>Y\xc3\xb6nlendiriliyor... 5 saniye sonra</html>"

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return json_response(html, "text/html")

    client = _client_with(handler)
    with pytest.raises(ConnectorError) as excinfo:
        client.dataflows()
    assert excinfo.value.kind == THROTTLED
    assert calls["n"] == 2  # one retry with the configured max_retries=1
    assert client.throttled is True
    client.close()


def test_dataflows_are_stored_raw() -> None:
    store = InMemoryObjectStore()
    payload = json.dumps(fixture_json("dataflows.raw.json")).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(payload)

    client = _client_with(handler, store=store)
    client.dataflows()
    client.close()
    assert any("veriportali-catalog" in key for key in store.objects)
    assert all(key.startswith("sources/tuik/") for key in store.objects)


# --- connector fallback order ----------------------------------------------


class _FakePortal:
    def __init__(self, *, downloadable: bool = True, result=None, error=None) -> None:
        self.downloadable = downloadable
        self.result = result
        self.error = error
        self.calls: list[tuple] = []
        self.versions: list[str | None] = []

    def is_downloadable(self, dataset_code: str, *, version=None) -> bool:
        self.versions.append(version)
        return self.downloadable

    def fetch_series(self, dataset_code, codes, *, order, start=date(2000, 1, 1), version=None):
        self.calls.append((dataset_code, codes, order, start))
        self.versions.append(version)
        if self.error is not None:
            raise self.error
        return self.result

    def close(self) -> None:
        pass


class _FakeNsiws:
    def __init__(self, *, result=None, error=None) -> None:
        self.result = result
        self.error = error
        self.calls = 0

    def fetch_series(self, dataset_code, codes, *, version, order=None, start=date(2000, 1, 1)):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result

    def close(self) -> None:
        pass


class _StubClient:
    max_concurrency = 2
    throttled = False

    def dataset_data(self, *args, **kwargs):
        raise ConnectorError(FORMAT_CHANGED, "no probe in tests")

    def dataset_partial_codelist(self, *args, **kwargs):
        raise ConnectorError(FORMAT_CHANGED, "no codelist in tests")

    def dataset_structure(self, *args, **kwargs):
        raise ConnectorError(FORMAT_CHANGED, "no structure in tests")

    def close(self) -> None:
        pass


def _info():
    from app.connectors.tuik.parsers import DataflowInfo

    return DataflowInfo(
        dataflow_id="DF_TUFE_SDMX_TT10",
        version="1.0",
        agency="TR",
        title="x",
        description=None,
        source_category=None,
    )


def _connector(*, nsiws, veriportali, boom_kind):
    connector = TuikConnector(client=_StubClient(), nsiws=nsiws, veriportali=veriportali)
    connector.resolve = lambda code: _info()  # type: ignore[method-assign]

    def bombe(*args, **kwargs):
        raise ConnectorError(boom_kind, "databrowser2 down")

    connector._fetch_databrowser2 = bombe  # type: ignore[method-assign]
    return connector


def test_fallback_chain_databrowser2_nsiws_veriportali(caplog) -> None:
    from app.connectors.base import FetchResult

    result = FetchResult("x", [(date(2025, 1, 1), Decimal("1"))], [], "veriportali")
    nsiws = _FakeNsiws(error=ConnectorError(SOURCE_ERROR, "nsiws down"))
    portal = _FakePortal(result=result)
    connector = _connector(nsiws=nsiws, veriportali=portal, boom_kind=SOURCE_ERROR)
    with caplog.at_level(logging.WARNING):
        fetched = connector.fetch_series(
            "DF_TUFE_SDMX_TT10", {"REF_AREA": "TR"}, order=["REF_AREA"]
        )
    assert fetched.channel == "veriportali"
    assert nsiws.calls == 1
    assert portal.calls
    assert "tuik nsiws failed, falling back to veriportali: DF_TUFE_SDMX_TT10" in caplog.text


def test_fallback_without_nsiws_goes_to_veriportali(caplog) -> None:
    from app.connectors.base import FetchResult

    result = FetchResult("x", [(date(2025, 1, 1), Decimal("1"))], [], "veriportali")
    portal = _FakePortal(result=result)
    connector = _connector(nsiws=None, veriportali=portal, boom_kind=SOURCE_ERROR)
    with caplog.at_level(logging.WARNING):
        fetched = connector.fetch_series(
            "DF_TUFE_SDMX_TT10", {"REF_AREA": "TR"}, order=["REF_AREA"]
        )
    assert fetched.channel == "veriportali"
    assert "tuik nsiws not configured, falling back to veriportali" in caplog.text


def test_not_downloadable_reraises_previous_error() -> None:
    nsiws = _FakeNsiws(error=ConnectorError(SOURCE_ERROR, "nsiws down"))
    portal = _FakePortal(downloadable=False)
    connector = _connector(nsiws=nsiws, veriportali=portal, boom_kind=SOURCE_ERROR)
    with pytest.raises(ConnectorError) as excinfo:
        connector.fetch_series("DF_TUFE_SDMX_TT10", {"REF_AREA": "TR"}, order=["REF_AREA"])
    assert excinfo.value.kind == SOURCE_ERROR
    assert portal.calls == []


def test_force_veriportali_channel_bypasses_databrowser2() -> None:
    from app.connectors.base import FetchResult

    result = FetchResult("x", [(date(2025, 1, 1), Decimal("1"))], [], "veriportali")
    portal = _FakePortal(result=result)
    connector = _connector(nsiws=None, veriportali=portal, boom_kind=SOURCE_ERROR)
    fetched = connector.fetch_series(
        "DF_TUFE_SDMX_TT10", {"REF_AREA": "TR"}, order=["REF_AREA"], channel="veriportali"
    )
    assert fetched.channel == "veriportali"
    assert portal.calls


# --- CLI --------------------------------------------------------------------


def test_veriportali_parser_flags() -> None:
    args = build_parser().parse_args(["veriportali-catalog", "--dry-run"])
    assert args.command == "veriportali-catalog"
    assert args.dry_run is True

    args = build_parser().parse_args(
        ["press-catalog", "--dry-run", "--from-year", "2025", "--to-year", "2025"]
    )
    assert (args.from_year, args.to_year) == (2025, 2025)

    args = build_parser().parse_args(["press-fetch", "--id", "58290", "--dry-run"])
    assert args.press_id == "58290"

    args = build_parser().parse_args(
        ["fetch", "--dataset", "DF_X", "--code", "REF_AREA=TR", "--channel", "veriportali"]
    )
    assert args.channel == "veriportali"


def test_veriportali_routes_to_run(monkeypatch) -> None:
    calls: list[str] = []

    def fake_run(args, dry_run):
        calls.append(args.command)
        return 0

    monkeypatch.setattr(tuik_cli, "_run_veriportali", fake_run)
    assert tuik_cli.main(["veriportali-catalog", "--dry-run"]) == 0
    assert tuik_cli.main(["press-catalog", "--dry-run"]) == 0
    assert tuik_cli.main(["press-fetch", "--id", "1", "--dry-run"]) == 0
    assert calls == ["veriportali-catalog", "press-catalog", "press-fetch"]


def test_partial_press_year_range_is_never_complete() -> None:
    today = date(2026, 10, 2)
    assert covers_all_press_years(2005, 2026, today=today)
    assert not covers_all_press_years(2025, 2026, today=today)
    assert not covers_all_press_years(2005, 2025, today=today)
    partial = CalendarCrawl(items=[], covers_all_years=False)
    assert not partial.complete
    assert CalendarCrawl(items=[]).complete
    assert not CalendarCrawl(items=[], failed_years=[2010]).complete


def test_select_dataflow_prefers_catalogued_version_then_highest() -> None:
    records = parse_dataflow_records(
        [
            {"id": "DF_X+V1.1", "name": "x", "version": "1.1", "downloadable": True},
            {"id": "DF_X+V1.0", "name": "x", "version": "1.0", "downloadable": True},
            {"id": "DF_X+V1.10", "name": "x", "version": "1.10", "downloadable": False},
        ]
    )
    assert select_dataflow(records, version="1.0").portal_id == "DF_X+V1.0"
    assert select_dataflow(records).portal_id == "DF_X+V1.10"
    assert select_dataflow(records, version="9.9").portal_id == "DF_X+V1.10"
    assert select_dataflow([]) is None
