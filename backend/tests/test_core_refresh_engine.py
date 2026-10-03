"""Unit tests for the ``refresh_core`` wiring (no database).

The engine itself runs against PostgreSQL in ``tests/integration``; here the
collaborators are stubbed so the plumbing can be asserted cheaply: ``now`` drives
the decisions while the injected ``clock`` is handed to ``_process_series`` for
the ``fetch_jobs`` timestamps.
"""

from __future__ import annotations

from datetime import UTC, datetime

import app.core.refresh as refresh
from app.config import Settings

NO_ENV = Settings(_env_file=None)
INJECTED = datetime(2030, 1, 1, 12, tzinfo=UTC)
SENTINEL = datetime(2030, 1, 1, 12, 30, tzinfo=UTC)


class _Session:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_refresh_core_forwards_now_and_clock_to_the_job_writer(monkeypatch) -> None:
    captured: dict = {}

    monkeypatch.setattr(refresh, "find_institution", lambda session, code: None)
    monkeypatch.setattr(refresh, "_load_calendar_rows", lambda session, core: [])
    monkeypatch.setattr(
        refresh,
        "build_state",
        lambda session, core, rows, *, institution_id: refresh.SeriesState(
            external_code=core.external_code,
            frequency="monthly",
            has_calendar=True,
            max_period=None,
        ),
    )
    monkeypatch.setattr(
        refresh,
        "decide",
        lambda state, rows, now, config=None: refresh.Decision(
            state.external_code, True, "release_due", False
        ),
    )

    def fake_process(
        session,
        core,
        connector,
        decision,
        *,
        force,
        dry_run,
        now,
        clock,
        config,
        institution_id,
        series_id,
    ):
        captured["now"] = now
        captured["clock"] = clock
        return refresh.SeriesReport(core.external_code, "skip", "skipped", True, "release_due")

    monkeypatch.setattr(refresh, "_process_series", fake_process)

    report = refresh.refresh_core(
        lambda: _Session(),
        lambda source: object(),
        now=INJECTED,
        clock=lambda: SENTINEL,
        only="bie_cli2:TP.CLI2.A01",
        config=NO_ENV,
    )

    assert captured["now"] == INJECTED
    assert captured["clock"]() == SENTINEL
    assert report.series[0].due is True
