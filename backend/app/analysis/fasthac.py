"""Vectorised OLS + Newey-West (HAC) for the resampling methods (Task 3.2, protocol v1.1).

The whole-chain bootstrap re-runs the lag search and the regression thousands of times, so the
statsmodels call per replicate is too slow. This module reproduces exactly what
``HacOls`` asks statsmodels for (Bartlett kernel, ``use_correction=True`` small-sample factor
``n / (n - k)``, t reference) with plain numpy. ``tests/test_analysis_fasthac.py`` checks that the
t statistics equal statsmodels' to 1e-8 on random data; any change here must keep that test green.
"""

from __future__ import annotations

import numpy as np

from app.analysis.errors import NumericalFailure, SingularDesignError


def ols_hac_t(y: np.ndarray, x: np.ndarray, maxlags: int) -> tuple[np.ndarray, np.ndarray]:
    """OLS of ``y`` on ``[1, x]`` with HAC standard errors.

    Returns ``(coefficients, t_values)`` for the intercept followed by every column of ``x``
    (``x`` is ``n`` or ``n x k``). ``maxlags`` is the Bartlett bandwidth (0 = White).
    """
    y = np.asarray(y, dtype="float64")
    n = y.shape[0]
    design = np.column_stack([np.ones(n), np.asarray(x, dtype="float64").reshape(n, -1)])
    k = design.shape[1]
    try:
        xtx_inv = np.linalg.inv(design.T @ design)
    except np.linalg.LinAlgError as error:
        raise SingularDesignError("singular regression design") from error
    beta = xtx_inv @ design.T @ y
    resid = y - design @ beta
    scores = design * resid[:, None]  # n x k
    meat = scores.T @ scores
    for lag in range(1, maxlags + 1):
        weight = 1.0 - lag / (maxlags + 1.0)
        gamma = scores[lag:].T @ scores[:-lag]
        meat += weight * (gamma + gamma.T)
    cov = xtx_inv @ meat @ xtx_inv * (n / (n - k))
    return beta, beta / np.sqrt(np.diag(cov))


def ols_hac_t_batch(y: np.ndarray, x: np.ndarray, maxlags: int) -> tuple[np.ndarray, np.ndarray]:
    """Many regressions at once: ``y`` is ``B x n``, ``x`` is ``B x n x k``.

    Same arithmetic as :func:`ols_hac_t` per replicate (a test asserts equality); returns
    ``(coefficients B x (k+1), t_values B x (k+1))``.
    """
    y = np.asarray(y, dtype="float64")
    x = np.asarray(x, dtype="float64")
    b, n = y.shape
    design = np.concatenate([np.ones((b, n, 1)), x], axis=2)
    k = design.shape[2]
    try:
        xtx_inv = np.linalg.inv(np.einsum("bnk,bnl->bkl", design, design))
    except np.linalg.LinAlgError as error:
        raise SingularDesignError("singular regression design") from error
    beta = np.einsum("bkl,bnl,bn->bk", xtx_inv, design, y)
    resid = y - np.einsum("bnk,bk->bn", design, beta)
    scores = design * resid[:, :, None]
    meat = np.einsum("bnk,bnl->bkl", scores, scores)
    for lag in range(1, maxlags + 1):
        weight = 1.0 - lag / (maxlags + 1.0)
        gamma = np.einsum("bnk,bnl->bkl", scores[:, lag:], scores[:, :-lag])
        meat = meat + weight * (gamma + gamma.transpose(0, 2, 1))
    cov = xtx_inv @ meat @ xtx_inv * (n / (n - k))
    se = np.sqrt(np.diagonal(cov, axis1=1, axis2=2))
    t_values = beta / se
    if not np.isfinite(t_values).all():
        raise NumericalFailure("non-finite HAC t statistic")
    return beta, t_values
