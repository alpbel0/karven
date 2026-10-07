"""Test windows and the longest contiguous block (decision: gaps are never filled)."""

from __future__ import annotations

from datetime import date

import pandas as pd

from app.analysis.models import PERIOD_ALL, PERIOD_RECENT
from app.analysis.slots import slot_of


def window_slots(period: str, step: int) -> tuple[int | None, int | None]:
    """First and last target slot of a window (``None`` = unbounded)."""
    if period == PERIOD_ALL:
        return None, None
    if period == PERIOD_RECENT:
        return slot_of(date(2017, 1, 1), step), slot_of(date(2026, 12, 1), step)
    raise ValueError(f"unknown period {period!r}")


def longest_block(frame: pd.DataFrame) -> pd.DataFrame:
    """The longest run of consecutive slots in which no column is missing.

    ``frame`` is indexed by integer slots. A row with a missing value breaks the run, and so
    does a missing slot, because the grid is rebuilt over the full range first.
    """
    if frame.empty:
        return frame
    grid = frame.reindex(range(int(frame.index.min()), int(frame.index.max()) + 1))
    complete = grid.notna().all(axis=1).to_numpy()
    best_start = best_len = 0
    start: int | None = None
    for position, ok in enumerate(complete):
        if ok:
            if start is None:
                start = position
            length = position - start + 1
            if length > best_len:
                best_start, best_len = start, length
        else:
            start = None
    if best_len == 0:
        return grid.iloc[0:0]
    return grid.iloc[best_start : best_start + best_len]
