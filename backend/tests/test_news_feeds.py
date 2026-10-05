"""Unit tests for the pure RSS feed parser (Task 1.7).

Every test parses a REAL feed fixture saved from the live sources on 2026-10-03
(no network). No module under test touches the database or MinIO here.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import UTC, timedelta
from pathlib import Path

import pytest

from app.news.feeds import NEWS_FEEDS, FeedError, parse_feed, parse_pub_date, strip_html

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "news"

#: First item's ``pubDate`` per source, converted to UTC (from the fixtures).
#: CNN Türk's label says GMT but is really Turkey local time (UTC+3), so its
#: value is the wall-clock minus 3 hours (22:56:02 -> 19:56:02 UTC).
EXPECTED_FIRST_PUBLISHED = {
    "sabah": "2026-10-03T14:28:06+00:00",
    "haberturk": "2026-10-03T15:01:10+00:00",
    "sozcu": "2026-10-03T15:18:08+00:00",
    "bloomberght": "2026-10-03T18:51:00+00:00",
    "cnnturk": "2026-10-03T19:56:02+00:00",
}


def _load(source: str) -> bytes:
    return (FIXTURES / f"{source}.feed.raw.xml").read_bytes()


@pytest.mark.parametrize("source", sorted(NEWS_FEEDS))
def test_real_feed_parses_with_url_and_title(source: str) -> None:
    items = parse_feed(_load(source), source)

    assert items, f"{source} parsed no items"
    for item in items:
        assert item.url.startswith("http")
        assert item.title
        assert item.external_id == item.url


@pytest.mark.parametrize("source", sorted(NEWS_FEEDS))
def test_external_id_is_the_normalised_link(source: str) -> None:
    items = parse_feed(_load(source), source)
    raw = ET.fromstring(_load(source))
    channel = next(child for child in raw if child.tag.rsplit("}", 1)[-1] == "channel")
    raw_links: list[str] = []
    for raw_item in channel:
        if raw_item.tag.rsplit("}", 1)[-1] != "item":
            continue
        for child in raw_item:
            if child.tag == "link":
                raw_links.append((child.text or "").strip())
    assert [item.external_id for item in items] == raw_links


def test_cnnturk_ignores_the_opaque_guid() -> None:
    items = parse_feed(_load("cnnturk"), "cnnturk")
    guids = [child.text for child in ET.fromstring(_load("cnnturk")).iter() if child.tag == "guid"]
    assert guids  # the feed does carry non-URL guids
    assert all(item.external_id not in guids for item in items)
    assert all(item.external_id.startswith("https://www.cnnturk.com/") for item in items)


@pytest.mark.parametrize("source", sorted(NEWS_FEEDS))
def test_pub_date_is_tz_aware_utc(source: str) -> None:
    item = parse_feed(_load(source), source)[0]
    assert item.published_at is not None
    assert item.published_at.tzinfo is not None
    assert item.published_at.utcoffset().total_seconds() == 0
    assert item.published_at.isoformat() == EXPECTED_FIRST_PUBLISHED[source]
    assert item.published_at.astimezone(UTC) == item.published_at


def test_cnnturk_pubdate_is_turkey_local_not_gmt() -> None:
    """CNN's pubDate label says GMT but is really UTC+3 (measured live 2026-10-03)."""
    raw = _load("cnnturk").decode("utf-8", errors="replace")
    match = re.search(r"<pubDate>([^<]+)</pubDate>", raw)
    assert match is not None
    raw_stamp = match.group(1)
    assert raw_stamp.endswith("GMT")  # the wrong label

    wall_clock = parse_pub_date(raw_stamp.replace("GMT", "+0000"))
    assert wall_clock is not None
    item = parse_feed(_load("cnnturk"), "cnnturk")[0]
    # Interpreted as UTC+3, i.e. the naive wall-clock minus 3 hours in UTC.
    assert item.published_at == wall_clock - timedelta(hours=3)


@pytest.mark.parametrize("source", ["sabah", "haberturk", "sozcu", "bloomberght"])
def test_other_sources_keep_their_labelled_zone(source: str) -> None:
    """Only CNN Türk is overridden; the other four parse as labelled."""
    item = parse_feed(_load(source), source)[0]
    assert item.published_at is not None
    assert item.published_at.isoformat() == EXPECTED_FIRST_PUBLISHED[source]


def test_cdata_and_whitespace_are_handled() -> None:
    items = parse_feed(_load("sozcu"), "sozcu")
    summary = items[0].rss_summary
    assert summary is not None
    assert summary == summary.strip()
    assert "<" not in summary and ">" not in summary

    bloom = parse_feed(_load("bloomberght"), "bloomberght")
    assert bloom[0].title == bloom[0].title.strip()
    assert bloom[0].title


def test_non_xml_body_raises_feed_error() -> None:
    with pytest.raises(FeedError):
        parse_feed(b"<html><body>not a feed</body></html>", "sabah")


def test_body_without_channel_raises_feed_error() -> None:
    with pytest.raises(FeedError):
        parse_feed(b'<?xml version="1.0"?><rss version="2.0"></rss>', "sabah")


def test_unknown_source_is_rejected() -> None:
    with pytest.raises(ValueError):
        parse_feed(_load("sabah"), "yenisafak")


def test_item_without_pub_date_keeps_none() -> None:
    feed = (
        b'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>'
        b"<item><title>T</title><link>https://example.com/a</link>"
        b"<description>d</description></item></channel></rss>"
    )
    items = parse_feed(feed, "sabah")
    assert len(items) == 1
    assert items[0].published_at is None
    assert items[0].external_id == "https://example.com/a"


def test_item_without_link_is_skipped() -> None:
    feed = (
        b'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>'
        b"<item><title>T</title><pubDate>Sat, 03 Oct 2026 17:28:06 +0300</pubDate></item>"
        b"</channel></rss>"
    )
    # A present item whose link is missing is skipped, not an error.
    assert parse_feed(feed, "sabah") == []


def test_channel_without_items_raises_feed_error() -> None:
    feed = b'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title></channel></rss>'
    with pytest.raises(FeedError, match="has no items"):
        parse_feed(feed, "bloomberght")


def test_channel_whose_items_all_lack_a_link_returns_empty() -> None:
    feed = (
        b'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>'
        b"<item><title>A</title></item><item><title>B</title></item>"
        b"</channel></rss>"
    )
    # Items are present (just unusable), so no FeedError is raised.
    assert parse_feed(feed, "bloomberght") == []


def test_parse_pub_date_rejects_garbage() -> None:
    assert parse_pub_date("not a date") is None
    assert parse_pub_date(None) is None
    assert parse_pub_date("  ") is None


def test_strip_html_drops_tags_and_collapses_space() -> None:
    assert strip_html("<p>Merhaba <b>dünya</b></p>") == "Merhaba dünya"
    assert strip_html(None) is None
    assert strip_html("   ") is None
