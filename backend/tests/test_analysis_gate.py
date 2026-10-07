"""Gate features and thresholds (protocol v1.1 section 7)."""

from __future__ import annotations

import numpy as np

from app.analysis.gate import GateThresholds, assess, autocorrelation, window_features

LIMITS = GateThresholds(
    n_min=60, rho1_max=0.8, rho_seasonal_max=0.8, var_ratio_max=3.0, mean_shift_max=1.0
)


def test_autocorrelation_of_known_series():
    rng = np.random.default_rng(1)
    noise = rng.normal(size=4000)
    assert abs(autocorrelation(noise, 1)) < 0.05
    ar = np.zeros(4000)
    for i in range(1, 4000):
        ar[i] = 0.9 * ar[i - 1] + noise[i]
    assert 0.85 < autocorrelation(ar, 1) < 0.95
    assert autocorrelation(np.ones(10), 1) == 0.0
    assert autocorrelation(noise[:3], 5) == 0.0


def test_features_detect_breaks_and_seasonality():
    rng = np.random.default_rng(2)
    n = 240
    calm = rng.normal(size=n)
    assert window_features(calm, [calm], 1)["var_ratio"] < 1.6
    broken = calm.copy()
    broken[n // 2 :] *= 4.0
    assert window_features(broken, [calm], 1)["var_ratio"] > 8.0
    shifted = calm.copy()
    shifted[n // 2 :] += 3.0
    assert window_features(shifted, [calm], 1)["mean_shift"] > 1.0
    seasonal = np.sin(2 * np.pi * np.arange(n) / 12) + 0.3 * rng.normal(size=n)
    assert window_features(seasonal, [calm], 1)["rho_seasonal"] > 0.8
    assert window_features(seasonal, [calm], 3)["n"] == n  # a quarterly step uses lag 4


def test_assess_reports_every_violated_condition():
    ok = {"n": 120, "rho1": 0.3, "rho_seasonal": 0.2, "var_ratio": 1.2, "mean_shift": 0.2}
    assert assess(ok, LIMITS) == (True, None)
    bad = {"n": 40, "rho1": 0.95, "rho_seasonal": 0.9, "var_ratio": 5.0, "mean_shift": 2.0}
    reliable, reason = assess(bad, LIMITS)
    assert reliable is False
    for fragment in (
        "sample 40",
        "autocorrelation 0.95",
        "seasonal",
        "variance break",
        "mean break",
    ):
        assert fragment in reason


def test_lag_24_separates_a_deterministic_cycle_from_an_annual_change():
    rng = np.random.default_rng(4)
    n = 240
    cycle = np.sin(2 * np.pi * np.arange(n) / 12) + 0.7 * rng.normal(size=n)
    shocks = rng.normal(size=n + 11)
    annual_change = np.convolve(shocks, np.ones(12), mode="valid") / np.sqrt(12)  # MA(11)
    cyc = window_features(cycle, [], 1)
    ann = window_features(annual_change, [], 1)
    assert cyc["rho_seasonal24"] > 0.3  # a cycle keeps its autocorrelation at lag 24
    assert ann["rho_seasonal24"] < 0.25  # an annual change does not
    assert cyc["rho_seasonal24"] > ann["rho_seasonal24"] + 0.1


def test_the_gate_flags_persistence_only_when_lag_12_and_lag_24_are_both_high():
    limits = GateThresholds(60, 0.99, 0.3, 4.0, 1.0, rho_seasonal24_max=0.2)
    base = {"n": 120, "rho1": 0.3, "var_ratio": 1.2, "mean_shift": 0.2}
    cycle = {**base, "rho_seasonal": 0.47, "rho_seasonal24": 0.45}
    annual = {**base, "rho_seasonal": 0.47, "rho_seasonal24": 0.05}  # high at 12 only
    calm = {**base, "rho_seasonal": 0.1, "rho_seasonal24": 0.05}
    assert (
        assess(cycle, limits)[0] is False and "persistent seasonality" in assess(cycle, limits)[1]
    )
    assert assess(annual, limits) == (True, None)
    assert assess(calm, limits) == (True, None)
    off = GateThresholds(60, 0.99, 0.3, 4.0, 1.0)  # lag-24 condition off: lag 12 alone flags
    assert assess(annual, off)[0] is False
