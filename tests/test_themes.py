from __future__ import annotations

import numpy as np
import pandas as pd

from kdtb.ranker.themes import map_stock_to_themes, theme_name
from kdtb.ranker.theme_momentum import (
    attach_theme_score,
    attach_themes,
    hot_themes,
    theme_strength,
)
from kdtb.ranker.ranker import FactorWeights, RankFilters, rank_universe


def test_map_by_ticker():
    assert "defense" in map_stock_to_themes("012450", "한화에어로스페이스")
    assert "batteries" in map_stock_to_themes("373220", "LG에너지솔루션")
    assert "semiconductors" in map_stock_to_themes("005930", "삼성전자")


def test_map_by_keyword():
    # not in any ticker list, but name contains a distinctive keyword
    assert "shipbuilding" in map_stock_to_themes("999999", "어떤중공업")
    assert "robotics" in map_stock_to_themes("999998", "테스트로봇")


def test_map_unknown_is_empty():
    assert map_stock_to_themes("999000", "무명상사") == []


def test_theme_name_lookup():
    assert theme_name("defense") == "방산"
    assert theme_name("nonexistent") == "nonexistent"


def _df(rows):
    return pd.DataFrame(rows)


def test_theme_strength_respects_min_members():
    df = _df([
        {"stock_code": "012450", "corp_name": "A", "mom_12m": 0.5},   # defense
        {"stock_code": "079550", "corp_name": "B", "mom_12m": 0.3},   # defense
        {"stock_code": "064350", "corp_name": "C", "mom_12m": 0.4},   # defense (3 -> counts)
        {"stock_code": "207940", "corp_name": "D", "mom_12m": 0.9},   # bio (only 1 -> dropped)
    ])
    s = theme_strength(df, min_members=3)
    assert "defense" in s and abs(s["defense"] - 0.4) < 1e-9
    assert "bio" not in s  # too few members


def test_attach_theme_score_uses_best_theme_and_nan_for_none():
    df = _df([
        {"stock_code": "012450", "corp_name": "A", "mom_12m": 0.5},
        {"stock_code": "079550", "corp_name": "B", "mom_12m": 0.3},
        {"stock_code": "064350", "corp_name": "C", "mom_12m": 0.4},
        {"stock_code": "999000", "corp_name": "무명", "mom_12m": 0.1},  # no theme
    ])
    out = attach_theme_score(df, min_members=3)
    defense_strength = out.loc[out["stock_code"] == "012450", "theme_raw"].iloc[0]
    assert abs(defense_strength - 0.4) < 1e-9            # inherits defense basket strength
    assert out.loc[out["stock_code"] == "012450", "top_theme"].iloc[0] == "방산"
    assert np.isnan(out.loc[out["stock_code"] == "999000", "theme_raw"].iloc[0])


def test_hot_themes_sorted_desc():
    hot = hot_themes({"defense": 0.5, "bio": 0.1, "nuclear": 0.9})
    assert hot[0][0] == "원전" and hot[0][1] == 0.9
    assert [h[0] for h in hot] == ["원전", "방산", "바이오"]


def test_theme_tilt_changes_ranking():
    # identical fundamentals; theme_raw varies -> with a theme tilt, hottest theme wins
    base = dict(market="KOSPI", price=1000, shares=1_000_000,
                equity=1_000_000_000, net_income=100_000_000, debt=500_000_000, mom_12m=0.10)
    df = pd.DataFrame([
        {"stock_code": "A", "corp_name": "HotThemeCo", **base, "theme_raw": 0.90},
        {"stock_code": "B", "corp_name": "MidThemeCo", **base, "theme_raw": 0.40},
        {"stock_code": "C", "corp_name": "ColdThemeCo", **base, "theme_raw": 0.05},
    ])
    # theme-dominant weighting should order by theme_raw
    ranked = rank_universe(df, weights=FactorWeights(value=0.2, quality=0.2, momentum=0.1, theme=0.5),
                           filters=RankFilters(min_market_cap_krw=0))
    assert list(ranked["corp_name"]) == ["HotThemeCo", "MidThemeCo", "ColdThemeCo"]

    # with NO theme weight, the tie-broken order should not be driven by theme
    ranked0 = rank_universe(df, weights=FactorWeights(value=0.4, quality=0.4, momentum=0.2, theme=0.0),
                            filters=RankFilters(min_market_cap_krw=0))
    assert set(ranked0["corp_name"]) == {"HotThemeCo", "MidThemeCo", "ColdThemeCo"}
