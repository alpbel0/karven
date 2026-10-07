"""The vectorised HAC must equal statsmodels exactly (protocol v1.1)."""

from __future__ import annotations

import numpy as np
import pytest
import statsmodels.api as sm

from app.analysis.fasthac import ols_hac_t


@pytest.mark.parametrize("maxlags", [0, 3, 11, 18])
@pytest.mark.parametrize("columns", [1, 2, 3])
def test_matches_statsmodels_hac(maxlags, columns):
    rng = np.random.default_rng(maxlags * 10 + columns)
    n = 90
    x = rng.normal(size=(n, columns))
    for column in range(columns):  # autocorrelated regressors and errors
        for i in range(1, n):
            x[i, column] += 0.6 * x[i - 1, column]
    noise = rng.normal(size=n)
    for i in range(1, n):
        noise[i] += 0.5 * noise[i - 1]
    y = 0.3 + x @ np.arange(1, columns + 1) * 0.2 + noise
    beta, t_values = ols_hac_t(y, x, maxlags)
    reference = sm.OLS(y, sm.add_constant(x)).fit(
        cov_type="HAC", cov_kwds={"maxlags": maxlags, "use_correction": True}, use_t=True
    )
    np.testing.assert_allclose(beta, reference.params, rtol=1e-9, atol=1e-10)
    np.testing.assert_allclose(t_values, reference.tvalues, rtol=1e-8, atol=1e-10)


def test_single_column_vector_input():
    rng = np.random.default_rng(1)
    x = rng.normal(size=60)
    y = x + rng.normal(size=60)
    beta, t_values = ols_hac_t(y, x, 2)
    assert beta.shape == (2,) and t_values.shape == (2,)


def test_batch_equals_single_runs():
    from app.analysis.fasthac import ols_hac_t_batch

    rng = np.random.default_rng(5)
    b, n, k = 7, 80, 2
    x = rng.normal(size=(b, n, k))
    y = x[:, :, 0] * 0.5 + rng.normal(size=(b, n))
    beta_b, t_b = ols_hac_t_batch(y, x, 6)
    for i in range(b):
        beta, t_values = ols_hac_t(y[i], x[i], 6)
        np.testing.assert_allclose(beta_b[i], beta, rtol=1e-9, atol=1e-10)
        np.testing.assert_allclose(t_b[i], t_values, rtol=1e-9, atol=1e-10)
