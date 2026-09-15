"""Small, deterministic inference tools for event-study research.

The confidence interval resamples whole issuer histories. Repeated filings by
one issuer therefore remain one dependence cluster rather than masquerading as
independent observations. Tail diagnostics are deterministic stress tests, not
alternative estimators selected to obtain a preferred conclusion.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from numbers import Integral
from typing import Any

import numpy as np
import pandas as pd


def _finite_values(values: Sequence[float], *, label: str) -> np.ndarray:
    array = np.asarray(values, dtype=float)
    if array.ndim != 1 or len(array) == 0:
        raise ValueError(f"{label} must be a non-empty one-dimensional sequence")
    if not np.isfinite(array).all():
        raise ValueError(f"{label} contains a non-finite value")
    return array


def issuer_clustered_mean_ci(
    values: Sequence[float],
    issuers: Sequence[Any],
    *,
    confidence_level: float = 0.95,
    n_resamples: int = 10_000,
    random_state: int = 0,
) -> dict[str, Any]:
    """Return a percentile CI for an event-weighted mean, clustered by issuer.

    Each bootstrap draw samples the observed number of issuers with replacement
    and carries every event belonging to each sampled issuer into the draw.
    The mean remains event-weighted, matching the repository's headline
    estimand, while the resampling unit reflects within-issuer dependence.
    """
    observed = _finite_values(values, label="values")
    issuer_series = pd.Series(list(issuers), dtype="string")
    if len(issuer_series) != len(observed):
        raise ValueError("values and issuers must have the same length")
    if issuer_series.isna().any() or issuer_series.str.strip().eq("").any():
        raise ValueError("issuer clusters contain a missing value")
    if not 0 < confidence_level < 1:
        raise ValueError("confidence_level must be strictly between zero and one")
    if (
        isinstance(n_resamples, bool)
        or not isinstance(n_resamples, Integral)
        or n_resamples < 2
    ):
        raise ValueError("n_resamples must be at least two")
    if isinstance(random_state, bool) or not isinstance(random_state, Integral):
        raise TypeError("random_state must be an integer")
    n_resamples = int(n_resamples)
    random_state = int(random_state)

    labels = sorted(issuer_series.unique().tolist())
    if len(labels) < 2:
        raise ValueError("issuer-clustered inference requires at least two issuers")
    issuer_array = issuer_series.to_numpy(dtype=str)
    cluster_sums = np.asarray(
        [observed[issuer_array == label].sum() for label in labels], dtype=float
    )
    cluster_counts = np.asarray(
        [(issuer_array == label).sum() for label in labels], dtype=np.int64
    )

    rng = np.random.default_rng(random_state)
    bootstrap_means = np.empty(n_resamples, dtype=float)
    batch_size = 256
    for start in range(0, n_resamples, batch_size):
        stop = min(start + batch_size, n_resamples)
        draws = rng.integers(
            0,
            len(labels),
            size=(stop - start, len(labels)),
            endpoint=False,
        )
        bootstrap_means[start:stop] = (
            cluster_sums[draws].sum(axis=1)
            / cluster_counts[draws].sum(axis=1)
        )

    alpha = 1.0 - confidence_level
    lower, upper = np.quantile(
        bootstrap_means,
        [alpha / 2.0, 1.0 - alpha / 2.0],
        method="linear",
    )
    estimate = float(observed.mean())
    return {
        "estimate": estimate,
        "ci_lower": float(lower),
        "ci_upper": float(upper),
        "bootstrap_standard_error": float(bootstrap_means.std(ddof=1)),
        "confidence_level": float(confidence_level),
        "n_resamples": int(n_resamples),
        "random_state": int(random_state),
        "n_events": int(len(observed)),
        "n_issuers": int(len(labels)),
        "repeated_event_rows": int(len(observed) - len(labels)),
        "largest_issuer_cluster": int(cluster_counts.max()),
        "resampling_unit": "issuer",
        "estimand": "event_weighted_mean",
        "interval": "percentile_cluster_bootstrap",
        "ci_excludes_zero": bool(lower > 0 or upper < 0),
    }


def _distribution_summary(values: np.ndarray) -> dict[str, Any]:
    return {
        "n": int(len(values)),
        "mean": float(values.mean()),
        "median": float(np.median(values)),
        "win_fraction": float((values > 0).mean()),
    }


def tail_sensitivity(
    values: Sequence[float],
    *,
    fractions: Sequence[float] = (0.01, 0.05),
) -> dict[str, Any]:
    """Report fixed one-sided and symmetric tail exclusions.

    For each requested fraction, exactly ``floor(n * fraction)`` observations
    are removed. Reporting top, bottom, and symmetric exclusions together makes
    skew visible and prevents a one-sided stress test from being mistaken for a
    replacement estimate.
    """
    observed = _finite_values(values, label="values")
    ordered = np.sort(observed)
    scenarios: list[dict[str, Any]] = []
    seen: set[float] = set()
    for raw_fraction in fractions:
        fraction = float(raw_fraction)
        if not 0 < fraction < 0.5 or not math.isfinite(fraction):
            raise ValueError("tail fractions must be finite and between zero and 0.5")
        if fraction in seen:
            raise ValueError("tail fractions must be unique")
        seen.add(fraction)
        remove_n = math.floor(len(ordered) * fraction)
        if remove_n < 1:
            raise ValueError(
                f"tail fraction {fraction} removes no observations from n={len(ordered)}"
            )
        if 2 * remove_n >= len(ordered):
            raise ValueError("symmetric tail exclusion leaves no observations")
        variants = (
            ("exclude_top", ordered[:-remove_n]),
            ("exclude_bottom", ordered[remove_n:]),
            ("symmetric_trim", ordered[remove_n:-remove_n]),
        )
        for scenario, retained in variants:
            scenarios.append(
                {
                    "scenario": scenario,
                    "fraction": fraction,
                    "removed_per_selected_tail": int(remove_n),
                    **_distribution_summary(retained),
                }
            )

    return {
        "full_sample": _distribution_summary(ordered),
        "quantiles": {
            "p01": float(np.quantile(ordered, 0.01)),
            "p05": float(np.quantile(ordered, 0.05)),
            "p95": float(np.quantile(ordered, 0.95)),
            "p99": float(np.quantile(ordered, 0.99)),
        },
        "scenarios": scenarios,
    }
