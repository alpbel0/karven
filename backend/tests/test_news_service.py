"""Unit tests for the news service write paths (Task 1.7).

Fakes only: no network, no real database. The real PostgreSQL path (``ON
CONFLICT``, ``SELECT ... FOR UPDATE``) is covered by
``tests/integration/test_news_flow.py``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from app.connectors.base import InMemoryObjectStore
from app.news import extract as extract_module
from app.news import service
from app.news.feeds import NEWS_FEEDS, NEWS_SOURCES, parse_feed
from app.news.service import (
    NewsPollError,
    fetch_article,
    poll_feeds,
    requeue_pending,
)
from app.news.tasks import run_poll
from tests.news_fakes import (
    ClientScript,
    FakeClient,
    FakeResponse,
    FakeSession,
    FakeStore,
    connect_timeout,
    make_article,
    raise_for,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "news"
# The fixtures' newest pubDate (after CNN's UTC+3 correction) is
# 2026-10-03 19:56 UTC, so a first poll at this time is genuinely after every
# item (as a real poll always is).
NOW = datetime(2026, 10, 4, 0, 0, tzinfo=UTC)


def _fixture(source: str) -> bytes:
    return (FIXTURES / f"{source}.feed.raw.xml").read_bytes()


def _article_html(source: str) -> bytes:
    return (FIXTURES / f"{source}.article.raw.html").read_bytes()


def _healthy_feeds() -> dict[str, bytes]:
    return {NEWS_FEEDS[source]: _fixture(source) for source in NEWS_SOURCES}


def _healthy_client() -> FakeClient:
    routes = _healthy_feeds()
    return FakeClient(lambda url: FakeResponse(200, routes[url]))


def _newest_ids(items, limit: int) -> set[str]:
    ordered = sorted(
        items,
        key=lambda item: item.published_at or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    return {item.external_id for item in ordered[:limit]}


def test_first_run_takes_only_the_four_newest_per_source() -> None:
    store = FakeStore()
    report = poll_feeds(FakeSession(store), _healthy_client(), now=NOW, first_run_limit=4)

    assert report.failed_sources == []
    assert len(report.new_ids) == 4 * len(NEWS_SOURCES)
    for source in NEWS_SOURCES:
        result = next(item for item in report.sources if item.source == source)
        items = parse_feed(_fixture(source), source)
        assert result.fetched_items == len(items)
        assert result.inserted == 4
        assert result.error is None
        inserted = {row.external_id for row in store.articles if row.source == source}
        assert inserted == _newest_ids(items, 4)


def test_second_poll_of_unchanged_feeds_inserts_nothing() -> None:
    store = FakeStore()
    session = FakeSession(store)
    first = poll_feeds(session, _healthy_client(), now=NOW, first_run_limit=4)
    assert len(first.new_ids) == 20
    assert len(store.articles) == 20

    # Same feed bytes 15 minutes later: only new items are taken, and there are
    # none, so the old backlog the first-run limit skipped is never inserted.
    later = NOW + timedelta(minutes=15)
    second = poll_feeds(session, _healthy_client(), now=later, first_run_limit=4)
    assert second.new_ids == []
    assert len(store.articles) == 20


def test_later_poll_takes_only_items_newer_than_the_baseline() -> None:
    store = FakeStore()
    session = FakeSession(store)
    poll_feeds(session, _healthy_client(), now=NOW, first_run_limit=4)

    newer = (
        b"<item><title>Yeni test haberi</title>"
        b"<link>https://www.sabah.com.tr/ekonomi/yeni-test-haberi-999</link>"
        b"<description>d</description>"
        b"<pubDate>Sun, 04 Oct 2026 09:00:00 +0300</pubDate></item>"
    )
    older = (
        b"<item><title>Eski haber</title>"
        b"<link>https://www.sabah.com.tr/ekonomi/eski-haber-000</link>"
        b"<description>d</description>"
        b"<pubDate>Fri, 25 Sep 2026 08:00:00 +0300</pubDate></item>"
    )
    modified = _fixture("sabah").replace(b"</channel>", newer + older + b"</channel>")
    routes = _healthy_feeds()
    routes[NEWS_FEEDS["sabah"]] = modified
    client = FakeClient(lambda url: FakeResponse(200, routes[url]))

    later = NOW + timedelta(minutes=15)
    second = poll_feeds(session, client, now=later, first_run_limit=4)
    assert len(second.new_ids) == 1
    inserted = next(row for row in store.articles if row.id == second.new_ids[0])
    assert inserted.external_id == "https://www.sabah.com.tr/ekonomi/yeni-test-haberi-999"
    assert not any(
        row.external_id == "https://www.sabah.com.tr/ekonomi/eski-haber-000"
        for row in store.articles
    )


def test_undated_item_after_first_poll_is_taken_and_warned(caplog) -> None:
    store = FakeStore()
    session = FakeSession(store)
    poll_feeds(session, _healthy_client(), now=NOW, first_run_limit=4)

    undated = (
        b"<item><title>Tarihsiz haber</title>"
        b"<link>https://www.sabah.com.tr/ekonomi/tarihsiz-haber-000</link>"
        b"<description>d</description></item>"
    )
    modified = _fixture("sabah").replace(b"</channel>", undated + b"</channel>")
    routes = _healthy_feeds()
    routes[NEWS_FEEDS["sabah"]] = modified
    client = FakeClient(lambda url: FakeResponse(200, routes[url]))

    later = NOW + timedelta(minutes=15)
    with caplog.at_level(logging.WARNING, logger="app.news.service"):
        second = poll_feeds(session, client, now=later, first_run_limit=4)

    assert len(second.new_ids) == 1
    inserted = next(row for row in store.articles if row.id == second.new_ids[0])
    assert inserted.external_id == "https://www.sabah.com.tr/ekonomi/tarihsiz-haber-000"
    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and "undated items taken" in record.message
    ]
    assert len(warnings) == 1
    assert "news source sabah: 1 undated items taken" in warnings[0].message


def test_failing_feed_is_reported_while_others_are_inserted(monkeypatch) -> None:
    monkeypatch.setattr(service, "_FETCH_BACKOFF_S", 0.0)
    routes = _healthy_feeds()

    def handler(url: str) -> FakeResponse:
        if url == NEWS_FEEDS["cnnturk"]:
            raise httpx.ConnectError("cnn is down")
        return FakeResponse(200, routes[url])

    store = FakeStore()
    report = poll_feeds(FakeSession(store), FakeClient(handler), now=NOW, first_run_limit=4)

    cnnturk = next(item for item in report.sources if item.source == "cnnturk")
    assert cnnturk.error is not None
    assert cnnturk.inserted == 0
    assert report.failed_sources == ["cnnturk"]
    assert len(report.new_ids) == 16
    assert {row.source for row in store.articles} == set(NEWS_SOURCES) - {"cnnturk"}


def test_run_poll_raises_after_enqueuing_the_healthy_rows(monkeypatch) -> None:
    monkeypatch.setattr(service, "_FETCH_BACKOFF_S", 0.0)
    routes = _healthy_feeds()
    store = FakeStore()

    def handler(url: str) -> FakeResponse:
        if url == NEWS_FEEDS["haberturk"]:
            raise httpx.ConnectError("haberturk is down")
        return FakeResponse(200, routes[url])

    def factory() -> FakeSession:
        return FakeSession(store)

    enqueued: list[int] = []
    with pytest.raises(NewsPollError) as excinfo:
        run_poll(
            factory,
            FakeClient(handler),
            now=NOW,
            first_run_limit=4,
            requeue_after=timedelta(seconds=600),
            enqueue=enqueued.append,
        )
    assert excinfo.value.sources == ["haberturk"]
    assert len(enqueued) == 16
    assert store.commits >= 1


def test_fetch_article_ok_stores_text_and_raw() -> None:
    store = FakeStore()
    article = make_article(
        store, source="sabah", external_id="https://example.com/ok", url="https://example.com/ok"
    )
    raw = InMemoryObjectStore()
    client = FakeClient(lambda url: FakeResponse(200, _article_html("sabah")))

    result = fetch_article(
        FakeSession(store),
        client,
        raw,
        article.id,
        now=NOW,
        sleeper=lambda _seconds: None,
        min_text_chars=200,
    )

    assert result.outcome == "ok"
    assert article.text_status == "ok"
    assert article.content_text is not None and len(article.content_text) >= 200
    assert article.text_error is None
    assert article.fetch_attempts == 1
    assert article.fetched_at == NOW
    assert article.raw_object_key is not None
    assert article.raw_object_key in raw.objects


def test_fetch_article_no_text_records_reason() -> None:
    store = FakeStore()
    article = make_article(store, external_id="https://example.com/tiny")
    raw = InMemoryObjectStore()
    client = FakeClient(lambda url: FakeResponse(200, b"<html><body><p>Merhaba</p></body></html>"))

    result = fetch_article(FakeSession(store), client, raw, article.id, now=NOW, min_text_chars=200)

    assert result.outcome == "no_text"
    assert article.text_status == "no_text"
    assert article.content_text is None
    assert article.text_error is not None
    assert "chars < 200" in article.text_error or article.text_error == "no text extracted"
    assert article.fetch_attempts == 1
    assert article.raw_object_key in raw.objects


def test_fetch_article_extraction_error_is_stored(monkeypatch, caplog) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("trafilatura exploded")

    monkeypatch.setattr(extract_module.trafilatura, "extract", boom)
    store = FakeStore()
    article = make_article(store, external_id="https://example.com/broken")
    client = FakeClient(lambda url: FakeResponse(200, b"<html><body><p>x</p></body></html>"))

    with caplog.at_level(logging.WARNING, logger="app.news.extract"):
        result = fetch_article(
            FakeSession(store), client, InMemoryObjectStore(), article.id, now=NOW
        )

    assert result.outcome == "no_text"
    assert article.text_status == "no_text"
    assert article.text_error == "extraction error: RuntimeError: trafilatura exploded"
    assert any("news text extraction failed" in record.message for record in caplog.records)


def test_fetch_article_http_404_is_final() -> None:
    store = FakeStore()
    article = make_article(store, external_id="https://example.com/gone")
    client = FakeClient(lambda url: FakeResponse(404, b"not found"))

    result = fetch_article(FakeSession(store), client, InMemoryObjectStore(), article.id, now=NOW)

    assert result.outcome == "failed"
    assert article.text_status == "failed"
    assert article.text_error == "HTTP 404"
    assert article.fetch_attempts == 1
    assert len(client.calls) == 1


def test_fetch_article_timeout_is_retried_then_failed() -> None:
    store = FakeStore()
    article = make_article(store, external_id="https://example.com/slow")
    client = FakeClient(raise_for(connect_timeout()))

    result = fetch_article(
        FakeSession(store),
        client,
        InMemoryObjectStore(),
        article.id,
        now=NOW,
        sleeper=lambda _seconds: None,
        max_attempts=3,
    )

    assert result.outcome == "failed"
    assert article.text_status == "failed"
    assert article.fetch_attempts == 3
    assert len(client.calls) == 3
    assert article.text_error is not None and "ConnectTimeout" in article.text_error


def test_fetch_article_503_is_retried_then_succeeds() -> None:
    store = FakeStore()
    article = make_article(store, external_id="https://example.com/flaky")
    script = ClientScript(
        {
            "https://example.com/flaky": [
                FakeResponse(503),
                FakeResponse(503),
                FakeResponse(200, _article_html("sabah")),
            ]
        }
    )
    client = FakeClient(script.handler)

    result = fetch_article(
        FakeSession(store),
        client,
        InMemoryObjectStore(),
        article.id,
        now=NOW,
        sleeper=lambda _seconds: None,
        max_attempts=3,
        min_text_chars=200,
    )

    assert result.outcome == "ok"
    assert article.text_status == "ok"
    assert article.fetch_attempts == 3


def test_fetch_article_skips_when_not_pending() -> None:
    store = FakeStore()
    article = make_article(
        store,
        text_status="ok",
        content_text="already fetched",
        external_id="https://example.com/done",
    )
    calls: list[str] = []

    def handler(url: str) -> FakeResponse:
        calls.append(url)
        return FakeResponse(200, b"")

    result = fetch_article(
        FakeSession(store), FakeClient(handler), InMemoryObjectStore(), article.id, now=NOW
    )

    assert result.outcome == "skipped"
    assert result.status == "ok"
    assert calls == []
    assert article.fetch_attempts == 0
    assert article.fetched_at is None


def test_fetch_article_storage_failure_does_not_break_text() -> None:
    class FailingStore(InMemoryObjectStore):
        def put(self, key, payload, *, content_type="application/octet-stream"):
            raise RuntimeError("minio is down")

    store = FakeStore()
    article = make_article(store, external_id="https://example.com/raw")
    client = FakeClient(lambda url: FakeResponse(200, _article_html("haberturk")))

    result = fetch_article(
        FakeSession(store),
        client,
        FailingStore(),
        article.id,
        now=NOW,
        min_text_chars=200,
    )

    assert result.outcome == "ok"
    assert article.text_status == "ok"
    assert article.raw_object_key is None


def test_requeue_pending_selects_only_old_unfetched_pending() -> None:
    store = FakeStore()
    old = NOW - timedelta(seconds=601)
    recent = NOW - timedelta(seconds=100)
    requeued = make_article(store, external_id="old", first_seen_at=old)
    make_article(store, external_id="recent", first_seen_at=recent)
    make_article(store, external_id="attempted", first_seen_at=old, fetched_at=NOW)
    make_article(store, external_id="ok", text_status="ok", first_seen_at=old)

    ids = requeue_pending(FakeSession(store), NOW, timedelta(seconds=600))

    assert ids == [requeued.id]
