"""Regression tests for fetch Celery task registration (Task 1.5, round 2).

The worker and beat are started with ``celery -A app.fetching.celery_app`` and
nothing imports ``app.fetching.tasks`` directly, so registration depends on the
app's ``include``. An in-process check is unreliable: other test modules import
``app.fetching.tasks`` first and leave its tasks on the shared app, hiding a
missing ``include``. This probe therefore runs in a fresh interpreter that
imports ONLY ``app.fetching.celery_app`` and then loads default modules exactly
the way the worker/beat do.

No broker and no database are touched.
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
    "beat": [entry["task"] for entry in app.conf.beat_schedule.values()],
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


def test_run_job_watchdog_and_diagnose_are_registered() -> None:
    tasks = _probe()["tasks"]

    assert "fetch.run_job" in tasks
    assert "fetch.watchdog" in tasks
    assert "fetch.diagnose_job" in tasks


def test_beat_schedule_points_at_a_registered_task() -> None:
    probe = _probe()

    assert probe["beat"], "beat schedule is empty"
    assert set(probe["beat"]) <= set(probe["tasks"])


def test_default_queue_is_fetch() -> None:
    assert _probe()["default_queue"] == "fetch"
