"""Reduce a daily or weekly series to monthly values (frequency matching, DECISIONS §8).

Same rules as the slot-based reduction (``toplam`` / ``ortalama`` / ``donem_sonu`` from the
catalog measure), but the sub-periods are calendar days, so "all sub-periods present" is
defined here (our default, the decision text does not give it): a month counts only when

- it is closed: the series has a later observation (an unfinished month is dropped), and
- it has no hole: the distance from the month start to the first observation, between two
  observations, and from the last observation to the next month start never exceeds
  ``max_gap_days`` (10 days covers weekends and the longest public holiday breaks).
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from app.analysis.errors import UnsupportedSeriesError
from app.analysis.slots import slot_of

MAX_GAP_DAYS = 10
SUPPORTED_RULES = ("toplam", "ortalama", "donem_sonu")


def _next_month_start(day: date) -> date:
    return date(day.year + (day.month == 12), day.month % 12 + 1, 1)


def reduce_to_monthly(
    points: dict[date, float],
    rule: str | None,
    *,
    cumulative: bool,
    key: str,
    max_gap_days: int = MAX_GAP_DAYS,
) -> pd.Series:
    """Monthly slot-indexed values from dated observations (index = monthly slots)."""
    if rule not in SUPPORTED_RULES:
        raise UnsupportedSeriesError(
            key, f"aggregation rule {rule!r} cannot reduce a daily/weekly series to months"
        )
    if cumulative:
        raise UnsupportedSeriesError(key, "cumulative daily/weekly flows are not supported")
    if not points:
        return pd.Series(dtype="float64")
    ordered = sorted(points)
    last_day = ordered[-1]
    by_month: dict[tuple[int, int], list[date]] = {}
    for day in ordered:
        by_month.setdefault((day.year, day.month), []).append(day)
    out: dict[int, float] = {}
    for (year, month), days in by_month.items():
        start = date(year, month, 1)
        following = _next_month_start(start)
        if last_day < following:
            continue  # the month is still open
        edges = [start, *days, following]
        if any(
            (edges[i + 1] - edges[i]) > timedelta(days=max_gap_days) for i in range(len(edges) - 1)
        ):
            continue
        values = [points[day] for day in days]
        if rule == "toplam":
            value = float(sum(values))
        elif rule == "ortalama":
            value = float(sum(values) / len(values))
        else:
            value = float(values[-1])
        out[slot_of(start, 1)] = value
    return pd.Series(out, dtype="float64").sort_index()
