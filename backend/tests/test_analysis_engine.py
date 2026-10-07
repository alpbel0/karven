"""The relation test engine on synthetic data with known results (Task 3.2 acceptance)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import numpy as np

from app.analysis.engine import run_relation_test
from app.analysis.models import (
    PERIOD_ALL,
    PERIOD_RECENT,
    RelationSpec,
    SeriesData,
    TestReport,
    Unsupported,
)
from app.analysis.runner import to_graph_period_results
from app.analysis.significance import UNCALIBRATED_REASON, FitContext, HacOls
from app.analysis.slots import slot_of
from tests.analysis_helpers import (
    independent_pair,
    linked_pair,
    random_walk,
    series_from_levels,
)

SPEC = RelationSpec("tcmb|TGT", {"tcmb|DRV": "positive"}, 0, 3, "difference")
CALIBRATED = HacOls(calibrated=True)


def test_strong_relation_is_supported_in_both_windows_with_the_right_lag():
    target, driver = linked_pair()
    report = run_relation_test(target, [driver], SPEC, method=CALIBRATED)
    assert isinstance(report, TestReport)
    assert report.status == "supported"
    for period in (PERIOD_ALL, PERIOD_RECENT):
        window = report.windows[period]
        assert window.result == "supported"
        assert window.best_lag == 1
        assert window.p_value < 0.001
        assert window.statistic_name == "pearson_r"
        assert window.statistic_value > 0.5
        assert window.reliable is True
    assert report.windows[PERIOD_RECENT].n_obs < report.windows[PERIOD_ALL].n_obs
    assert report.windows[PERIOD_RECENT].start >= date(2017, 1, 1)
    assert report.windows[PERIOD_RECENT].end <= date(2026, 12, 1)


def test_uncalibrated_method_marks_every_result_unreliable_with_a_reason():
    target, driver = linked_pair()
    report = run_relation_test(target, [driver], SPEC)  # default HacOls is not calibrated
    assert report.status == "supported"
    assert report.reliable is False
    for window in report.windows.values():
        assert window.reliable is False
        assert window.reliability_reason == UNCALIBRATED_REASON


def test_noise_is_rarely_supported():
    """Independent random walks: a 'supported' window is a false positive.

    The lag search picks the best of four lags without a correction (decision 2026-10-05),
    so a few false positives are expected; the calibration study measures the real rate.
    This guards against a gross bug (e.g. a sign error) that would make noise look real.
    """
    windows = supported = 0
    for seed in range(30):
        target, driver = independent_pair(seed=seed)
        report = run_relation_test(target, [driver], SPEC, method=CALIBRATED)
        for window in report.windows.values():
            windows += 1
            supported += window.result == "supported"
    assert supported / windows < 0.15


def test_a_reversed_direction_is_unsupported_even_when_the_link_is_strong():
    target, driver = linked_pair()
    spec = replace(SPEC, driver_directions={"tcmb|DRV": "negative"})
    report = run_relation_test(target, [driver], spec, method=CALIBRATED)
    assert report.status == "unsupported"
    assert (
        report.windows[PERIOD_ALL].statistic_value > 0
    )  # the data are positive, the claim negative


def test_a_short_series_is_insufficient_data():
    target, driver = linked_pair(n=20)
    report = run_relation_test(target, [driver], SPEC, method=CALIBRATED)
    assert report.status == "insufficient_data"
    assert all(window.result == "insufficient_data" for window in report.windows.values())
    assert report.windows[PERIOD_ALL].n_obs < 36


def test_the_recent_window_can_be_insufficient_while_all_years_is_decided():
    target, driver = linked_pair(n=200)  # 2005-01 .. 2021-08: only 56 months fall in 2017+
    spec = replace(SPEC, lag_max=2)
    report = run_relation_test(target, [driver], spec, method=CALIBRATED)
    assert report.windows[PERIOD_ALL].result == "supported"
    assert report.windows[PERIOD_RECENT].n_obs >= 36
    short_target, short_driver = linked_pair(n=170)
    shorter = run_relation_test(short_target, [short_driver], spec, method=CALIBRATED)
    # 170 months end 2019-02: only 26 months in 2017+ -> that window is insufficient,
    # and any insufficient window makes the whole relation insufficient (no partial verdict)
    assert shorter.windows[PERIOD_RECENT].result == "insufficient_data"
    assert shorter.status == "insufficient_data"


def drop_months(series: SeriesData, months: list[int]) -> SeriesData:
    """Remove the months at these positions (0 = first month) to create calendar gaps."""
    slots = [series.values.index[position] for position in months]
    return replace(series, values=series.values.drop(index=slots))


def test_gaps_are_never_bridged_the_longest_contiguous_block_is_used():
    target, driver = linked_pair(n=252)
    gapped = drop_months(target, [100])  # one hole in the middle of 2005-01 .. 2025-12
    report = run_relation_test(gapped, [driver], SPEC, method=CALIBRATED)
    all_years = report.windows[PERIOD_ALL]
    assert all_years.result == "supported"
    # the longest block is the part after the hole (or before it), never the whole series
    assert all_years.n_obs < 251
    assert all_years.n_obs >= 150


def test_many_gaps_leave_no_block_long_enough():
    target, driver = linked_pair(n=252)
    gapped = drop_months(target, list(range(25, 252, 30)))  # a hole every 30 months
    report = run_relation_test(gapped, [driver], SPEC, method=CALIBRATED)
    assert report.status == "insufficient_data"
    assert report.windows[PERIOD_ALL].n_obs < 36


def regression_data(seed: int = 3, n: int = 252):
    rng = np.random.default_rng(seed)
    inc1 = rng.normal(size=n)
    inc2 = rng.normal(size=n)
    target_inc = rng.normal(scale=0.5, size=n)
    target_inc[1:] += 0.6 * inc1[:-1]
    target_inc[2:] -= 0.5 * inc2[:-2]
    target = series_from_levels("tcmb|TGT", random_walk(rng, target_inc))
    d1 = series_from_levels("tcmb|D1", random_walk(rng, inc1))
    d2 = series_from_levels("tcmb|D2", random_walk(rng, inc2))
    return target, d1, d2


def test_regression_with_two_drivers_uses_each_drivers_own_lag_and_direction():
    target, d1, d2 = regression_data()
    spec = RelationSpec(
        "tcmb|TGT", {"tcmb|D1": "positive", "tcmb|D2": "negative"}, 0, 3, "difference"
    )
    report = run_relation_test(target, [d1, d2], spec, method=CALIBRATED)
    assert report.status == "supported"
    window = report.windows[PERIOD_ALL]
    assert window.statistic_name == "r_squared"
    assert window.details["lags"] == {"tcmb|D1": 1, "tcmb|D2": 2}
    assert window.details["coefficients"]["tcmb|D1"] > 0
    assert window.details["coefficients"]["tcmb|D2"] < 0


def test_regression_needs_every_coefficient_in_its_own_direction():
    target, d1, d2 = regression_data()
    spec = RelationSpec(
        "tcmb|TGT", {"tcmb|D1": "positive", "tcmb|D2": "positive"}, 0, 3, "difference"
    )
    report = run_relation_test(target, [d1, d2], spec, method=CALIBRATED)
    assert report.status == "unsupported"  # D2 really acts negatively


def test_regression_minimum_observations_scale_with_the_number_of_drivers():
    target, d1, d2 = regression_data(n=60)  # 36 would do for one driver, two need 72
    spec = RelationSpec(
        "tcmb|TGT", {"tcmb|D1": "positive", "tcmb|D2": "negative"}, 0, 3, "difference"
    )
    report = run_relation_test(target, [d1, d2], spec, method=CALIBRATED)
    assert report.status == "insufficient_data"
    assert report.windows[PERIOD_ALL].details["needed"] == 72


def test_mixed_frequencies_are_reduced_to_the_lowest_one():
    rng = np.random.default_rng(11)
    months = 12 * 20
    level = random_walk(rng, rng.normal(size=months))
    driver = series_from_levels("tcmb|DRV", level)  # monthly index level, rule: mean
    quarterly_mean = level.reshape(-1, 3).mean(axis=1)
    delta = np.diff(quarterly_mean, prepend=quarterly_mean[0])
    target_inc = rng.normal(scale=0.4, size=len(delta))
    target_inc[1:] += 0.9 * delta[:-1]
    first_quarter = slot_of(date(2005, 1, 1), 3)
    target = series_from_levels(
        "tcmb|TGT", random_walk(rng, target_inc), step=3, start_slot=first_quarter
    )
    spec = RelationSpec("tcmb|TGT", {"tcmb|DRV": "positive"}, 0, 2, "difference")
    report = run_relation_test(target, [driver], spec, method=CALIBRATED)
    assert isinstance(report, TestReport)
    window = report.windows[PERIOD_ALL]
    assert window.result == "supported"
    assert window.best_lag == 1
    assert window.n_obs <= 80
    assert window.start.month in (1, 4, 7, 10)  # quarter starts


def test_unsupported_series_are_reported_not_tested():
    target, driver = linked_pair()
    weight = replace(driver, aggregation="test_disi")
    result = run_relation_test(target, [weight], SPEC)
    assert isinstance(result, Unsupported)
    assert "tcmb|DRV" in result.reasons

    nominal = replace(SPEC, nominal_tl_series=("tcmb|DRV",))
    no_deflator = run_relation_test(target, [driver], nominal)
    assert isinstance(no_deflator, Unsupported)
    assert "deflator" in no_deflator.reasons["tcmb|DRV"]


def test_percent_change_levels_must_be_positive():
    rng = np.random.default_rng(5)
    target = series_from_levels("tcmb|TGT", rng.normal(size=100))  # crosses zero
    driver = series_from_levels("tcmb|DRV", random_walk(rng, rng.normal(size=100)))
    spec = replace(SPEC, transform="annual_pct_change")
    result = run_relation_test(target, [driver], spec)
    assert isinstance(result, Unsupported)
    assert "positive" in result.reasons["tcmb|TGT"]


def test_nominal_series_are_made_real_with_the_cpi_before_testing():
    target, driver = linked_pair()
    cpi_values = 100.0 * np.exp(np.linspace(0.0, 1.5, len(driver.values)))  # strong inflation
    cpi = series_from_levels("tuik|CPI", cpi_values)
    nominal_driver = replace(driver, values=driver.values * cpi.values.to_numpy())
    # Divided by the CPI again, the driver is the original real series and the link reappears.
    spec = replace(SPEC, nominal_tl_series=("tcmb|DRV",))
    report = run_relation_test(target, [nominal_driver], spec, deflator=cpi, method=CALIBRATED)
    assert report.status == "supported"
    # Without the deflation the common trend would corrupt the series: the engine must not
    # silently skip it (there is no deflator -> Unsupported, covered above).


def test_annual_pct_change_on_monthly_data_runs_with_the_overlap_horizon_lags():
    rng = np.random.default_rng(21)
    n = 240
    driver_level = 100.0 * np.exp(np.cumsum(rng.normal(scale=0.01, size=n)))
    target_level = 100.0 * np.exp(np.cumsum(rng.normal(scale=0.01, size=n)))
    spec = RelationSpec("tcmb|TGT", {"tcmb|DRV": "positive"}, 0, 2, "annual_pct_change")
    report = run_relation_test(
        series_from_levels("tcmb|TGT", target_level),
        [series_from_levels("tcmb|DRV", driver_level)],
        spec,
        method=CALIBRATED,
    )
    window = report.windows[PERIOD_ALL]
    assert window.details["settings"]["maxlags"] == 11  # 12 // 1 - 1 overlapping periods
    # one common date set for every lag: the edge loss is lag_max periods, not the chosen lag
    assert window.n_obs == n - 12 - spec.lag_max


def test_hac_lag_correction_multiplies_p_values_by_the_lags_searched():
    rng = np.random.default_rng(2)
    y = rng.normal(size=100)
    x = y * 0.2 + rng.normal(size=100)
    context = FitContext("difference", 1, lags_searched=4)
    plain = HacOls().fit(y, x, context)
    corrected = HacOls(lag_correction=True).fit(y, x, context)
    assert corrected.p_values[1] == min(1.0, plain.p_values[1] * 4)
    assert corrected.settings["lag_correction"] is True
    assert HacOls(maxlags=7).fit(y, x, context).settings["maxlags"] == 7


def test_report_converts_to_graph_period_results_with_the_reliability_fields():
    target, driver = linked_pair()
    report = run_relation_test(target, [driver], SPEC)  # uncalibrated -> unreliable
    results = to_graph_period_results(report)
    assert [item.period for item in results] == [PERIOD_ALL, PERIOD_RECENT]
    for item in results:
        assert item.result == "supported"
        assert item.transform == "difference"
        assert item.reliable is False
        assert item.reliability_reason == UNCALIBRATED_REASON
        assert item.start is not None and item.details.startswith("{")


def test_lags_are_compared_on_one_common_date_set():
    """Every candidate lag is scored on the same sample: n_obs does not depend on the best lag."""
    target, driver = linked_pair(n=200)
    counts = set()
    for lag_max in (1, 3):
        spec = replace(SPEC, lag_max=lag_max)
        report = run_relation_test(target, [driver], spec, method=CALIBRATED)
        counts.add((lag_max, report.windows[PERIOD_ALL].n_obs))
    # the usable sample shrinks by exactly the extra lag range (200 months, difference transform)
    assert counts == {(1, 200 - 1 - 1), (3, 200 - 1 - 3)}


def test_gate_features_are_always_stored_and_the_gate_can_mark_a_result_unreliable():
    from app.analysis.engine import EngineConfig
    from app.analysis.gate import GateThresholds

    target, driver = linked_pair()
    report = run_relation_test(target, [driver], SPEC, method=CALIBRATED)
    features = report.windows[PERIOD_ALL].details["features"]
    assert set(features) == {
        "n",
        "rho1",
        "rho_seasonal",
        "rho_seasonal24",
        "var_ratio",
        "mean_shift",
    }
    assert report.windows[PERIOD_ALL].reliable is True  # no gate configured

    strict = GateThresholds(
        n_min=10_000, rho1_max=0.9, rho_seasonal_max=0.9, var_ratio_max=9, mean_shift_max=9
    )
    gated = run_relation_test(
        target, [driver], SPEC, method=CALIBRATED, config=EngineConfig(gate=strict)
    )
    assert gated.status == "supported"  # the decision is untouched
    assert gated.windows[PERIOD_ALL].reliable is False
    assert "sample" in gated.windows[PERIOD_ALL].reliability_reason
    assert gated.reliable is False


def test_hac_rule_bandwidth_ignores_the_overlap_horizon():
    target, driver = linked_pair(n=252)
    spec = RelationSpec("tcmb|TGT", {"tcmb|DRV": "positive"}, 0, 3, "annual_pct_change")
    default = run_relation_test(target, [driver], spec, method=HacOls(calibrated=True))
    rule = run_relation_test(target, [driver], spec, method=HacOls(maxlags="rule", calibrated=True))
    eleven = run_relation_test(target, [driver], spec, method=HacOls(maxlags=11, calibrated=True))
    settings = lambda report: report.windows[PERIOD_ALL].details["settings"]["maxlags"]  # noqa: E731
    assert settings(default) == 11 and settings(eleven) == 11  # the overlap horizon
    # rule of thumb floor(4 * (n / 100) ** (2 / 9)) for the 237-observation block (252 - 12 - 3)
    assert settings(rule) == 4


def test_a_gate_pass_is_never_a_validation_and_the_three_facts_are_stored_apart():
    from app.analysis.engine import EngineConfig
    from app.analysis.gate import GateThresholds

    target, driver = linked_pair()
    open_gate = GateThresholds(0, 9, 9, float("inf"), float("inf"))
    shut_gate = GateThresholds(10_000, 9, 9, float("inf"), float("inf"))

    def window(method, gate):
        report = run_relation_test(
            target, [driver], SPEC, method=method, config=EngineConfig(gate=gate)
        )
        return report.windows[PERIOD_ALL]

    unvalidated = window(HacOls(calibrated=False), open_gate)
    assert unvalidated.reliable is False  # the gate passes, the method is not validated
    assert (
        unvalidated.details["gate_passed"] is True
        and unvalidated.details["method_validated"] is False
    )
    assert UNCALIBRATED_REASON in unvalidated.reliability_reason

    gated_out = window(HacOls(calibrated=True), shut_gate)
    assert gated_out.reliable is False  # validated method, window outside the gate
    assert (
        gated_out.details["method_validated"] is True and gated_out.details["gate_passed"] is False
    )
    assert "sample" in gated_out.reliability_reason

    both = window(HacOls(calibrated=True), open_gate)
    assert both.reliable is True and both.reliability_reason is None

    both_bad = window(HacOls(calibrated=False), shut_gate)
    assert (
        UNCALIBRATED_REASON in both_bad.reliability_reason
        and "sample" in both_bad.reliability_reason
    )


def test_a_structurally_insufficient_window_is_never_reliable_even_with_a_validated_method():
    target, driver = linked_pair(n=20)  # far below the minimum observations
    report = run_relation_test(target, [driver], SPEC, method=CALIBRATED)
    assert report.status == "insufficient_data"
    for window in report.windows.values():
        assert window.reliable is False
        assert window.reliability_reason.startswith("insufficient data")
        assert window.details["structurally_eligible"] is False
    assert report.reliable is False
    results = to_graph_period_results(report)
    assert all(item.reliable is False and item.reliability_reason for item in results)
