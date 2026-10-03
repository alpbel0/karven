"""Unit tests for the pure :func:`app.core.refresh.decide` (no database).

Every case uses a fixed, timezone-aware clock; the Europe/Istanbul conversion is
part of the logic under test, so the UTC instants are chosen so the local date
and time are unambiguous. Config defaults are grace=2 days, retry=2 hours,
daily cut-off 16:30, daily stale=10 days.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from app.config import Settings
from app.core.refresh import CalendarRow, SeriesState, decide

_NO_ENV = Settings(_env_file=None)

#: expected_on of the single monthly release used by the calendar cases.
RELEASE = date(2026, 10, 30)


def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def _monthly_state(**overrides) -> SeriesState:
    values = {
        "external_code": "bie_cli2:TP.CLI2.A01",
        "frequency": "monthly",
        "has_calendar": True,
        "max_period": date(2026, 8, 1),
    }
    values.update(overrides)
    return SeriesState(**values)


def _release_row() -> CalendarRow:
    return CalendarRow(RELEASE, "2026-10-30 23:00", "Eylül 2026")


# --- calendar series --------------------------------------------------------


def test_calendar_before_release_is_not_due_and_not_alerting() -> None:
    decision = decide(
        _monthly_state(),
        [_release_row()],
        _utc(2026, 10, 29, 12),
        config=_NO_ENV,
    )
    assert decision.due is False
    assert decision.reason == "not_due"
    assert decision.alert_no_new_period is False


def _unsatisfied_state(**overrides) -> SeriesState:
    return _monthly_state(baselines=((RELEASE, date(2026, 8, 1)),), **overrides)


def test_calendar_at_release_is_due_but_not_alerting() -> None:
    # 2026-10-29 21:00 UTC is 2026-10-30 00:00 Istanbul: the release day begins.
    decision = decide(
        _unsatisfied_state(), [_release_row()], _utc(2026, 10, 29, 21), config=_NO_ENV
    )
    assert decision.due is True
    assert decision.reason == "release_due"
    assert decision.alert_no_new_period is False


def test_calendar_respects_retry_spacing() -> None:
    state = _unsatisfied_state(last_attempt_at=_utc(2026, 10, 29, 21))
    decision = decide(state, [_release_row()], _utc(2026, 10, 29, 22), config=_NO_ENV)
    assert decision.due is False
    assert decision.reason == "waiting_retry"


def test_calendar_retries_after_the_window() -> None:
    state = _unsatisfied_state(last_attempt_at=_utc(2026, 10, 29, 15))
    decision = decide(state, [_release_row()], _utc(2026, 10, 29, 21), config=_NO_ENV)
    assert decision.due is True


def test_calendar_satisfied_when_max_period_grew_past_baseline() -> None:
    state = _monthly_state(
        max_period=date(2026, 9, 1),
        baselines=((RELEASE, date(2026, 8, 1)),),
    )
    decision = decide(state, [_release_row()], _utc(2026, 11, 5), config=_NO_ENV)
    assert decision.due is False
    assert decision.reason == "satisfied"
    assert decision.alert_no_new_period is False


def test_calendar_alert_opens_at_grace_boundary() -> None:
    # Grace = 2 days: expected_on + 2 days 00:00 Istanbul is 2026-11-01.
    state = _unsatisfied_state()
    just_before = decide(state, [_release_row()], _utc(2026, 10, 31, 20), config=_NO_ENV)
    assert just_before.alert_no_new_period is False
    at_boundary = decide(state, [_release_row()], _utc(2026, 10, 31, 21), config=_NO_ENV)
    assert at_boundary.due is True
    assert at_boundary.alert_no_new_period is True
    after = decide(state, [_release_row()], _utc(2026, 11, 2, 12), config=_NO_ENV)
    assert after.alert_no_new_period is True


def test_calendar_no_alert_while_retry_window_blocks() -> None:
    # Past the release but inside the retry window: not due, but the grace alert
    # still fires once the grace deadline passes.
    state = _unsatisfied_state(last_attempt_at=_utc(2026, 10, 31, 20))
    decision = decide(state, [_release_row()], _utc(2026, 10, 31, 21), config=_NO_ENV)
    assert decision.due is False
    assert decision.reason == "waiting_retry"
    assert decision.alert_no_new_period is True


def test_calendar_baseline_missing_but_period_present_is_satisfied() -> None:
    state = _monthly_state(max_period=date(2026, 9, 1), baselines=((RELEASE, None),))
    decision = decide(state, [_release_row()], _utc(2026, 10, 31), config=_NO_ENV)
    assert decision.reason == "satisfied"


def test_calendar_future_row_only_is_not_due() -> None:
    future = CalendarRow(date(2027, 1, 29), None, None)
    decision = decide(_monthly_state(), [future], _utc(2026, 10, 30, 12), config=_NO_ENV)
    assert decision.due is False
    assert decision.alert_no_new_period is False


def test_calendar_missing_falls_back_to_one_poll_per_day() -> None:
    state = _monthly_state(max_period=date(2026, 8, 1))
    first = decide(state, [], _utc(2026, 10, 30, 12), config=_NO_ENV)
    assert first.due is True
    assert first.reason == "calendar_fallback"
    assert first.alert_no_new_period is False
    done = decide(
        _monthly_state(last_success_at=_utc(2026, 10, 30, 8)),
        [],
        _utc(2026, 10, 30, 12),
        config=_NO_ENV,
    )
    assert done.due is False
    assert done.reason == "calendar_fallback"


def test_calendar_missing_fallback_waits_inside_retry_window() -> None:
    state = _monthly_state(
        max_period=date(2026, 8, 1), last_attempt_at=_utc(2026, 10, 30, 13)
    )
    decision = decide(state, [], _utc(2026, 10, 30, 14), config=_NO_ENV)
    assert decision.due is False
    assert decision.reason == "waiting_retry"


def test_calendar_missing_fallback_due_after_retry_window() -> None:
    state = _monthly_state(
        max_period=date(2026, 8, 1), last_attempt_at=_utc(2026, 10, 30, 11)
    )
    decision = decide(state, [], _utc(2026, 10, 30, 14), config=_NO_ENV)
    assert decision.due is True
    assert decision.reason == "calendar_fallback"


def test_calendar_stale_falls_back_and_never_alerts() -> None:
    # Last monthly row ~7 months old, no future row: stale -> one poll/day.
    old = CalendarRow(date(2026, 3, 31), None, None)
    decision = decide(
        _monthly_state(max_period=date(2026, 3, 1)),
        [old],
        _utc(2026, 10, 30),
        config=_NO_ENV,
    )
    assert decision.due is True
    assert decision.reason == "calendar_fallback"
    assert decision.alert_no_new_period is False


# --- calendar-less daily series --------------------------------------------


def _daily_state(**overrides) -> SeriesState:
    values = {
        "external_code": "bie_dkefkytl:TP.DK.USD.A.EF.YTL",
        "frequency": "daily",
        "has_calendar": False,
        "max_period": date(2026, 10, 29),
    }
    values.update(overrides)
    return SeriesState(**values)


def test_daily_weekend_is_not_due() -> None:
    # 2026-10-31 is a Saturday.
    decision = decide(_daily_state(), [], _utc(2026, 10, 31, 14), config=_NO_ENV)
    assert decision.due is False
    assert decision.reason == "weekend"


def test_daily_before_cutoff_is_not_due() -> None:
    # 13:00 UTC = 16:00 Istanbul, before 16:30.
    decision = decide(_daily_state(), [], _utc(2026, 10, 30, 13), config=_NO_ENV)
    assert decision.due is False
    assert decision.reason == "before_cutoff"


def test_daily_after_cutoff_is_due_once() -> None:
    decision = decide(_daily_state(), [], _utc(2026, 10, 30, 14), config=_NO_ENV)
    assert decision.due is True
    assert decision.reason == "daily_due"


def test_daily_already_refreshed_today_is_not_due() -> None:
    state = _daily_state(last_success_at=_utc(2026, 10, 30, 8))
    decision = decide(state, [], _utc(2026, 10, 30, 14), config=_NO_ENV)
    assert decision.due is False
    assert decision.reason == "already_refreshed"


def test_daily_failed_attempt_inside_retry_window_waits() -> None:
    # Retry = 2 h: a failure 1 h ago must not be retried on the next 15-min tick.
    state = _daily_state(last_attempt_at=_utc(2026, 10, 30, 13))
    decision = decide(state, [], _utc(2026, 10, 30, 14), config=_NO_ENV)
    assert decision.due is False
    assert decision.reason == "waiting_retry"


def test_daily_failed_attempt_after_retry_window_is_due() -> None:
    state = _daily_state(last_attempt_at=_utc(2026, 10, 30, 11))
    decision = decide(state, [], _utc(2026, 10, 30, 14), config=_NO_ENV)
    assert decision.due is True
    assert decision.reason == "daily_due"


def test_daily_ahead_dated_period_is_not_stale() -> None:
    state = _daily_state(max_period=date(2026, 10, 31))
    decision = decide(state, [], _utc(2026, 10, 30, 14), config=_NO_ENV)
    assert decision.alert_no_new_period is False


def test_daily_long_gap_does_not_alert() -> None:
    # A 9-day bayram-length gap is below the 10-day stale threshold.
    state = _daily_state(max_period=date(2026, 10, 21))
    decision = decide(state, [], _utc(2026, 10, 30, 14), config=_NO_ENV)
    assert decision.alert_no_new_period is False


def test_daily_truly_stale_alerts() -> None:
    state = _daily_state(max_period=date(2026, 10, 1))
    decision = decide(state, [], _utc(2026, 10, 30, 14), config=_NO_ENV)
    assert decision.alert_no_new_period is True


def test_daily_stale_alerts_even_on_weekend() -> None:
    state = _daily_state(max_period=date(2026, 10, 1))
    decision = decide(state, [], _utc(2026, 10, 31, 14), config=_NO_ENV)
    assert decision.due is False
    assert decision.alert_no_new_period is True


def test_decision_is_deterministic_for_same_inputs() -> None:
    state = _monthly_state()
    now = _utc(2026, 10, 30, 12)
    first = decide(state, [_release_row()], now, config=_NO_ENV)
    second = decide(state, [_release_row()], now, config=_NO_ENV)
    assert first == second


def test_release_friday_grace_lands_on_sunday() -> None:
    # 2026-10-30 is a Friday; grace+2 is Sunday 2026-11-01 (date-only, no weekday rule).
    state = _unsatisfied_state()
    decision = decide(state, [_release_row()], _utc(2026, 11, 1, 0), config=_NO_ENV)
    assert decision.alert_no_new_period is True


def test_retry_spacing_boundary_is_inclusive() -> None:
    at_release = datetime(2026, 10, 29, 21, tzinfo=UTC)
    state = _unsatisfied_state(last_attempt_at=at_release - timedelta(hours=2))
    decision = decide(state, [_release_row()], at_release, config=_NO_ENV)
    assert decision.due is True
