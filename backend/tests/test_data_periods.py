"""Unit tests for period alignment (no database required)."""

from datetime import date, datetime

import pytest

from app.data.errors import PeriodError
from app.data.periods import (
    ANNUAL,
    DAILY,
    MONTHLY,
    QUARTERLY,
    SEMIANNUAL,
    WEEKLY,
    normalize_period,
)


@pytest.mark.parametrize(
    ("frequency", "value"),
    [
        (DAILY, date(2024, 9, 15)),
        (WEEKLY, date(2024, 9, 2)),
        (MONTHLY, date(2024, 9, 1)),
        (QUARTERLY, date(2024, 7, 1)),
        (SEMIANNUAL, date(2024, 7, 1)),
        (ANNUAL, date(2024, 1, 1)),
    ],
)
def test_valid_period_is_returned_unchanged(frequency: str, value: date) -> None:
    assert normalize_period(frequency, value) == value


@pytest.mark.parametrize(
    ("frequency", "value"),
    [
        (MONTHLY, date(2024, 9, 15)),
        (MONTHLY, date(2024, 9, 2)),
        (QUARTERLY, date(2024, 2, 1)),
        (QUARTERLY, date(2024, 4, 2)),
        (SEMIANNUAL, date(2024, 4, 1)),
        (ANNUAL, date(2024, 2, 1)),
    ],
)
def test_misaligned_period_raises(frequency: str, value: date) -> None:
    with pytest.raises(PeriodError):
        normalize_period(frequency, value)


def test_daily_and_weekly_accept_any_day() -> None:
    assert normalize_period(DAILY, date(2024, 9, 15)) == date(2024, 9, 15)
    assert normalize_period(WEEKLY, date(2024, 9, 4)) == date(2024, 9, 4)


def test_unknown_frequency_raises() -> None:
    with pytest.raises(PeriodError):
        normalize_period("hourly", date(2024, 9, 1))


def test_datetime_instead_of_date_is_rejected() -> None:
    with pytest.raises(PeriodError):
        normalize_period(MONTHLY, datetime(2024, 9, 1))  # type: ignore[arg-type]
