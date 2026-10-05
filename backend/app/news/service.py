"""News polling and full-text fetching (Task 1.7).

The two write paths the CLI and the Celery tasks share:

- :func:`poll_feeds` fetches the five RSS feeds, parses them, and inserts items
  not yet known (``INSERT ... ON CONFLICT (source, external_id) DO NOTHING
  RETURNING id``). The first time a source has no rows only its
  ``first_run_limit`` newest items (by ``pubDate``) are taken; every later poll
  takes every unknown item. A failing feed never stops the others: its error is
  recorded in the report and logged, and the caller decides to raise
  :class:`NewsPollError`.
- :func:`fetch_article` locks one ``pending`` row, fetches its page (up to
  ``news_max_attempts`` attempts, only timeout/transport/5xx retried; a 4xx is
  final), stores the raw HTML and extracts the full text. The outcome is written
  on the row (``ok`` / ``no_text`` / ``failed`` plus ``text_error``); expected
  source errors are recorded, never raised.

There is no cross-source duplicate-news detection in the MVP: every article is
processed, and the only dedup is the technical ``(source, external_id)`` one.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import settings
from app.connectors.base import ObjectStore, store_raw
from app.data.models import NewsArticle
from app.news.extract import extract_with_error
from app.news.feeds import NEWS_FEEDS, FeedError, FeedItem, parse_feed

logger = logging.getLogger(__name__)

#: ``news_articles.text_status`` values.
PENDING = "pending"
OK = "ok"
NO_TEXT = "no_text"
FAILED = "failed"

#: Feed requests are attempted twice (spec: "2 attempts").
_FEED_ATTEMPTS = 2
#: Short backoff between article attempts (no setting for this in the MVP).
_FETCH_BACKOFF_S = 1.0

Clock = Callable[[], datetime]
Sleeper = Callable[[float], None]


class NewsPollError(Exception):
    """One or more feeds failed during a poll; the rest were still handled."""

    def __init__(self, sources: Sequence[str]) -> None:
        self.sources = list(sources)
        super().__init__("news feeds failed: " + ", ".join(self.sources))


@dataclass(frozen=True)
class SourcePollResult:
    """Per-source outcome of one poll (an error is recorded, never raised here)."""

    source: str
    fetched_items: int
    inserted: int
    error: str | None = None


@dataclass(frozen=True)
class PollReport:
    """A full poll: every source's outcome plus the ids of newly inserted rows."""

    sources: list[SourcePollResult]
    new_ids: list[int] = field(default_factory=list)

    @property
    def failed_sources(self) -> list[str]:
        return [item.source for item in self.sources if item.error]


@dataclass(frozen=True)
class ArticleFetchResult:
    """The outcome of one :func:`fetch_article` call."""

    article_id: int
    outcome: str  # skipped | ok | no_text | failed
    status: str | None
    chars: int
    error: str | None = None


def _error_text(exc: BaseException) -> str:
    message = str(exc).strip()
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def _fetch_feed(
    client: httpx.Client,
    url: str,
    *,
    user_agent: str,
    timeout_s: float,
    sleeper: Sleeper,
) -> httpx.Response:
    """GET one feed with a browser UA, retrying timeouts/transport/5xx once."""
    headers = {"User-Agent": user_agent}
    last_exc: Exception | None = None
    for attempt in range(_FEED_ATTEMPTS):
        try:
            response = client.get(url, headers=headers, timeout=timeout_s)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_exc = exc
            if attempt + 1 < _FEED_ATTEMPTS:
                sleeper(_FETCH_BACKOFF_S)
                continue
            raise
        if response.status_code == 200:
            return response
        if response.status_code >= 500 and attempt + 1 < _FEED_ATTEMPTS:
            sleeper(_FETCH_BACKOFF_S)
            continue
        return response
    raise last_exc if last_exc is not None else RuntimeError("feed request failed")


def load_feed(
    client: httpx.Client, source: str, *, sleeper: Sleeper = time.sleep
) -> list[FeedItem]:
    """Fetch and parse one feed; raise on HTTP/parse failure.

    Shared by :func:`poll_feeds` and the CLI ``--dry-run`` so both use the same
    retry/parse path.
    """
    response = _fetch_feed(
        client,
        NEWS_FEEDS[source],
        user_agent=settings.news_user_agent,
        timeout_s=settings.news_request_timeout_s,
        sleeper=sleeper,
    )
    if response.status_code != 200:
        raise FeedError(f"HTTP {response.status_code}")
    return parse_feed(response.content, source)


def _select_items(
    items: Sequence[FeedItem], *, first_run_limit: int, baseline: datetime | None
) -> tuple[list[FeedItem], int]:
    """Pick the items to insert for one source.

    ``baseline is None`` is the first poll of the source: only the newest
    ``first_run_limit`` items (by ``pubDate``) are taken. On a later poll
    ``baseline`` is the source's first-poll time (``MIN(first_seen_at)``): only
    genuinely new items (``published_at >= baseline``) are taken, so the older
    backlog the first-run limit was meant to skip is never inserted. Items with
    no ``published_at`` are always taken (never lose news); their count is
    returned so the caller can warn once per poll per source.
    """
    if baseline is None:
        ordered = sorted(
            items,
            key=lambda item: item.published_at or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        return ordered[:first_run_limit], 0
    selected: list[FeedItem] = []
    undated = 0
    for item in items:
        if item.published_at is None:
            selected.append(item)
            undated += 1
        elif item.published_at >= baseline:
            selected.append(item)
    return selected, undated


def _source_baseline(session: Session, source: str) -> datetime | None:
    """The source's first-poll time (``MIN(first_seen_at)``), or ``None`` if new."""
    return session.scalar(
        sa.select(sa.func.min(NewsArticle.first_seen_at)).where(NewsArticle.source == source)
    )


def _insert_unknown(
    session: Session, source: str, items: Sequence[FeedItem], *, now: datetime
) -> list[int]:
    """Insert unknown items, ignoring conflicts; return the new row ids."""
    new_ids: list[int] = []
    for item in items:
        statement = (
            pg_insert(NewsArticle)
            .values(
                source=source,
                external_id=item.external_id,
                url=item.url,
                title=item.title,
                published_at=item.published_at,
                rss_summary=item.rss_summary,
                text_status=PENDING,
                fetch_attempts=0,
                first_seen_at=now,
                attributes={},
            )
            .on_conflict_do_nothing(index_elements=["source", "external_id"])
            .returning(NewsArticle.id)
        )
        new_id = session.scalar(statement)
        if new_id is not None:
            new_ids.append(int(new_id))
    return new_ids


def poll_feeds(
    session: Session,
    client: httpx.Client,
    *,
    now: datetime,
    first_run_limit: int,
) -> PollReport:
    """Poll all five feeds and insert unknown new items; never aborts on one failure.

    The first time a source is seen, only its ``first_run_limit`` newest items
    are taken; afterwards only genuinely new items (``published_at`` at or after
    the source's first-poll time) are taken, so the old backlog is not inserted.
    """
    results: list[SourcePollResult] = []
    new_ids: list[int] = []
    for source in NEWS_FEEDS:
        try:
            items = load_feed(client, source)
        except (FeedError, httpx.HTTPError) as exc:
            error = _error_text(exc)
            logger.error("news feed %s failed: %s", source, error)
            results.append(SourcePollResult(source, 0, 0, error))
            continue

        baseline = _source_baseline(session, source)
        selected, undated = _select_items(items, first_run_limit=first_run_limit, baseline=baseline)
        if undated:
            logger.warning("news source %s: %d undated items taken", source, undated)
        inserted = _insert_unknown(session, source, selected, now=now)
        new_ids.extend(inserted)
        results.append(SourcePollResult(source, len(items), len(inserted)))
    session.flush()
    return PollReport(sources=results, new_ids=new_ids)


def _store_raw_article(store: ObjectStore | None, source: str, html_bytes: bytes) -> str | None:
    """Store the raw HTML; a storage failure is logged and never breaks the fetch."""
    if store is None or not html_bytes:
        return None
    try:
        return store_raw("news", source, "article", html_bytes, "html", store=store)
    except Exception:  # noqa: BLE001 - raw storage must not break the data path
        logger.warning("news raw store failed for source %s", source, exc_info=True)
        return None


def fetch_article(
    session: Session,
    client: httpx.Client,
    store: ObjectStore | None,
    article_id: int,
    *,
    now: datetime,
    sleeper: Sleeper = time.sleep,
    max_attempts: int | None = None,
    min_text_chars: int | None = None,
    backoff_s: float = _FETCH_BACKOFF_S,
) -> ArticleFetchResult:
    """Fetch one pending article's page, store it and write the text outcome.

    The row is locked with ``FOR UPDATE`` and re-read with
    ``populate_existing`` (the session uses ``expire_on_commit=False``, so a
    stale in-memory status was a real bug in Task 1.5). Only ``timeout`` /
    transport / 5xx are retried; a 4xx is final. Expected source errors are
    recorded on the row and never raised.
    """
    attempts_limit = max_attempts if max_attempts is not None else settings.news_max_attempts
    threshold = min_text_chars if min_text_chars is not None else settings.news_min_text_chars
    article = session.scalar(
        sa.select(NewsArticle)
        .where(NewsArticle.id == article_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if article is None:
        return ArticleFetchResult(article_id, "skipped", None, 0, "article not found")
    if article.text_status != PENDING:
        return ArticleFetchResult(
            article_id, "skipped", article.text_status, len(article.content_text or "")
        )

    source = article.source
    url = article.url
    headers = {"User-Agent": settings.news_user_agent}
    attempts = 0
    response: httpx.Response | None = None
    failure: str | None = None
    for attempt in range(attempts_limit):
        attempts += 1
        try:
            candidate = client.get(url, headers=headers, timeout=settings.news_request_timeout_s)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            failure = _error_text(exc)
            if attempt + 1 < attempts_limit:
                sleeper(backoff_s)
                continue
            break
        if candidate.status_code == 200:
            response = candidate
            failure = None
            break
        failure = f"HTTP {candidate.status_code}"
        if candidate.status_code >= 500 and attempt + 1 < attempts_limit:
            sleeper(backoff_s)
            continue
        break

    article.fetch_attempts = (article.fetch_attempts or 0) + attempts
    article.fetched_at = now

    if response is None:
        article.text_status = FAILED
        article.content_text = None
        article.text_error = failure or "fetch failed"
        session.flush()
        return ArticleFetchResult(article_id, "failed", FAILED, 0, article.text_error)

    html_bytes = response.content
    raw_key = _store_raw_article(store, source, html_bytes)
    if raw_key is not None:
        article.raw_object_key = raw_key

    extracted = extract_with_error(html_bytes)
    text = extracted.text
    if text is not None and len(text) >= threshold:
        article.text_status = OK
        article.content_text = text
        article.text_error = None
        session.flush()
        return ArticleFetchResult(article_id, "ok", OK, len(text))

    article.text_status = NO_TEXT
    article.content_text = None
    if extracted.error is not None:
        article.text_error = f"extraction error: {extracted.error}"
    elif not text:
        article.text_error = "no text extracted"
    else:
        article.text_error = f"extracted {len(text)} chars < {threshold}"
    session.flush()
    return ArticleFetchResult(article_id, "no_text", NO_TEXT, 0, article.text_error)


def requeue_pending(session: Session, now: datetime, older_than: timedelta) -> list[int]:
    """Ids of ``pending`` rows never fetched and older than ``older_than``.

    Covers an enqueue that was lost: such a row would otherwise stay ``pending``
    forever. Rows already attempted (``fetched_at`` set) are not requeued.
    """
    cutoff = now - older_than
    statement = (
        sa.select(NewsArticle.id)
        .where(
            NewsArticle.text_status == PENDING,
            NewsArticle.fetched_at.is_(None),
            NewsArticle.first_seen_at < cutoff,
        )
        .order_by(NewsArticle.id)
    )
    return [int(row) for row in session.scalars(statement).all()]


__all__ = [
    "FAILED",
    "NO_TEXT",
    "OK",
    "PENDING",
    "ArticleFetchResult",
    "NewsPollError",
    "PollReport",
    "SourcePollResult",
    "fetch_article",
    "load_feed",
    "poll_feeds",
    "requeue_pending",
]
