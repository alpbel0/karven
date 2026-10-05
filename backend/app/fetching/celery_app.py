"""The Celery application for on-demand fetching (Task 1.5) and news (Task 1.7).

Broker and result backend come from ``settings.redis_url``; results are ignored
(``task_ignore_result``) because a job's state lives in ``fetch_jobs`` and a news
article's in ``news_articles``. The default queue stays ``fetch`` for the run and
watchdog tasks; ``news.*`` is routed to its own ``news`` queue so news fetching
never starves on-demand jobs. The beat schedules the fetch watchdog and the news
poll. There is no automatic retry (acks are late, no autoretry): the watchdog
owns every stuck fetch job and every expected news source error is recorded.

``include=["app.fetching.tasks", "app.news.tasks"]`` is how Celery registers the
tasks: neither the worker nor the beat imports the task modules directly, so
without it the worker starts with an empty task list and both ``fetch.*`` and
``news.*`` (and the beat entries pointing at them) go unregistered. The task
modules import ``app`` from this module, and ``include`` is imported lazily at
worker/beat start, so there is no circular-import problem.
"""

from __future__ import annotations

from celery import Celery

from app.config import settings

app = Celery(
    "karven_fetch",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.fetching.tasks", "app.news.tasks"],
)

app.conf.update(
    task_ignore_result=True,
    task_default_queue="fetch",
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    # No automatic retry: a lost/broken worker must not silently retry; the
    # watchdog fails a stalled job instead.
    task_reject_on_worker_lost=False,
    timezone="UTC",
    # News articles go to their own queue; the fetch.* tasks keep the default.
    task_routes={"news.*": {"queue": "news"}},
    beat_schedule={
        "fetch-watchdog": {
            "task": "fetch.watchdog",
            "schedule": float(settings.fetch_watchdog_interval_seconds),
        },
        "news-poll": {
            "task": "news.poll",
            "schedule": float(settings.news_poll_interval_seconds),
        },
    },
)
