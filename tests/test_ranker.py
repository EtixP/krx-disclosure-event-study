from __future__ import annotations

import numpy as np
import pandas as pd

from kdtb.ranker.ranker import (
    FactorWeights,
    RankFilters,
    _pct_rank,
    compute_factor_columns,
    rank_universe,
)


def _stock(code, name, market, price, shares, equity, ni, debt, mom):
    return dict(stock_code=code, corp_name=name, market=market, price=price,
                shares=shares, equity=equity, net_income=ni, debt=debt, mom_12m=mom)


def _universe():
    # cheap+quality+rising "good" name vs expensive+weak+falling "bad" name + middles
    return pd.DataFrame([
        _stock("000001", "Good", "KOSPI", 1000, 1_000_000, 2_000_000_000, 300_000_000, 200_000_000, 0.40),
        _stock("000002", "Mid", "KOSPI", 1000, 1_000_000, 1_000_000_000, 100_000_000, 500_000_000, 0.05),
        _stock("000003", "Bad", "KOSPI", 5000, 1_000_000, 800_000_000, -50_000_000, 2_000_000_000, -0.30),
        _stock("000004", "Mid2", "KOSDAQ", 2000, 1_000_000, 1_500_000_000, 150_000_000, 400_000_000, 0.15),
    ])


def test_pct_rank_basic():
    s = pd.Series([10.0, 20.0, 30.0])
    hi = _pct_rank(s, higher_better=True)
    assert hi.iloc[0] == 0.0 and hi.iloc[2] == 1.0
    lo = _pct_rank(s, higher_better=False)
    assert lo.iloc[0] == 1.0 and lo.iloc[2] == 0.0


def test_pct_rank_handles_nan():
    s = pd.Series([10.0, np.nan, 30.0])
    out = _pct_rank(s, higher_better=True)
    assert np.isnan(out.iloc[1])
    assert out.iloc[0] == 0.0 and out.iloc[2] == 1.0


def test_compute_factor_columns_derives_yields():
    df = pd.DataFrame([_stock("x", "X", "KOSPI", 1000, 1000, 2_000_000, 100_000, 500_000, 0.1)])
    out = compute_factor_columns(df)
    assert out["market_cap"].iloc[0] == 1_000_000
    assert abs(out["book_yield"].iloc[0] - 2.0) < 1e-9        # equity 2M / mcap 1M
    assert abs(out["earnings_yield"].iloc[0] - 0.1) < 1e-9     # 100k / 1M
    assert abs(out["roe"].iloc[0] - 0.05) < 1e-9              # 100k / 2M
    assert abs(out["pbr"].iloc[0] - 0.5) < 1e-9


def test_loss_making_has_no_per_but_negative_earnings_yield():
    df = pd.DataFrame([_stock("x", "X", "KOSPI", 1000, 1000, 2_000_000, -100_000, 0, 0.1)])
    out = compute_factor_columns(df)
    assert np.isnan(out["per"].iloc[0])             # PER undefined for losses
    assert out["earnings_yield"].iloc[0] < 0        # but earnings yield is negative (worst)


def test_rank_universe_puts_good_first_bad_last():
    ranked = rank_universe(_universe(), filters=RankFilters(min_market_cap_krw=0))
    assert ranked["corp_name"].iloc[0] == "Good"
    assert ranked["corp_name"].iloc[-1] == "Bad"
    assert (ranked["composite"].diff().dropna() <= 1e-9).all()  # descending
    assert list(ranked["rank"]) == [1, 2, 3, 4]


def test_market_cap_filter_drops_small():
    u = _universe()
    # all have mcap = price*shares; Good=1e9, Bad=5e9. Floor at 2e9 keeps only Bad/Mid2(2e9)
    ranked = rank_universe(u, filters=RankFilters(min_market_cap_krw=2e9))
    assert "Good" not in set(ranked["corp_name"])  # mcap 1e9 < 2e9 dropped


def test_negative_equity_filtered():
    u = _universe()
    u.loc[u["corp_name"] == "Good", "equity"] = -1
    ranked = rank_universe(u, filters=RankFilters(min_market_cap_krw=0, require_positive_equity=True))
    assert "Good" not in set(ranked["corp_name"])


def test_preferred_shares_excluded():
    u = _universe()
    u.loc[u["corp_name"] == "Mid", "corp_name"] = "삼성전자우"
    ranked = rank_universe(u, filters=RankFilters(min_market_cap_krw=0, exclude_preferred=True))
    assert not any(n.endswith("우") for n in ranked["corp_name"])


def test_weights_normalized_and_momentum_tilt_changes_order():
    u = _universe()
    value_heavy = rank_universe(u, weights=FactorWeights(value=1, quality=0, momentum=0),
                                filters=RankFilters(min_market_cap_krw=0))
    mom_heavy = rank_universe(u, weights=FactorWeights(value=0, quality=0, momentum=1),
                              filters=RankFilters(min_market_cap_krw=0))
    # the orderings should not be identical when the factor emphasis flips
    assert list(value_heavy["stock_code"]) != list(mom_heavy["stock_code"]) or len(u) <= 1


def test_missing_momentum_group_reweights():
    u = _universe()
    u["mom_12m"] = np.nan  # momentum entirely missing
    ranked = rank_universe(u, filters=RankFilters(min_market_cap_krw=0))
    # composite still computed from value+quality only, no NaNs
    assert ranked["composite"].notna().all()
