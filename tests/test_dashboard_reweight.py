from __future__ import annotations

import numpy as np
import pandas as pd

from kdtb.dashboard.reweight import (
    filter_universe,
    recompute_composite,
    theme_table,
)


def _wl():
    return pd.DataFrame([
        {"stock_code": "A", "corp_name": "Aco", "market": "KOSPI", "market_cap": 5e11,
         "value_score": 0.9, "quality_score": 0.9, "momentum_score": 0.2, "theme_score": 0.1,
         "top_theme": "방산", "mom_12m": 0.10},
        {"stock_code": "B", "corp_name": "Bco", "market": "KOSDAQ", "market_cap": 2e11,
         "value_score": 0.2, "quality_score": 0.2, "momentum_score": 0.9, "theme_score": 0.9,
         "top_theme": "반도체", "mom_12m": 0.50},
        {"stock_code": "C", "corp_name": "Cco", "market": "KOSPI", "market_cap": 8e10,
         "value_score": 0.5, "quality_score": 0.5, "momentum_score": 0.5, "theme_score": np.nan,
         "top_theme": "", "mom_12m": 0.05},
    ])


def test_value_weight_favors_value_stock():
    r = recompute_composite(_wl(), w_value=1, w_quality=0, w_momentum=0, w_theme=0)
    assert r["stock_code"].iloc[0] == "A"  # highest value_score


def test_momentum_weight_favors_momentum_stock():
    r = recompute_composite(_wl(), w_value=0, w_quality=0, w_momentum=1, w_theme=0)
    assert r["stock_code"].iloc[0] == "B"  # highest momentum_score


def test_missing_theme_reweighted_not_penalized():
    # C has NaN theme; with theme-only weight it must get a NaN composite (no theme),
    # while A/B rank by their theme score — C should sink to the bottom, not error.
    r = recompute_composite(_wl(), w_value=0, w_quality=0, w_momentum=0, w_theme=1)
    assert r["stock_code"].iloc[0] == "B"
    assert pd.isna(r[r["stock_code"] == "C"]["composite"].iloc[0])


def test_rank_is_contiguous_and_sorted():
    r = recompute_composite(_wl(), 0.25, 0.25, 0.25, 0.25)
    assert list(r["rank"]) == [1, 2, 3]
    comps = r["composite"].dropna().to_numpy()
    assert (np.diff(comps) <= 1e-9).all()


def test_filter_by_market_cap_and_market():
    f = filter_universe(_wl(), min_market_cap_krw=1e11, markets=["KOSPI"])
    assert set(f["stock_code"]) == {"A"}  # B is KOSDAQ, C below 100B


def test_theme_table_orders_by_median_momentum():
    t = theme_table(_wl())
    assert list(t["theme"]) == ["반도체", "방산"]  # 반도체 0.50 > 방산 0.10
    assert t["n"].sum() == 2  # C has no theme, excluded
