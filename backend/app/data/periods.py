"""Period normalization and alignment for series frequencies.

A series period is always the period *start* date: monthly Sep 2024 ->
2024-09-01, quarterly Q3 2024 -> 2024-07-01, annual 2024 -> 2024-01-01. Daily
observations keep the day and weekly observations keep the week start date the
source reports, so those two frequencies have no alignment rule to enforce.
``biennial`` is a survey published every second year (e.g. the TÜİK waste and
waste-water surveys, SDMX ``A2``): its period start is January 1 like ``annual``;
which years exist is the source's business, so no even/odd rule is enforced.
``irregular`` covers sources with no fixed cadence (e.g. election results): its
period is the real event date and is stored verbatim.
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
BIENNIAL = "biennial"
IRREGULAR = "irregular"

FREQUENCIES = (DAILY, WEEKLY, MONTHLY, QUARTERLY, SEMIANNUAL, ANNUAL, BIENNIAL, IRREGULAR)

_FIRST_OF_QUARTER = (1, 4, 7, 10)
_FIRST_OF_HALF = (1, 7)

# Frequencies that store the source's period start verbatim (no alignment rule).
_UNCHECKED = (DAILY, WEEKLY, IRREGULAR)

_FREQUENCY_BY_INITIAL = {
    "D": DAILY,
    "W": WEEKLY,
    "M": MONTHLY,
    "Q": QUARTERLY,
    "S": SEMIANNUAL,
    "A": ANNUAL,
}

# Whole codes that must win over the first-letter rule (``A2`` is biennial, not annual).
_FREQUENCY_BY_CODE = {
    "A2": BIENNIAL,
}


def frequency_for_sdmx_code(code: str) -> str:
    """Map an SDMX frequency code (``M``, ``Q``, ``A``, ``A2``...) to our vocabulary."""
    text = str(code).strip().upper()
    explicit = _FREQUENCY_BY_CODE.get(text)
    if explicit is not None:
        return explicit
    frequency = _FREQUENCY_BY_INITIAL.get(text[:1])
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
    else:  # ANNUAL and BIENNIAL
        aligned = value.month == 1 and value.day == 1
        expected = "Jan 1"

    if not aligned:
        raise PeriodError(f"{frequency} period must start on {expected}; got {value.isoformat()}")
    return value
