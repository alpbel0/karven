"""Period slots: integer positions on a regular calendar grid.

A *slot* is ``(year * 12 + month - 1) // step`` where ``step`` is the period length in
months (monthly 1, quarterly 3, semiannual 6, annual 12). Consecutive periods have
consecutive slots, so contiguity, lags and shifts are plain integer arithmetic and a
calendar gap can never be mistaken for two adjacent periods (DECISIONS §8, task 3.2).
"""

from __future__ import annotations

from datetime import date

FREQUENCY_STEP: dict[str, int] = {"monthly": 1, "quarterly": 3, "semiannual": 6, "annual": 12}
STEP_FREQUENCY: dict[int, str] = {step: name for name, step in FREQUENCY_STEP.items()}


def frequency_step(frequency: str) -> int | None:
    """Months per period, or ``None`` for a frequency the engine does not test."""
    return FREQUENCY_STEP.get(frequency)


def slot_of(day: date, step: int) -> int:
    return (day.year * 12 + day.month - 1) // step


def date_of(slot: int, step: int) -> date:
    month_index = slot * step
    return date(month_index // 12, month_index % 12 + 1, 1)
