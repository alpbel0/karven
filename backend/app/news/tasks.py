"""Celery tasks for news intake (Task 1.7).

``news.poll`` runs on the beat every ``news_poll_interval_seconds`` (900 s): it
polls the feeds, commits the new rows, then enqueues one ``news.fetch_article``
per newly inserted id and per requeued ``pending`` id. After all feeds were
handled (and the new rows committed and enqueued) it raises
:class:`~app.news.service.NewsPollError` when a feed failed, so a broken feed is
visible while the other four still succeed.

``news.fetch_article`` fetches one article's full text. It is ``acks_late`` with
no autoretry: an expected source error is recorded on the row, not retried.

Every task builds and disposes its own engine, like ``app.fetching.tasks``, so
the worker holds no connection across tasks. A failed enqueue is logged and
ignored (the row stays ``pending`` and ``requeue_pending`` picks it up next
tick).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.connectors.base import MinioObjectStore
from app.db.session import create_db_engine
from app.fetching.celery_app import app
from app.news.service import (
    NewsPollError,
    PollReport,
    fetch_article,
    poll_feeds,
    requeue_pending,
)

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], Session]
Enqueue = Callable[[int], None]


def _real_clock() -> datetime:
    return datetime.now(UTC)


def _session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


def enqueue_article(article_id: int) -> None:
    """Put one article on the ``news`` queue for the fetch task."""
    fetch_article_task.apply_async(args=[article_id], queue="news")


def _safe_enqueue_article(article_id: int) -> None:
    """Enqueue an article; a broker failure is logged, never raised."""
    try:
        enqueue_article(article_id)
    except Exception:  # noqa: BLE001 - the row stays pending and is requeued
        logger.exception("news article %s could not be enqueued", article_id)


def run_poll(
    session_factory: SessionFactory,
    client: httpx.Client,
    *,
    now: datetime,
    first_run_limit: int,
    requeue_after: timedelta,
    enqueue: Enqueue = _safe_enqueue_article,
) -> PollReport:
    """Poll, commit new rows, enqueue fetches, then raise on any failed feed."""
    with session_factory() as session:
        report = poll_feeds(session, client, now=now, first_run_limit=first_run_limit)
        session.commit()
    for article_id in report.new_ids:
        enqueue(article_id)
    with session_factory() as session:
        pending_ids = requeue_pending(session, now, requeue_after)
    for article_id in pending_ids:
        enqueue(article_id)
    if report.failed_sources:
        raise NewsPollError(report.failed_sources)
    return report


@app.task(name="news.poll", ignore_result=True)
def poll_feeds_task() -> None:
    """Beat task: poll the five feeds and enqueue the new articles."""
    engine = create_db_engine()
    client = httpx.Client(follow_redirects=True)
    try:
        report = run_poll(
            _session_factory(engine),
            client,
            now=_real_clock(),
            first_run_limit=settings.news_first_run_limit,
            requeue_after=timedelta(seconds=settings.news_pending_requeue_seconds),
        )
        logger.info(
            "news poll: sources=%d inserted=%d failed=%d",
            len(report.sources),
            len(report.new_ids),
            len(report.failed_sources),
        )
    finally:
        client.close()
        engine.dispose()


@app.task(name="news.fetch_article", acks_late=True, ignore_result=True)
def fetch_article_task(article_id: int) -> None:
    """Fetch one article's page and write the extracted text outcome."""
    engine = create_db_engine()
    client = httpx.Client(follow_redirects=True)
    try:
        with _session_factory(engine)() as session:
            result = fetch_article(
                session,
                client,
                MinioObjectStore(),
                article_id,
                now=_real_clock(),
            )
            session.commit()
        logger.info(
            "news article %d: outcome=%s status=%s chars=%d",
            article_id,
            result.outcome,
            result.status,
            result.chars,
        )
    finally:
        client.close()
        engine.dispose()


__all__ = [
    "enqueue_article",
    "fetch_article_task",
    "poll_feeds_task",
    "run_poll",
]
