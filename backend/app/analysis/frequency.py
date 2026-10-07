"""Frequency matching (DECISIONS §8): reduce a series to a lower frequency.

The rule comes from the catalog (``measure_combinations.aggregation``):
``toplam`` (sum of a flow), ``ortalama`` (mean of a price/rate/index), ``donem_sonu``
(last value of a stock). A target period exists only when **all** its sub-periods are
present (an incomplete period is dropped, user decision 2026-10-05): a sum over two of
three months would otherwise look like a fake fall. Cumulative flows are turned into
period values before they are summed. Percent-change series cannot be aggregated
(``yeniden_hesapla``: they would have to be recomputed from the level) and are rejected.
"""

from __future__ import annotations

import pandas as pd

from app.analysis.errors import UnsupportedSeriesError

SUPPORTED_RULES = ("toplam", "ortalama", "donem_sonu")


def decumulate(values: pd.Series, step: int) -> pd.Series:
    """Turn a within-year running total into period values.

    The first period of a year keeps its value; any later period needs its direct
    predecessor in the same year, otherwise it becomes missing.
    """
    out = {}
    for slot, value in values.items():
        first_of_year = (slot * step) % 12 == 0
        if first_of_year:
            out[slot] = value
        elif (slot - 1) in values.index:
            out[slot] = value - values.loc[slot - 1]
        else:
            out[slot] = float("nan")
    return pd.Series(out, dtype="float64")


def aggregate(
    values: pd.Series,
    from_step: int,
    to_step: int,
    rule: str | None,
    *,
    cumulative: bool,
    key: str,
) -> pd.Series:
    """Reduce ``values`` (index = slots at ``from_step``) to ``to_step``."""
    if to_step == from_step:
        return values
    if to_step < from_step or to_step % from_step != 0:
        raise UnsupportedSeriesError(
            key, f"cannot reduce {from_step}-month to {to_step}-month periods"
        )
    if rule not in SUPPORTED_RULES:
        raise UnsupportedSeriesError(
            key,
            f"aggregation rule {rule!r} cannot reduce frequency "
            "(percent-change, weight and distribution series are not aggregated)",
        )
    ratio = to_step // from_step
    data = decumulate(values, from_step) if cumulative and rule == "toplam" else values
    data = data.dropna()
    if data.empty:
        return pd.Series(dtype="float64")
    target_slot = (data.index.to_series() * from_step) // to_step
    groups = data.groupby(target_slot.to_numpy())
    counts = groups.size()
    complete = counts[counts == ratio].index
    if rule == "toplam":
        reduced = groups.sum()
    elif rule == "ortalama":
        reduced = groups.mean()
    else:
        reduced = groups.last()
    reduced = reduced.loc[reduced.index.isin(complete)]
    reduced.index = reduced.index.astype("int64")
    return reduced.astype("float64")
