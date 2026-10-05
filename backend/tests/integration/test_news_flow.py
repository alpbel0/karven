"""Integration tests for the real news intake path (Task 1.7).

Real PostgreSQL only; the HTTP layer is a fake, so no network is touched. Rows
in ``news_articles`` are identified by unique ``external_id`` values so tests
never collide with each other or with earlier runs. The migration round-trip
downgrades 0017 and re-upgrades to head.

Run with ``uv run python scripts/integration.py``; the session fixture refuses
to run against anything but the isolated ``karven-test`` stack.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.data.models import NewsArticle
from app.db.session import SessionLocal
from app.migrator.postgres import ALEMBIC_INI
from app.news import service
from app.news.feeds import NEWS_FEEDS
from app.news.service import fetch_article, poll_feeds
from tests.news_fakes import FakeClient, FakeResponse

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "news"
NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)

#: Sources these tests poll directly; each test must own them to be a real
#: "first poll" (the first-run rule and the ``MIN(first_seen_at)`` baseline both
#: depend on the source having no rows).
OWNED_SOURCES = ("sabah",)


def _delete_owned_sources() -> None:
    with SessionLocal() as session:
        session.execute(sa.delete(NewsArticle).where(NewsArticle.source.in_(OWNED_SOURCES)))
        session.commit()


@pytest.fixture(autouse=True)
def _clean_owned_sources() -> None:
    """Delete every row of the sources these tests own, before and after.

    ``news_articles`` may not be deleted through the app, but the isolated test
    DB is disposable: wiping the source makes the first-run / baseline behaviour
    deterministic regardless of which tests ran earlier in the module.
    """
    _delete_owned_sources()
    yield
    _delete_owned_sources()


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


def _feed(links: list[str]) -> bytes:
    items = "".join(
        "<item>"
        f"<title>T {index}</title><link>{link}</link>"
        f"<description>summary {index}</description>"
        f"<pubDate>Sat, 03 Oct 2026 1{index}:00:00 +0300</pubDate>"
        "</item>"
        for index, link in enumerate(links)
    )
    body = (
        f'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>{items}</channel></rss>'
    )
    return body.encode()


def _article_html() -> bytes:
    return (FIXTURES / "sabah.article.raw.html").read_bytes()


def _single_source(monkeypatch, feed: bytes, article_urls: set[str]) -> FakeClient:
    """Restrict polling to one source and route its feed/articles to fakes."""
    monkeypatch.setattr(service, "NEWS_FEEDS", {"sabah": NEWS_FEEDS["sabah"]})
    monkeypatch.setattr(service, "_FETCH_BACKOFF_S", 0.0)

    def handler(url: str) -> FakeResponse:
        if url == NEWS_FEEDS["sabah"]:
            return FakeResponse(200, feed)
        if url in article_urls:
            return FakeResponse(200, _article_html())
        raise AssertionError(f"unexpected URL {url}")

    return FakeClient(handler)


def test_poll_then_repoll_then_fetch_and_read_back(monkeypatch) -> None:
    link_a = f"https://example.com/{_unique('news-a')}"
    link_b = f"https://example.com/{_unique('news-b')}"
    feed = _feed([link_a, link_b])
    client = _single_source(monkeypatch, feed, {link_a, link_b})

    with SessionLocal() as session:
        first = poll_feeds(session, client, now=NOW, first_run_limit=4)
        session.commit()
    assert len(first.new_ids) == 2
    article_id = first.new_ids[0]

    with SessionLocal() as session:
        second = poll_feeds(session, client, now=NOW, first_run_limit=4)
        session.commit()
    assert second.new_ids == []

    with SessionLocal() as session:
        result = fetch_article(session, client, None, article_id, now=NOW, min_text_chars=200)
        session.commit()
    assert result.outcome == "ok"

    with SessionLocal() as session:
        row = session.get(NewsArticle, article_id)
        assert row is not None
        assert row.text_status == "ok"
        assert row.content_text is not None and len(row.content_text) >= 200
        assert row.fetched_at is not None
        assert row.fetch_attempts == 1


def test_second_poll_takes_only_genuinely_new_items(monkeypatch) -> None:
    link_a = f"https://example.com/{_unique('baseline-a')}"
    link_b = f"https://example.com/{_unique('baseline-b')}"
    feed = _feed([link_a, link_b])
    client = _single_source(monkeypatch, feed, {link_a, link_b})

    with SessionLocal() as session:
        first = poll_feeds(session, client, now=NOW, first_run_limit=4)
        session.commit()
    assert len(first.new_ids) == 2

    # The baseline is the source's first-poll time, read back from the DB.
    with SessionLocal() as session:
        baseline = session.scalar(
            sa.select(sa.func.min(NewsArticle.first_seen_at)).where(
                NewsArticle.source == "sabah",
                NewsArticle.external_id.in_([link_a, link_b]),
            )
        )
    assert baseline is not None and baseline == NOW

    # Same feed, later poll: the two known items are excluded and their
    # pubDates are before the baseline, so nothing is inserted.
    later = NOW + timedelta(minutes=15)
    with SessionLocal() as session:
        second = poll_feeds(session, client, now=later, first_run_limit=4)
        session.commit()
    assert second.new_ids == []

    # One genuinely new item (published after the baseline) is inserted alone.
    newer_link = f"https://example.com/{_unique('baseline-new')}"
    newer_item = (
        "<item>"
        f"<title>Newer</title><link>{newer_link}</link>"
        "<description>d</description>"
        f"<pubDate>Sun, 04 Oct 2026 09:00:00 +0300</pubDate>"
        "</item>"
    )
    updated = feed.replace(b"</channel>", newer_item.encode() + b"</channel>")
    client2 = _single_source(monkeypatch, updated, {newer_link})
    with SessionLocal() as session:
        third = poll_feeds(session, client2, now=later, first_run_limit=4)
        session.commit()
    assert len(third.new_ids) == 1
    with SessionLocal() as session:
        row = session.get(NewsArticle, third.new_ids[0])
        assert row is not None and row.external_id == newer_link


def test_duplicate_source_external_id_is_rejected() -> None:
    external_id = f"https://example.com/{_unique('dup')}"
    with SessionLocal() as session:
        session.add(
            NewsArticle(
                source="sabah",
                external_id=external_id,
                url=external_id,
                title="first",
                first_seen_at=NOW,
            )
        )
        session.commit()

    with SessionLocal() as session:
        session.add(
            NewsArticle(
                source="sabah",
                external_id=external_id,
                url=external_id,
                title="duplicate",
                first_seen_at=NOW,
            )
        )
        with pytest.raises(sa.exc.IntegrityError):
            session.flush()
        session.rollback()


def test_fetch_article_uses_populate_existing_against_a_stale_read() -> None:
    external_id = f"https://example.com/{_unique('stale')}"
    with SessionLocal() as session:
        article = NewsArticle(
            source="sabah",
            external_id=external_id,
            url=external_id,
            title="stale",
            text_status="pending",
            first_seen_at=NOW,
        )
        session.add(article)
        session.commit()
        article_id = article.id

    def handler(_url: str) -> FakeResponse:
        raise AssertionError("must not fetch a non-pending article")

    client = FakeClient(handler)

    # Session A caches the row as pending; session B flips it to no_text first.
    with SessionLocal() as session_a:
        cached = session_a.get(NewsArticle, article_id)
        assert cached is not None and cached.text_status == "pending"
        with SessionLocal() as session_b:
            other = session_b.get(NewsArticle, article_id)
            other.text_status = "no_text"
            other.text_error = "flipped by another worker"
            session_b.commit()
        result = fetch_article(session_a, client, None, article_id, now=NOW)
        assert result.outcome == "skipped"
        assert result.status == "no_text"
    assert client.calls == []


def test_migration_0017_downgrade_and_upgrade_round_trip() -> None:
    config = Config(str(ALEMBIC_INI))
    script = ScriptDirectory.from_config(config)
    head = script.get_current_head()
    # The revision right below head (0016 today; read, not hard-coded, so a
    # later migration on top of 0017 keeps this test valid).
    parent = script.get_revision(head).down_revision
    command.downgrade(config, parent)
    try:
        with SessionLocal() as session:
            assert not sa.inspect(session.get_bind()).has_table("news_articles")
    finally:
        command.upgrade(config, "head")
    with SessionLocal() as session:
        assert sa.inspect(session.get_bind()).has_table("news_articles")
