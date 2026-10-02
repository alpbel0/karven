"""Period normalization and alignment for series frequencies.

A series period is always the period *start* date: monthly Sep 2024 ->
2024-09-01, quarterly Q3 2024 -> 2024-07-01, annual 2024 -> 2024-01-01. Daily
observations keep the day and weekly observations keep the week start date the
source reports, so those two frequencies have no alignment rule to enforce.
"""

from __future__ import annotations

from datetime import date, datetime

from app.data.errors import PeriodError

DAILY = "daily"
WEEKLY = "weekly"
MONTHLY = "monthly"
QUARTERLY = "quarterly"
SEMIANNUAL = "semiannual"
ANNUAL = "annual"

FREQUENCIES = (DAILY, WEEKLY, MONTHLY, QUARTERLY, SEMIANNUAL, ANNUAL)

_FIRST_OF_QUARTER = (1, 4, 7, 10)
_FIRST_OF_HALF = (1, 7)

# Frequencies that store the source's period start verbatim (no alignment rule).
_UNCHECKED = (DAILY, WEEKLY)

_FREQUENCY_BY_INITIAL = {
    "D": DAILY,
    "W": WEEKLY,
    "M": MONTHLY,
    "Q": QUARTERLY,
    "S": SEMIANNUAL,
    "A": ANNUAL,
}


def frequency_for_sdmx_code(code: str) -> str:
    """Map an SDMX frequency code (``M``, ``Q``, ``A``, ``A2``...) to our vocabulary."""
    initial = str(code).strip()[:1].upper()
    frequency = _FREQUENCY_BY_INITIAL.get(initial)
    if frequency is None:
        raise PeriodError(f"unknown frequency code {code!r}")
    return frequency


def normalize_period(frequency: str, value: date) -> date:
    """Return ``value`` if it is a valid period start for ``frequency``.

    Raises :class:`PeriodError` when the date is misaligned or the frequency is
    unknown. The date is never rewritten: callers must pass the period start.
    """
    if frequency not in FREQUENCIES:
        raise PeriodError(f"unknown frequency {frequency!r}")

    if isinstance(value, datetime) or not isinstance(value, date):
        raise PeriodError(f"period must be a date, got {value!r}")

    if frequency in _UNCHECKED:
        return value

    if frequency == MONTHLY:
        aligned = value.day == 1
        expected = "day 1"
    elif frequency == QUARTERLY:
        aligned = value.day == 1 and value.month in _FIRST_OF_QUARTER
        expected = "Jan/Apr/Jul/Oct day 1"
    elif frequency == SEMIANNUAL:
        aligned = value.day == 1 and value.month in _FIRST_OF_HALF
        expected = "Jan/Jul day 1"
    else:  # ANNUAL
        aligned = value.month == 1 and value.day == 1
        expected = "Jan 1"

    if not aligned:
        raise PeriodError(f"{frequency} period must start on {expected}; got {value.isoformat()}")
    return value
