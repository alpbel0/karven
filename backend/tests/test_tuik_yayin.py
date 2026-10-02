"""Unit tests for the TÜİK Biruni publication connector (no network; fixtures)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from app.connectors.base import FORMAT_CHANGED, ConnectorError, InMemoryObjectStore
from app.connectors.tuik import __main__ as tuik_cli
from app.connectors.tuik.__main__ import build_parser
from app.connectors.tuik.yayin import (
    YayinClient,
    YayinItem,
    _plan_document_changes,
    crawl_yayin,
    parse_desktop_id,
    parse_items,
    parse_pager,
)

FIXTURES = Path(__file__).parent / "fixtures" / "tuik" / "yayin"
INDEX_HTML = "index.raw.html"
PAGE2_AU = "page2.au.raw.txt"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text("utf-8")


def json_response(content: bytes, content_type: str) -> httpx.Response:
    return httpx.Response(200, content=content, headers={"content-type": content_type})


# --- pager detection --------------------------------------------------------


def test_parse_pager_picks_the_block_after_listyayinlar() -> None:
    text = (
        "['zul.mesh.Paging','BEFORE',{$onPaging:true,totalSize:3},[]],"
        "id:'listYayinlar',"
        "['zul.mesh.Paging','AFTER',{$onPaging:true,totalSize:634,pageSize:9,pageCount:71},[]]"
    )
    pager = parse_pager(text)
    assert pager.uuid == "AFTER"
    assert (pager.total_size, pager.page_size, pager.page_count) == (634, 9, 71)


def test_parse_pager_on_captured_bootstrap() -> None:
    pager = parse_pager(fixture_text(INDEX_HTML))
    assert pager.total_size == 634
    assert (pager.page_size, pager.page_count) == (9, 71)
    # The page has an earlier Paging component without totalSize; the correct
    # one is the first Paging after the catalogue listbox.
    assert pager.uuid == "n5zPb1"


def test_parse_pager_rejects_a_page_without_a_listbox() -> None:
    with pytest.raises(ConnectorError) as excinfo:
        parse_pager("['zul.mesh.Paging','X',{}]")
    assert excinfo.value.kind == FORMAT_CHANGED


def test_parse_desktop_id() -> None:
    assert parse_desktop_id(fixture_text(INDEX_HTML)) == "z_dbj"


# --- item parsing -----------------------------------------------------------


def test_parse_items_reads_yayin_no_labels_and_url() -> None:
    items = parse_items(fixture_text(PAGE2_AU), page=2)
    assert len(items) == 9
    first = items[0]
    assert first.external_id == "718"
    assert first.subject == "İstihdam, İşsizlik ve Ücret"
    assert first.title == "4A Aktif Sigortalı İdari Kayıt Mikro Veri Seti, 2025"
    assert first.doc_type == "Mikro Veri Seti"
    assert first.year == 2025
    assert first.url == (
        "https://biruni.tuik.gov.tr/yayin/views/visitorPages/yayinGoruntuleme.zul?yayin_no=718"
    )
    assert first.attributes["labels"][2] == "Mikro Veri Seti"
    assert first.attributes["page"] == 2


def test_parse_items_missing_year_is_none() -> None:
    by_id = {item.external_id: item for item in parse_items(fixture_text(INDEX_HTML), page=1)}
    assert by_id["723"].year is None
    assert by_id["725"].year == 2025


def test_parse_items_strips_jsessionid_from_the_url() -> None:
    by_id = {item.external_id: item for item in parse_items(fixture_text(INDEX_HTML), page=1)}
    assert ";jsessionid" not in by_id["725"].url


# --- pure upsert planning ---------------------------------------------------


@dataclass
class _Existing:
    id: int
    external_id: str
    doc_type: str
    title: str
    subject: str | None
    year: int | None
    published_at: object
    url: str
    language: str
    attributes: dict
    first_seen_at: datetime
    institution_id: int | None


def _item(external_id: str, *, year: int | None = 2025) -> YayinItem:
    return YayinItem(
        external_id=external_id,
        title=f"Title {external_id}",
        subject="Subject",
        doc_type="Rapor",
        year=year,
        url=f"https://example.test/{external_id}",
        attributes={"labels": ["Subject", f"Title {external_id}", "Rapor"]},
    )


def _existing(external_id: str, *, attributes: dict | None = None) -> _Existing:
    return _Existing(
        id=abs(hash(external_id)) % 10_000,
        external_id=external_id,
        doc_type="Rapor",
        title=f"Title {external_id}",
        subject="Subject",
        year=2025,
        published_at=None,
        url=f"https://example.test/{external_id}",
        language="tr",
        attributes=attributes
        if attributes is not None
        else {"labels": ["Subject", f"Title {external_id}", "Rapor"]},
        first_seen_at=datetime(2026, 1, 1, tzinfo=UTC),
        institution_id=None,
    )


NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def test_plan_document_changes_is_idempotent() -> None:
    incoming = [_item("1"), _item("2")]
    rows, inserted, updated, unchanged, removed = _plan_document_changes(
        incoming, [_existing("1")], institution_id=None, source="s", now=NOW, complete=True
    )
    assert (inserted, updated, unchanged, removed) == (1, 0, 1, [])
    assert {row["external_id"] for row in rows} == {"1", "2"}
    assert all(row["last_seen_at"] == NOW for row in rows)


def test_plan_document_changes_detects_a_title_change() -> None:
    changed = replace(_item("1"), title="Renamed")
    rows, inserted, updated, unchanged, removed = _plan_document_changes(
        [changed], [_existing("1")], institution_id=None, source="s", now=NOW, complete=True
    )
    assert (inserted, updated, unchanged, removed) == (0, 1, 0, [])
    assert rows[0]["title"] == "Renamed"


def test_plan_document_changes_marks_removed_only_when_complete() -> None:
    existing = [_existing("9")]
    partial = _plan_document_changes(
        [], existing, institution_id=None, source="s", now=NOW, complete=False
    )
    assert partial[4] == []

    complete = _plan_document_changes(
        [], existing, institution_id=None, source="s", now=NOW, complete=True
    )
    assert [row.external_id for row in complete[4]] == ["9"]


def test_plan_document_changes_clears_a_removed_marker() -> None:
    existing = _existing("1", attributes={**_existing("1").attributes, "removed_at": "old"})
    rows, inserted, updated, unchanged, removed = _plan_document_changes(
        [_item("1")], [existing], institution_id=None, source="s", now=NOW, complete=True
    )
    assert (inserted, updated, unchanged, removed) == (0, 1, 0, [])
    assert "removed_at" not in rows[0]["attributes"]


# --- client + crawl ---------------------------------------------------------


def _fixture_handler(requests: list[str]):
    html = (FIXTURES / INDEX_HTML).read_bytes()
    au = (FIXTURES / PAGE2_AU).read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.method == "GET":
            return json_response(html, "text/html")
        return json_response(au, "text/plain;charset=UTF-8")

    return handler


def test_crawl_yayin_bootstraps_pages_and_reports_raw_keys() -> None:
    requests: list[str] = []
    sleeps: list[float] = []
    store = InMemoryObjectStore()
    http_client = httpx.Client(transport=httpx.MockTransport(_fixture_handler(requests)))
    client = YayinClient(
        http_client=http_client, store=store, sleeper=sleeps.append, clock=lambda: 0.0
    )
    crawl = crawl_yayin(client, max_pages=2)

    assert len(requests) == 2
    assert crawl.pages == 2
    assert len(crawl.items) == 18
    assert (crawl.total_size, crawl.page_count) == (634, 71)
    assert crawl.complete is False
    assert all(key.startswith("sources/tuik/") for key in crawl.raw_object_keys)
    assert any("/yayin/yayin-index-" in key for key in store.objects)
    assert any("/yayin/yayin-page-002-" in key for key in store.objects)
    assert len(sleeps) >= 1
    client.close()


def test_page_requests_increment_zk_sid() -> None:
    # ZK's AU protocol is sequence-numbered; reusing the seed makes the server
    # answer the previous page again (found live 2026-10-01).
    html = (FIXTURES / INDEX_HTML).read_bytes()
    au = (FIXTURES / PAGE2_AU).read_bytes()
    sids: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            sids.append(request.headers.get("ZK-SID"))
            return json_response(au, "text/plain;charset=UTF-8")
        return json_response(html, "text/html")

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = YayinClient(http_client=http_client, store=None, sleeper=lambda _: None)
    crawl_yayin(client, max_pages=3)
    assert sids == ["1", "2"]
    client.close()


def test_client_pauses_between_requests() -> None:
    requests: list[str] = []
    sleeps: list[float] = []
    http_client = httpx.Client(transport=httpx.MockTransport(_fixture_handler(requests)))
    client = YayinClient(http_client=http_client, store=None, sleeper=sleeps.append)
    crawl_yayin(client, max_pages=3)
    assert len(requests) == 3
    assert all(0 < wait <= client._pause_s + 0.01 for wait in sleeps)
    client.close()


# --- CLI --------------------------------------------------------------------


def test_yayin_parser_flags() -> None:
    args = build_parser().parse_args(["yayin", "--dry-run", "--max-pages", "3"])
    assert args.command == "yayin"
    assert args.dry_run is True
    assert args.max_pages == 3


def test_yayin_routes_to_run_yayin(monkeypatch) -> None:
    calls: list[str] = []

    def fake_run(args, dry_run):
        calls.append(args.command)
        return 0

    monkeypatch.setattr(tuik_cli, "_run_yayin", fake_run)
    assert tuik_cli.main(["yayin", "--dry-run"]) == 0
    assert calls == ["yayin"]


def test_listing_position_is_not_a_content_change() -> None:
    from app.connectors.tuik.yayin import _content_attributes

    before = {"labels": ["a", "b", "c"], "href": "x?yayin_no=1", "page": 3, "absolute_index": 21}
    after = {"labels": ["a", "b", "c"], "href": "x?yayin_no=1", "page": 4, "absolute_index": 30}
    assert _content_attributes(before) == _content_attributes(after)
    assert _content_attributes({**after, "labels": ["a", "b", "d"]}) != _content_attributes(before)


def test_session_id_is_stripped_from_links() -> None:
    from app.connectors.tuik.yayin import _JSESSIONID

    href = "/yayin/views/visitorPages/yayinGoruntuleme.zul;jsessionid=HQDx-Wf?yayin_no=727"
    assert (
        _JSESSIONID.sub("", href) == "/yayin/views/visitorPages/yayinGoruntuleme.zul?yayin_no=727"
    )
