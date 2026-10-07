"""Circular shift, stationary bootstrap and the whole-chain bootstrap (protocol v1.1)."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest
import statsmodels.api as sm

from app.analysis.engine import run_relation_test
from app.analysis.errors import AnalysisError
from app.analysis.models import PERIOD_ALL, RelationSpec
from app.analysis.resampling import (
    CircularShift,
    StationaryBootstrap,
    WholeChainBootstrap,
    _chain,
    _lag_columns,
    _standardise,
    block_periods,
    moving_block_indices,
    politis_white_block,
    stationary_indices,
)
from app.analysis.significance import BlockProblem, default_hac_lags
from tests.analysis_helpers import independent_pair, linked_pair, random_walk, series_from_levels

SPEC = RelationSpec("tcmb|TGT", {"tcmb|DRV": "positive"}, 0, 3, "difference")

METHODS = {
    "shift": CircularShift(shifts=199, seed=1, calibrated=True),
    "stationary": StationaryBootstrap(mean_block=12, replicates=199, seed=1, calibrated=True),
    "chain": WholeChainBootstrap(block=12, replicates=199, seed=1, calibrated=True),
}


@pytest.mark.parametrize("name", list(METHODS))
def test_a_strong_relation_is_supported_by_every_resampling_method(name):
    target, driver = linked_pair()
    report = run_relation_test(target, [driver], SPEC, method=METHODS[name])
    assert report.status == "supported"
    window = report.windows[PERIOD_ALL]
    assert window.best_lag == 1
    assert window.p_value <= 0.05
    assert window.reliable is True
    assert "settings" in window.details


@pytest.mark.parametrize("name", list(METHODS))
def test_the_wrong_direction_is_unsupported(name):
    target, driver = linked_pair()
    spec = replace(SPEC, driver_directions={"tcmb|DRV": "negative"})
    report = run_relation_test(target, [driver], spec, method=METHODS[name])
    assert report.status == "unsupported"


@pytest.mark.parametrize("name", list(METHODS))
def test_noise_is_rarely_supported(name):
    supported = windows = 0
    for seed in range(12):
        target, driver = independent_pair(n=150, seed=seed)
        report = run_relation_test(target, [driver], SPEC, method=METHODS[name])
        for window in report.windows.values():
            windows += 1
            supported += window.result == "supported"
    assert supported / windows < 0.2


@pytest.mark.parametrize("name", list(METHODS))
def test_results_are_deterministic_for_a_seed_and_change_with_it(name):
    target, driver = linked_pair(n=150, beta=0.15)
    first = run_relation_test(target, [driver], SPEC, method=METHODS[name])
    again = run_relation_test(target, [driver], SPEC, method=METHODS[name])
    other = run_relation_test(target, [driver], SPEC, method=replace(METHODS[name], seed=2))
    assert first.windows[PERIOD_ALL].p_value == again.windows[PERIOD_ALL].p_value
    assert first.windows[PERIOD_ALL].p_value >= 1 / 200
    assert first.windows[PERIOD_ALL].p_value != other.windows[PERIOD_ALL].p_value or name == "shift"


def test_uncalibrated_resampling_results_carry_the_warning():
    target, driver = linked_pair()
    report = run_relation_test(target, [driver], SPEC, method=CircularShift(seed=1))
    assert report.reliable is False


def test_pairwise_methods_refuse_several_drivers():
    rng = np.random.default_rng(1)
    target = series_from_levels("tcmb|TGT", random_walk(rng, rng.normal(size=150)))
    d1 = series_from_levels("tcmb|D1", random_walk(rng, rng.normal(size=150)))
    d2 = series_from_levels("tcmb|D2", random_walk(rng, rng.normal(size=150)))
    spec = RelationSpec(
        "tcmb|TGT", {"tcmb|D1": "positive", "tcmb|D2": "positive"}, 0, 2, "difference"
    )
    with pytest.raises(AnalysisError):
        run_relation_test(target, [d1, d2], spec, method=CircularShift(seed=1))
    with pytest.raises(AnalysisError):
        run_relation_test(target, [d1, d2], spec, method=StationaryBootstrap(seed=1))
    report = run_relation_test(target, [d1, d2], spec, method=WholeChainBootstrap(replicates=99))
    # the chain handles k drivers (the 2017+ window of a 150-month series is too short)
    assert report.windows[PERIOD_ALL].result in ("unsupported", "supported")


def test_the_chain_bootstrap_needs_every_driver_not_just_one():
    rng = np.random.default_rng(3)
    n = 250
    inc1, inc2 = rng.normal(size=n), rng.normal(size=n)
    target_inc = rng.normal(scale=0.5, size=n)
    target_inc[1:] += 0.6 * inc1[:-1]  # only the first driver is real
    target = series_from_levels("tcmb|TGT", random_walk(rng, target_inc))
    d1 = series_from_levels("tcmb|D1", random_walk(rng, inc1))
    d2 = series_from_levels("tcmb|D2", random_walk(rng, inc2))
    spec = RelationSpec(
        "tcmb|TGT", {"tcmb|D1": "positive", "tcmb|D2": "positive"}, 0, 3, "difference"
    )
    report = run_relation_test(
        target, [d1, d2], spec, method=WholeChainBootstrap(replicates=199, seed=4, calibrated=True)
    )
    window = report.windows[PERIOD_ALL]
    assert window.details["p_values"]["tcmb|D1"] < 0.05  # the real driver is found
    assert window.details["p_values"]["tcmb|D2"] > 0.05  # the irrelevant one is not
    assert window.result == "unsupported"  # partial null: no support for the whole relation


# --------------------------------------------------------------------------- #
# The fast chain must equal an explicit slow reference (protocol requirement)
# --------------------------------------------------------------------------- #


def _problem(rng, n=90, drivers=2, lag_min=0, lag_max=3):
    x_ext = {
        f"D{i}": rng.normal(size=n + lag_max - lag_min).cumsum() * 0.0
        + rng.normal(size=n + lag_max - lag_min)
        for i in range(drivers)
    }
    y = rng.normal(size=n)
    return BlockProblem(
        y=y,
        x_ext=x_ext,
        lag_min=lag_min,
        lag_max=lag_max,
        directions={key: "positive" for key in x_ext},
        transform="difference",
        step=1,
    )


def _slow_chain(problem, y, maxlags):
    """Plain loops: best |r| lag per driver with np.corrcoef, statsmodels HAC OLS."""
    columns, lags = [], {}
    for key, x_ext in problem.x_ext.items():
        best_r, best_lag = -1.0, None
        for lag in problem.lags:
            column = problem.lag_column(x_ext, lag)
            r = abs(float(np.corrcoef(column, y)[0, 1]))
            if r > best_r:
                best_r, best_lag = r, lag
        lags[key] = best_lag
        columns.append(problem.lag_column(x_ext, best_lag))
    fit = sm.OLS(y, sm.add_constant(np.column_stack(columns))).fit(
        cov_type="HAC", cov_kwds={"maxlags": maxlags, "use_correction": True}, use_t=True
    )
    return lags, np.asarray(fit.params)[1:], np.asarray(fit.tvalues)[1:]


def test_fast_chain_equals_the_slow_reference():
    rng = np.random.default_rng(11)
    problem = _problem(rng)
    keys = list(problem.x_ext)
    columns = {key: _lag_columns(problem, problem.x_ext[key]) for key in keys}
    standard = {key: _standardise(columns[key], axis=0) for key in keys}
    targets = rng.normal(size=(6, problem.n)) + problem.x_ext[keys[0]][3:][None, : problem.n] * 0.5
    maxlags = default_hac_lags(problem.n, "difference", 1)
    chosen, beta, t_values = _chain(problem, targets, columns, standard, maxlags)
    for i in range(len(targets)):
        lags, slow_beta, slow_t = _slow_chain(problem, targets[i], maxlags)
        assert {key: problem.lag_min + int(chosen[key][i]) for key in keys} == lags
        np.testing.assert_allclose(beta[i], slow_beta, rtol=1e-8, atol=1e-10)
        np.testing.assert_allclose(t_values[i], slow_t, rtol=1e-7, atol=1e-9)


# --------------------------------------------------------------------------- #
# Index generators and block lengths
# --------------------------------------------------------------------------- #


def test_stationary_indices_are_valid_and_follow_the_block_length():
    rng = np.random.default_rng(1)
    indices = stationary_indices(rng, 100, 400, 10)
    assert indices.shape == (400, 100) and indices.min() >= 0 and indices.max() < 100
    steps = (indices[:, 1:] - indices[:, :-1]) % 100
    continuing = float(np.mean(steps == 1))
    assert 0.85 < continuing < 0.95  # about 1 - 1/10 of the steps continue the block


def test_moving_block_indices_are_contiguous_blocks_trimmed_to_length():
    rng = np.random.default_rng(2)
    indices = moving_block_indices(rng, 50, 30, 12)
    assert indices.shape == (30, 50) and indices.max() < 50
    assert np.all(np.diff(indices[:, :12], axis=1) == 1)  # the first block is contiguous
    assert moving_block_indices(rng, 10, 3, 99).shape == (3, 10)  # block longer than the data


@pytest.mark.filterwarnings("ignore:invalid value:RuntimeWarning")
def test_block_periods_and_politis_white_fallback():
    assert block_periods(12, 1, 120) == 12
    assert block_periods(12, 3, 120) == 4
    assert block_periods(24, 12, 120) == 2  # never below 2
    # the fixed candidates are NOT capped at n / 3 (they stay distinct methods at small n)
    assert [block_periods(m, 1, 36) for m in (12, 18, 24)] == [12, 18, 24]
    assert block_periods(120, 1, 30) == 30  # only capped at n itself
    assert politis_white_block(np.ones(5), 1) >= 2
    series = np.random.default_rng(3).normal(size=200)
    assert 2 <= politis_white_block(series, 1) <= 66  # Politis-White is capped at n / 3


def test_two_identical_drivers_are_reported_as_a_singular_design_not_a_crash():
    rng = np.random.default_rng(8)
    level = random_walk(rng, rng.normal(size=200))
    target = series_from_levels("tcmb|TGT", random_walk(rng, rng.normal(size=200)))
    twin_a = series_from_levels("tcmb|A", level)
    twin_b = series_from_levels("tcmb|B", level)
    spec = RelationSpec(
        "tcmb|TGT", {"tcmb|A": "positive", "tcmb|B": "positive"}, 0, 2, "difference"
    )
    report = run_relation_test(
        target, [twin_a, twin_b], spec, method=WholeChainBootstrap(replicates=49, seed=1)
    )
    window = report.windows[PERIOD_ALL]
    assert window.result == "insufficient_data"
    assert window.details["reason"] == "singular regression design"
