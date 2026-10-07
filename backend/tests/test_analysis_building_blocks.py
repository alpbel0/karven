"""Frequency matching, transforms, deflating, windows and the status table."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from app.analysis.engine import derive_status
from app.analysis.errors import InvalidSpecError, UnsupportedSeriesError
from app.analysis.frequency import aggregate, decumulate
from app.analysis.models import (
    PERIOD_ALL,
    PERIOD_RECENT,
    RESULT_INSUFFICIENT,
    RESULT_SUPPORTED,
    RESULT_UNSUPPORTED,
    RelationSpec,
    WindowResult,
)
from app.analysis.slots import date_of, slot_of
from app.analysis.transforms import apply_transform, deflate, shift_for
from app.analysis.windows import longest_block, window_slots


def monthly(values: list[float], first_month_index: int = 0) -> pd.Series:
    """Monthly slots starting at 2020-01 + ``first_month_index`` months."""
    start = slot_of(date(2020, 1, 1), 1) + first_month_index
    return pd.Series(values, index=range(start, start + len(values)), dtype="float64")


def test_slots_round_trip_and_quarter_alignment():
    assert date_of(slot_of(date(2026, 8, 1), 1), 1) == date(2026, 8, 1)
    assert slot_of(date(2026, 4, 1), 3) == slot_of(date(2026, 6, 1), 3)
    assert date_of(slot_of(date(2026, 5, 1), 3), 3) == date(2026, 4, 1)


def test_aggregate_mean_sum_last_need_complete_periods():
    series = monthly([1, 2, 3, 4, 5, 6, 7, 8])  # Jan..Aug 2020: Q1, Q2 complete, Q3 has 2 months
    mean = aggregate(series, 1, 3, "ortalama", cumulative=False, key="k")
    total = aggregate(series, 1, 3, "toplam", cumulative=False, key="k")
    last = aggregate(series, 1, 3, "donem_sonu", cumulative=False, key="k")
    assert list(mean) == [2.0, 5.0]
    assert list(total) == [6.0, 15.0]
    assert list(last) == [3.0, 6.0]
    assert len(mean) == 2  # the incomplete third quarter is dropped, not summed over 2 months


def test_aggregate_drops_a_period_with_a_missing_month():
    series = monthly([1, 2, 3, 4, 5, 6]).drop(index=slot_of(date(2020, 2, 1), 1))
    total = aggregate(series, 1, 3, "toplam", cumulative=False, key="k")
    assert list(total) == [15.0]  # Q1 has a hole and is dropped


def test_aggregate_cumulative_flow_is_decumulated_before_summing():
    # running total within the year: 10, 30, 60 (Jan, Feb, Mar) -> monthly 10, 20, 30
    series = monthly([10, 30, 60])
    assert list(decumulate(series, 1)) == [10.0, 20.0, 30.0]
    total = aggregate(series, 1, 3, "toplam", cumulative=True, key="k")
    assert list(total) == [60.0]


def test_aggregate_rejects_rules_that_cannot_reduce_frequency():
    with pytest.raises(UnsupportedSeriesError):
        aggregate(monthly([1, 2, 3]), 1, 3, "yeniden_hesapla", cumulative=False, key="k")
    with pytest.raises(UnsupportedSeriesError):
        aggregate(monthly([1, 2, 3]), 3, 1, "ortalama", cumulative=False, key="k")


def test_same_frequency_is_returned_untouched():
    series = monthly([1, 2, 3])
    assert aggregate(series, 1, 1, "yeniden_hesapla", cumulative=False, key="k") is series


def test_annual_pct_change_uses_the_same_month_last_year():
    levels = monthly([100.0] * 12 + [110.0] + [100.0] * 11)
    changes = apply_transform(levels, "annual_pct_change", 1, key="k")
    assert round(float(changes.iloc[0]), 6) == 10.0  # month 13 vs month 1
    assert len(changes) == 12


def test_period_pct_change_and_difference():
    levels = monthly([100.0, 110.0, 121.0])
    pct = apply_transform(levels, "period_pct_change", 1, key="k")
    diff = apply_transform(levels, "difference", 1, key="k")
    assert [round(float(v), 6) for v in pct] == [10.0, 10.0]
    assert [round(float(v), 6) for v in diff] == [10.0, 11.0]


def test_transform_never_bridges_a_calendar_gap():
    levels = monthly([100.0, 110.0, 120.0, 130.0]).drop(index=slot_of(date(2020, 3, 1), 1))
    changes = apply_transform(levels, "difference", 1, key="k")
    assert slot_of(date(2020, 3, 1), 1) not in changes.index  # no value for the hole
    assert slot_of(date(2020, 4, 1), 1) not in changes.index  # and none that spans it


def test_pct_change_rejects_non_positive_levels():
    with pytest.raises(UnsupportedSeriesError):
        apply_transform(monthly([100.0, -5.0, 3.0]), "annual_pct_change", 1, key="k")
    # the difference transform is the allowed route for such series
    assert len(apply_transform(monthly([100.0, -5.0, 3.0]), "difference", 1, key="k")) == 2


def test_shift_for_each_frequency():
    assert shift_for("annual_pct_change", 1) == 12
    assert shift_for("annual_pct_change", 3) == 4
    assert shift_for("annual_pct_change", 12) == 1
    assert shift_for("period_pct_change", 3) == 1
    with pytest.raises(InvalidSpecError):
        shift_for("log", 1)


def test_deflate_divides_by_cpi_and_keeps_only_overlap():
    values = monthly([200.0, 400.0, 600.0])
    cpi = monthly([100.0, 200.0], first_month_index=0)
    real = deflate(values, 1, cpi, 1, key="v", cpi_key="cpi")
    assert list(real) == [2.0, 2.0]


def test_deflate_monthly_cpi_into_quarterly_series_uses_the_quarter_mean():
    quarterly = pd.Series([300.0], index=[slot_of(date(2020, 1, 1), 3)])
    cpi = monthly([100.0, 100.0, 100.0])
    assert list(deflate(quarterly, 3, cpi, 1, key="v", cpi_key="cpi")) == [3.0]


def test_longest_block_does_not_bridge_gaps_or_missing_values():
    frame = pd.DataFrame({"a": [1, 2, 3, np.nan, 5, 6, 7, 8, 9, 10]}, index=range(10))
    block = longest_block(frame)
    assert list(block.index) == [4, 5, 6, 7, 8, 9]
    gapped = pd.DataFrame({"a": [1.0] * 8}, index=[0, 1, 2, 5, 6, 7, 8, 9])
    assert list(longest_block(gapped).index) == [5, 6, 7, 8, 9]


def test_window_bounds():
    assert window_slots(PERIOD_ALL, 1) == (None, None)
    low, high = window_slots(PERIOD_RECENT, 1)
    assert date_of(low, 1) == date(2017, 1, 1) and date_of(high, 1) == date(2026, 12, 1)
    low_q, high_q = window_slots(PERIOD_RECENT, 3)
    assert date_of(low_q, 3) == date(2017, 1, 1) and date_of(high_q, 3) == date(2026, 10, 1)


def window(result: str, period: str = PERIOD_ALL) -> WindowResult:
    return WindowResult(period=period, result=result)


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        (RESULT_SUPPORTED, RESULT_SUPPORTED, "supported"),
        (RESULT_UNSUPPORTED, RESULT_UNSUPPORTED, "unsupported"),
        (RESULT_SUPPORTED, RESULT_UNSUPPORTED, "periods_differ"),
        (RESULT_UNSUPPORTED, RESULT_SUPPORTED, "periods_differ"),
        (RESULT_INSUFFICIENT, RESULT_SUPPORTED, "insufficient_data"),
        (RESULT_UNSUPPORTED, RESULT_INSUFFICIENT, "insufficient_data"),
        (RESULT_INSUFFICIENT, RESULT_INSUFFICIENT, "insufficient_data"),
    ],
)
def test_status_table(first, second, expected):
    windows = {PERIOD_ALL: window(first, PERIOD_ALL), PERIOD_RECENT: window(second, PERIOD_RECENT)}
    assert derive_status(windows) == expected


def test_relation_spec_validation():
    ok = RelationSpec("t", {"d": "positive"}, 0, 2, "difference")
    assert ok.drivers == ("d",)
    for bad in (
        dict(target="t", driver_directions={}, lag_min=0, lag_max=1, transform="difference"),
        dict(
            target="t",
            driver_directions={"t": "positive"},
            lag_min=0,
            lag_max=1,
            transform="difference",
        ),
        dict(
            target="t", driver_directions={"d": "up"}, lag_min=0, lag_max=1, transform="difference"
        ),
        dict(
            target="t",
            driver_directions={"d": "positive"},
            lag_min=2,
            lag_max=1,
            transform="difference",
        ),
        dict(
            target="t", driver_directions={"d": "positive"}, lag_min=0, lag_max=1, transform="log"
        ),
        dict(
            target="t",
            driver_directions={"d": "positive"},
            lag_min=0,
            lag_max=1,
            transform="difference",
            nominal_tl_series=("x",),
        ),
    ):
        with pytest.raises(InvalidSpecError):
            RelationSpec(**bad)
