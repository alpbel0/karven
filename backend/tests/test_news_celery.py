"""Regression tests for news Celery task registration (Task 1.7).

Like ``test_fetch_celery_app.py`` this probe runs in a FRESH interpreter that
imports ONLY ``app.fetching.celery_app`` and then loads default modules the way
the worker/beat do. An in-process check is unreliable because other test modules
import the task modules first and leave their tasks on the shared app, hiding a
missing ``include``. No broker and no database are touched.
"""

from __future__ import annotations

import json
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]

_PROBE = """
import json

from app.fetching.celery_app import app

app.loader.import_default_modules()

print(json.dumps({
    "tasks": sorted(t for t in app.tasks if t.startswith(("fetch.", "news."))),
    "beat_entries": sorted(app.conf.beat_schedule),
    "beat_tasks": sorted(entry["task"] for entry in app.conf.beat_schedule.values()),
    "routes": {key: dict(value) for key, value in app.conf.task_routes.items()},
    "default_queue": app.conf.task_default_queue,
}))
"""


@lru_cache(maxsize=1)
def _probe() -> dict:
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_news_and_fetch_tasks_are_registered() -> None:
    tasks = _probe()["tasks"]

    assert "news.poll" in tasks
    assert "news.fetch_article" in tasks
    assert "fetch.run_job" in tasks
    assert "fetch.watchdog" in tasks
    assert "fetch.diagnose_job" in tasks


def test_beat_has_a_news_poll_entry() -> None:
    probe = _probe()

    assert "news-poll" in probe["beat_entries"]
    assert "news.poll" in probe["beat_tasks"]
    assert "fetch.watchdog" in probe["beat_tasks"]
    assert set(probe["beat_tasks"]) <= set(probe["tasks"])


def test_news_routes_to_the_news_queue() -> None:
    probe = _probe()

    assert probe["routes"]["news.*"]["queue"] == "news"
    assert probe["default_queue"] == "fetch"
