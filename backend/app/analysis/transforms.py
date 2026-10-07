"""Hypothesis transforms and inflation adjustment (DECISIONS §8).

The test never runs on raw levels. ``annual_pct_change`` compares with the same period
one year earlier, ``period_pct_change`` with the previous period, ``difference`` is the
first difference (for rates such as interest or unemployment). All three are computed on
the full regular grid, so a calendar gap yields a missing value instead of a wrong shift.
Nominal TL series are divided by the CPI level first.
"""

from __future__ import annotations

import pandas as pd

from app.analysis.errors import InvalidSpecError, UnsupportedSeriesError
from app.analysis.frequency import aggregate

TRANSFORMS = ("annual_pct_change", "period_pct_change", "difference")


def full_grid(values: pd.Series) -> pd.Series:
    """Reindex to every slot between the first and last observation (gaps become NaN)."""
    if values.empty:
        return values
    return values.reindex(range(int(values.index.min()), int(values.index.max()) + 1))


def shift_for(transform: str, step: int) -> int:
    if transform == "annual_pct_change":
        return max(12 // step, 1)
    if transform in ("period_pct_change", "difference"):
        return 1
    raise InvalidSpecError(f"unknown transform {transform!r}")


def apply_transform(values: pd.Series, transform: str, step: int, *, key: str) -> pd.Series:
    """Apply the transform to ``values`` (index = slots at ``step``)."""
    shift = shift_for(transform, step)
    grid = full_grid(values.astype("float64"))
    previous = grid.shift(shift)
    if transform == "difference":
        return (grid - previous).dropna()
    if (grid.dropna() <= 0).any():
        raise UnsupportedSeriesError(
            key, "percent change needs strictly positive levels; use the difference transform"
        )
    return ((grid / previous - 1.0) * 100.0).dropna()


def deflate(
    values: pd.Series,
    step: int,
    cpi: pd.Series,
    cpi_step: int,
    *,
    key: str,
    cpi_key: str,
) -> pd.Series:
    """Divide a nominal series by the CPI level (the base cancels in every transform)."""
    cpi_at_step = aggregate(cpi, cpi_step, step, "ortalama", cumulative=False, key=cpi_key)
    joined = pd.concat([values.rename("v"), cpi_at_step.rename("c")], axis=1).dropna()
    if joined.empty:
        raise UnsupportedSeriesError(key, "no period overlaps the CPI series")
    return joined["v"] / joined["c"]
