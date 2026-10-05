"""CLI: ``python -m app.news``.

Commands:

- ``poll [--dry-run]`` — poll the five RSS feeds. ``--dry-run`` fetches and
  parses every feed and prints the per-source item count and what the first-run
  rule would pick, writing nothing (no DB, no MinIO). A real run inserts the
  unknown items through :func:`app.news.service.poll_feeds` (the same function
  the Celery task calls) and exits non-zero when a feed failed.
- ``fetch --id N`` — re-fetch one article's full text. The row is reset to
  ``pending`` first, so this also works on ``failed``/``no_text`` rows.

Windows consoles are cp1254: this CLI never prints article text, only counts and
short titles, so output cannot crash on Turkish characters.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime

import httpx

from app.config import settings
from app.connectors.base import MinioObjectStore
from app.data.models import NewsArticle
from app.news import service
from app.news.feeds import NEWS_FEEDS, FeedError


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser (pure, importable without a database)."""
    parser = argparse.ArgumentParser(
        prog="app.news",
        description="News RSS intake and full-text fetching (Task 1.7).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    poll = subparsers.add_parser("poll", help="poll the five RSS feeds")
    poll.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="fetch and parse every feed but write nothing (no DB, no MinIO)",
    )

    fetch = subparsers.add_parser("fetch", help="re-fetch one article's full text")
    fetch.add_argument("--id", type=int, required=True, dest="article_id")
    return parser


def _dry_run(client: httpx.Client) -> int:
    """Fetch and parse every feed; print counts; write nothing.

    With no DB the dry-run cannot know whether a source is on its first poll, so
    it does not claim a later-poll pick count: it reports the item count and the
    first-run rule ("4 newest if this were the first poll of the source").
    """
    for source in NEWS_FEEDS:
        try:
            items = service.load_feed(client, source)
        except (FeedError, httpx.HTTPError) as exc:
            print(f"{source}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        pick = min(settings.news_first_run_limit, len(items))
        print(
            f"poll (dry-run) {source}: items={len(items)} "
            f"first_run_pick={pick} (first run rule; later polls take new items only)"
        )
    return 0


def _poll(client: httpx.Client, *, dry_run: bool) -> int:
    if dry_run:
        return _dry_run(client)

    from app.db.session import SessionLocal

    with SessionLocal() as session:
        report = service.poll_feeds(
            session,
            client,
            now=datetime.now(UTC),
            first_run_limit=settings.news_first_run_limit,
        )
        session.commit()
    for item in report.sources:
        suffix = f" error={item.error}" if item.error else ""
        print(f"poll {item.source}: fetched={item.fetched_items} inserted={item.inserted}{suffix}")
    print(f"poll: inserted={len(report.new_ids)}")
    if report.failed_sources:
        print(
            f"poll: {len(report.failed_sources)} feed(s) failed: "
            f"{', '.join(report.failed_sources)}",
            file=sys.stderr,
        )
        return 1
    return 0


def _fetch(client: httpx.Client, article_id: int) -> int:
    from app.db.session import SessionLocal

    store = MinioObjectStore()
    with SessionLocal() as session:
        article = session.get(NewsArticle, article_id)
        if article is None:
            print(f"fetch: article {article_id} not found", file=sys.stderr)
            return 1
        if article.text_status != service.PENDING:
            article.text_status = service.PENDING
            article.text_error = None
            session.commit()
        result = service.fetch_article(session, client, store, article_id, now=datetime.now(UTC))
        session.commit()
    print(f"fetch: article={article_id} outcome={result.outcome} chars={result.chars}")
    if result.error:
        print(f"fetch: {result.error}", file=sys.stderr)
    return 0 if result.outcome == "ok" else 1


def main(argv: list[str] | None = None) -> int:
    """Run the CLI. Returns a process exit code."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    client = httpx.Client(follow_redirects=True)
    try:
        if args.command == "poll":
            return _poll(client, dry_run=bool(args.dry_run))
        return _fetch(client, args.article_id)
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
