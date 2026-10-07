"""Simulators of the calibration study: known processes, truth labels, calendar, ids."""

from __future__ import annotations

import numpy as np
import pytest

from app.analysis.calib.dgp import (
    END_SLOT,
    GATED_FAMILIES,
    HELD_OUT_FAMILIES,
    LAG_MAX,
    REGION_FAMILIES,
    STRUCTURES,
    TUNING_FAMILIES,
    Cell,
    make_replicate,
    noise,
    residual_pool,
)
from app.analysis.slots import date_of


@pytest.mark.parametrize("family", [*TUNING_FAMILIES, *HELD_OUT_FAMILIES])
def test_noise_has_roughly_unit_variance(family):
    rng = np.random.default_rng(1)
    values = np.concatenate([noise(rng, 400, family) for _ in range(30)])
    assert 0.5 < values.var() < 2.6  # breaks and seasonality are allowed to drift a little
    assert np.all(np.isfinite(values))


def _mean_rho1(family: str) -> float:
    rng = np.random.default_rng(2)
    series = [noise(rng, 600, family) for _ in range(20)]
    return float(np.mean([np.corrcoef(s[1:], s[:-1])[0, 1] for s in series]))


def test_ar_processes_have_their_autocorrelation():
    assert abs(_mean_rho1("iid")) < 0.05
    assert 0.4 < _mean_rho1("ar05") < 0.6
    assert 0.8 < _mean_rho1("ar09") < 0.95
    assert -0.6 < _mean_rho1("ar_neg") < -0.4
    assert 0.8 < _mean_rho1("ma11") < 1.0  # an annual sum of monthly shocks: 11/12 overlap


def test_truth_labels_of_the_structures():
    assert STRUCTURES["k1_null"].true_support is False
    assert STRUCTURES["k1_reversed"].true_support is False
    assert STRUCTURES["k2_real_null"].true_support is False  # partial null
    assert STRUCTURES["k2_real_reversed"].true_support is False
    assert STRUCTURES["k3_real_real_null"].true_support is False
    assert STRUCTURES["k1_real04"].true_support is True
    assert STRUCTURES["k2_real_nearzero"].true_support is True
    assert STRUCTURES["k3_real_real_real"].true_support is True
    assert {s.k for s in STRUCTURES.values()} == {1, 2, 3}


def test_cell_ids_are_unique_and_readable():
    cells = [
        Cell("stat", "iid", "k1_null", 120),
        Cell("e2e", "iid", "k1_null", 120, "annual_pct_change"),
        Cell("e2e", "iid", "k1_null", 120, "annual_pct_change", gap=True),
        Cell("e2e", "iid", "k1_null", 120, "annual_pct_change", nominal=True),
    ]
    ids = [cell.id for cell in cells]
    assert len(set(ids)) == 4
    assert ids[0] == "stat/iid/k1_null/n120"
    assert ids[2].endswith("/gap") and ids[3].endswith("/nominal")


def test_stat_replicate_has_the_requested_common_block_and_ends_in_2026_09():
    rng = np.random.default_rng(3)
    series, spec, deflator = make_replicate(Cell("stat", "iid", "k2_real_null", 120), rng)
    assert deflator is None and len(series) == 3
    assert spec.target == "T" and spec.drivers == ("D0", "D1")
    assert all(len(item.values) == 120 + LAG_MAX for item in series)
    assert series[0].values.index.max() == END_SLOT
    assert date_of(int(series[0].values.index.max()), 1).isoformat() == "2026-09-01"
    assert series[0].aggregation == "yeniden_hesapla"  # used as it is, no transform


def test_e2e_replicate_levels_gap_and_nominal():
    rng = np.random.default_rng(4)
    cell = Cell("e2e", "iid", "k1_null", 120, "annual_pct_change")
    series, spec, _ = make_replicate(cell, rng)
    assert spec.transform == "annual_pct_change"
    assert all(item.aggregation == "ortalama" for item in series)
    assert (series[0].values > 0).all()  # levels, so a percent change is defined
    full = 120 + 12 + LAG_MAX
    assert len(series[0].values) == full

    gap_cell = Cell("e2e", "iid", "k1_null", 120, "annual_pct_change", gap=True)
    gapped, _, _ = make_replicate(gap_cell, np.random.default_rng(4))
    assert len(gapped[0].values) < full and len(gapped[1].values) == full  # only the target

    nominal_cell = Cell("e2e", "iid", "k1_real04", 120, "annual_pct_change", nominal=True)
    _, spec_n, cpi = make_replicate(nominal_cell, np.random.default_rng(4))
    assert cpi is not None and set(spec_n.nominal_tl_series) == {"T", "D0"}


def test_the_effect_is_in_the_data_and_the_null_is_not():
    def correlation(structure: str, seed: int) -> float:
        rng = np.random.default_rng(seed)
        series, _, _ = make_replicate(Cell("stat", "iid", structure, 250), rng)
        y = series[0].values.to_numpy()
        x = series[1].values.to_numpy()
        return float(np.corrcoef(y[1:], x[:-1])[0, 1])  # true lag 1

    real = np.mean([correlation("k1_real07", seed) for seed in range(10)])
    null = np.mean([abs(correlation("k1_null", seed)) for seed in range(10)])
    assert real > 0.4 and null < 0.15


def test_the_residual_pool_exists_and_is_standardised():
    pool = residual_pool()
    assert len(pool) > 1000 and abs(pool.mean()) < 0.1 and 0.8 < pool.std() < 1.2


def _variance_halves(family: str, length: int, seed: int) -> tuple[float, float]:
    """Variance of the first and second half of the part that survives the burn-in."""
    rng = np.random.default_rng(seed)
    total = length + 200
    values = noise(rng, total, family, keep=length)[-length:]
    half = length // 2
    return float(values[:half].var()), float(values[half:].var())


@pytest.mark.parametrize("length", [39, 63, 123, 253])
def test_the_variance_break_survives_the_burn_in_at_every_sample_size(length):
    ratios = []
    for seed in range(40):
        first, second = _variance_halves("var_break", length, seed)
        ratios.append(second / first)
    assert 2.5 < float(np.median(ratios)) < 7.0  # a sd factor of 2 is a variance factor of 4


@pytest.mark.parametrize("length", [39, 123])
def test_the_mean_break_lies_inside_the_kept_window_and_shifts_the_mean(length):
    shifts = []
    for seed in range(60):
        rng = np.random.default_rng(seed)
        values = noise(rng, length + 200, "mean_break", keep=length)[-length:]
        shifts.append(abs(values[length // 2 :].mean() - values[: length // 2].mean()))
    assert float(np.mean(shifts)) > 0.4  # the +1 jump is in the window (about half of it per half)


def test_heavy_tails_are_standardised_and_heavier_than_normal():
    rng = np.random.default_rng(1)
    values = noise(rng, 200_000, "heavy")
    assert 0.8 < values.std() < 1.3
    assert np.mean(np.abs(values) > 4) > 3 * np.mean(np.abs(rng.normal(size=200_000)) > 4)


def _acf(family: str, lag: int, length: int = 600, count: int = 20) -> float:
    rng = np.random.default_rng(5)
    values = []
    for _ in range(count):
        series = noise(rng, length, family)
        values.append(float(np.corrcoef(series[lag:], series[:-lag])[0, 1]))
    return float(np.mean(values))


def test_stochastic_seasonality_has_its_lag_12_autocorrelation_and_nothing_at_lag_1():
    assert 0.4 < _acf("sar12_05", 12) < 0.6 and abs(_acf("sar12_05", 1)) < 0.1
    assert 0.7 < _acf("sar12_08", 12) < 0.9 and abs(_acf("sar12_08", 1)) < 0.1


def test_the_weak_seasonal_family_is_weaker_than_the_strong_one():
    assert _acf("seasonal_weak", 12) < _acf("seasonal_indep", 12) - 0.2


def test_region_and_gated_families_are_disjoint_and_declared():
    assert set(REGION_FAMILIES).isdisjoint(GATED_FAMILIES)
    assert {"iid", "ar05", "ma11", "sar12_05", "seasonal_weak"} == set(REGION_FAMILIES)
    assert {
        "seasonal_indep",
        "seasonal_common",
        "sar12_08",
        "seasonal_mid",
        "sar12_065",
    } == set(GATED_FAMILIES)
    # the two boundary controls are validation-only: unseen at tuning
    unseen = {"seasonal_mid", "sar12_065"}
    assert unseen.isdisjoint(TUNING_FAMILIES)
    assert (set(REGION_FAMILIES) | (set(GATED_FAMILIES) - unseen)) <= set(TUNING_FAMILIES)


def test_the_boundary_controls_sit_between_the_tuned_extremes():
    assert _acf("seasonal_weak", 12) < _acf("seasonal_mid", 12) < _acf("seasonal_indep", 12)
    assert _acf("sar12_05", 12) < _acf("sar12_065", 12) < _acf("sar12_08", 12)
