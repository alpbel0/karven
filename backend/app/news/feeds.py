"""The five news feeds and a pure RSS 2.0 parser (Task 1.7).

``NEWS_FEEDS`` fixes the five sources verified live on 2026-10-03. All five
serve RSS 2.0 ``<item>`` rows; the article URL is the plain ``<link>`` element in
every feed, so :func:`parse_feed` uses the normalised ``<link>`` as
``external_id`` (never ``<guid>``: CNN Türk's is an opaque hash, not a URL).

The parser is network-free and only uses the standard library. It tolerates XML
namespaces (Habertürk/Sabah/Sözcü/BloombergHT declare prefixes; CNN Türk adds an
``<atom:link>`` beside the RSS ``<link>``) by matching plain RSS tags first and
ignoring the Atom namespace. A non-XML body, or an XML body with no
``<channel>``, is a typed :class:`FeedError`.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

#: source name -> feed URL (the five feeds verified live 2026-10-03).
NEWS_FEEDS: dict[str, str] = {
    "sabah": "https://www.sabah.com.tr/rss/ekonomi.xml",
    "haberturk": "https://www.haberturk.com/rss/ekonomi.xml",
    "sozcu": "https://www.sozcu.com.tr/feeds-rss-category-ekonomi",
    "bloomberght": "https://www.bloomberght.com/rss",
    "cnnturk": "https://www.cnnturk.com/feed/rss/ekonomi/news",
}

#: Every known source name, in feed order.
NEWS_SOURCES: tuple[str, ...] = tuple(NEWS_FEEDS)

#: The RSS summary is not the article; keep only a short, HTML-free teaser.
RSS_SUMMARY_MAX_CHARS = 1000

# CNN Türk's RSS ``pubDate`` is labelled GMT but is really Turkey local time
# (UTC+3). Measured live 2026-10-03 against the article pages' own publish time:
# RSS "Sat, 03 Oct 2026 22:56:02 GMT" <-> page "2026-10-03T22:56:02+03:00";
# 21:52:54 GMT <-> 21:51:00+03:00; 20:56:03 GMT <-> 20:56:03+03:00. Parsed as
# GMT every CNN row's ``published_at`` is 3 hours in the future. The other four
# feeds are correct as labelled (checked the same way) and are NOT overridden.
PUBDATE_LOCAL_TIME_SOURCES: frozenset[str] = frozenset({"cnnturk"})

# Turkey has been UTC+3 all year since 2016; a fixed offset avoids needing
# tzdata, which the slim Docker image may not have (no ``zoneinfo``).
_TURKEY_OFFSET = timezone(timedelta(hours=3))

_ATOM_NAMESPACE = "http://www.w3.org/2005/Atom"
_WHITESPACE = re.compile(r"\s+")


class FeedError(Exception):
    """The feed body is not a usable RSS document (non-XML / no ``<channel>``)."""


@dataclass(frozen=True)
class FeedItem:
    """One parsed RSS item (pure data; no network, no HTML)."""

    external_id: str
    url: str
    title: str
    published_at: datetime | None
    rss_summary: str | None


class _HtmlTextExtractor(HTMLParser):
    """Collect text nodes, dropping tags (used to flatten an RSS summary)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def strip_html(value: str | None) -> str | None:
    """Return ``value`` with tags removed and whitespace collapsed.

    ``None`` for empty input; falls back to the stripped raw value when the
    fragment cannot be parsed as HTML.
    """
    if value is None:
        return None
    if not value.strip():
        return None
    parser = _HtmlTextExtractor()
    try:
        parser.feed(value)
        parser.close()
    except Exception:  # noqa: BLE001 - a broken fragment falls back to raw text
        return value.strip() or None
    text = _WHITESPACE.sub(" ", "".join(parser.parts)).strip()
    return text or None


def parse_pub_date(value: str | None, *, source: str | None = None) -> datetime | None:
    """Parse an RFC 822 ``pubDate`` into a tz-aware UTC datetime.

    A missing or unparseable value returns ``None`` (never invented). For the
    sources in :data:`PUBDATE_LOCAL_TIME_SOURCES` (CNN Türk) the wall-clock is
    interpreted as UTC+3 regardless of its (wrong) zone label -- see the table's
    comment. Every other source keeps its own labelled zone.
    """
    if value is None:
        return None
    stamp = value.strip()
    if not stamp:
        return None
    try:
        parsed = parsedate_to_datetime(stamp)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if source in PUBDATE_LOCAL_TIME_SOURCES:
        # Reinterpret the wall-clock in UTC+3, discarding the wrong label.
        parsed = parsed.replace(tzinfo=_TURKEY_OFFSET)
    elif parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find_child(item: ET.Element, name: str) -> ET.Element | None:
    """Find a plain RSS child by local name, ignoring Atom-namespaced tags.

    The plain (no namespace) tag always wins; a namespaced tag is a fallback only
    when it is not the Atom ``<link>`` that CNN Türk adds at item level.
    """
    fallback: ET.Element | None = None
    for child in item:
        if _local_name(child.tag) != name:
            continue
        if child.tag == name:
            return child
        if fallback is None and not child.tag.startswith(f"{{{_ATOM_NAMESPACE}}}"):
            fallback = child
    return fallback


def _text(item: ET.Element, name: str) -> str | None:
    child = _find_child(item, name)
    if child is None or child.text is None:
        return None
    return child.text


def parse_feed(xml_bytes: bytes, source: str) -> list[FeedItem]:
    """Parse one real RSS 2.0 feed into items; pure and network-free.

    Items without a usable ``<link>`` are skipped (they cannot be identified).
    Titles are kept verbatim but never empty; a missing ``<title>`` falls back to
    the URL. Raises :class:`FeedError` on non-XML bodies, a missing
    ``<channel>``, or a channel with no ``<item>`` at all (a real feed holds
    10-50 items, so an empty channel is a feed failure that must be visible).
    """
    if source not in NEWS_FEEDS:
        raise ValueError(f"unknown news source {source!r}")
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise FeedError(f"feed for {source!r} is not valid XML: {exc}") from exc

    channel: ET.Element | None = None
    for child in root:
        if _local_name(child.tag) == "channel":
            channel = child
            break
    if channel is None:
        raise FeedError(f"feed for {source!r} has no <channel>")

    items: list[FeedItem] = []
    raw_item_count = 0
    for raw in channel:
        if _local_name(raw.tag) != "item":
            continue
        raw_item_count += 1
        url = (_text(raw, "link") or "").strip()
        if not url:
            continue
        title = (_text(raw, "title") or "").strip() or url
        summary = strip_html(_text(raw, "description"))
        if summary is not None and len(summary) > RSS_SUMMARY_MAX_CHARS:
            summary = summary[:RSS_SUMMARY_MAX_CHARS].rstrip()
        items.append(
            FeedItem(
                external_id=url,
                url=url,
                title=title,
                published_at=parse_pub_date(_text(raw, "pubDate"), source=source),
                rss_summary=summary,
            )
        )
    if raw_item_count == 0:
        raise FeedError(f"feed for {source!r} has no items")
    return items


__all__ = [
    "NEWS_FEEDS",
    "NEWS_SOURCES",
    "PUBDATE_LOCAL_TIME_SOURCES",
    "RSS_SUMMARY_MAX_CHARS",
    "FeedError",
    "FeedItem",
    "parse_feed",
    "parse_pub_date",
    "strip_html",
]
