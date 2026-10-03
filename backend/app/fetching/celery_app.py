"""The Celery application for on-demand fetching (Task 1.5).

Broker and result backend come from ``settings.redis_url``; results are ignored
(``task_ignore_result``) because a job's state lives in ``fetch_jobs``, and one
worker queue named ``fetch`` carries both the run and the watchdog tasks. The
beat only schedules the watchdog; there is no automatic retry (acks are late,
no autoretry), so the watchdog owns every stuck job.

``include=["app.fetching.tasks"]`` is how Celery registers the tasks: neither
the worker nor the beat imports ``tasks.py`` directly, so without it the worker
starts with an empty task list and both ``fetch.run_job`` and ``fetch.watchdog``
(and the beat entry pointing at the latter) go unregistered. ``tasks.py`` imports
``app`` from this module, and ``include`` is imported lazily at worker/beat start,
so there is no circular-import problem.
"""

from __future__ import annotations

from celery import Celery

from app.config import settings

app = Celery(
    "karven_fetch",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.fetching.tasks"],
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
    beat_schedule={
        "fetch-watchdog": {
            "task": "fetch.watchdog",
            "schedule": float(settings.fetch_watchdog_interval_seconds),
        }
    },
)
