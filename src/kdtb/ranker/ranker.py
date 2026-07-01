"""Core ranking engine — pure functions over a DataFrame, no network.

Input: one row per stock with raw inputs
    stock_code, corp_name, market, price, shares, equity, net_income, debt,
    mom_12m   (12-month trailing return, e.g. 0.15 for +15%)

Output: the same rows plus derived factors, per-group percentile scores, a
composite score in [0, 1], and a rank. Higher composite = more attractive.

Standardization is cross-sectional PERCENTILE RANK per factor (0 = worst in the
universe, 1 = best), which is robust to the heavy tails of valuation ratios and
keeps every factor on the same scale before weighting.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class FactorWeights:
    """Weights for the factor GROUPS (need not sum to 1; normalized).

    `theme` is the data-driven theme-momentum tilt (0 = pure fundamentals).
    """
    value: float = 0.40
    quality: float = 0.35
    momentum: float = 0.25
    theme: float = 0.0

    def normalized(self) -> "FactorWeights":
        total = self.value + self.quality + self.momentum + self.theme
        if total <= 0:
            return FactorWeights(0.25, 0.25, 0.25, 0.25)
        return FactorWeights(self.value / total, self.quality / total,
                             self.momentum / total, self.theme / total)


@dataclass
class RankFilters:
    min_market_cap_krw: float = 100e9       # ₩100B floor for liquidity
    require_positive_equity: bool = True     # drop negative-book (distressed) names
    exclude_preferred: bool = True           # drop preferred shares (names ending 우)
    max_debt_to_equity: float | None = None  # optional hard cap


def _pct_rank(series: pd.Series, higher_better: bool) -> pd.Series:
    """Cross-sectional percentile rank in [0, 1]; NaN stays NaN (neutral later)."""
    s = series.astype(float)
    valid = s.notna()
    out = pd.Series(np.nan, index=s.index)
    if valid.sum() == 0:
        return out
    ranks = s[valid].rank(method="average", ascending=higher_better)
    out[valid] = (ranks - 1) / max(len(ranks) - 1, 1)  # 0..1
    return out


def compute_factor_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Derive market cap, yields, ROE, leverage and momentum from raw inputs."""
    out = df.copy()
    out["market_cap"] = out["price"] * out["shares"]
    eq = out["equity"].astype(float)
    mc = out["market_cap"].astype(float)
    ni = out["net_income"].astype(float)

    # Yields are more rank-robust than ratios (handle losses / tiny denominators).
    out["book_yield"] = np.where(mc > 0, eq / mc, np.nan)        # 1 / PBR
    out["earnings_yield"] = np.where(mc > 0, ni / mc, np.nan)    # 1 / PER (negative if loss)
    out["roe"] = np.where(eq > 0, ni / eq, np.nan)
    out["debt_to_equity"] = np.where(eq > 0, out["debt"].astype(float) / eq, np.nan)
    # Reader-friendly ratios
    out["pbr"] = np.where(out["book_yield"] > 0, 1 / out["book_yield"], np.nan)
    out["per"] = np.where(out["earnings_yield"] > 0, 1 / out["earnings_yield"], np.nan)
    return out


def _apply_filters(df: pd.DataFrame, f: RankFilters) -> pd.DataFrame:
    out = df
    if f.require_positive_equity:
        out = out[out["equity"].astype(float) > 0]
    out = out[out["market_cap"].astype(float) >= f.min_market_cap_krw]
    if f.exclude_preferred:
        out = out[~out["corp_name"].astype(str).str.endswith(("우", "우B", "우C"))]
    if f.max_debt_to_equity is not None:
        out = out[out["debt_to_equity"].astype(float) <= f.max_debt_to_equity]
    return out.copy()


def rank_universe(
    df: pd.DataFrame,
    weights: FactorWeights | None = None,
    filters: RankFilters | None = None,
) -> pd.DataFrame:
    """Compute factors, filter, score, and rank the universe (best first)."""
    weights = (weights or FactorWeights()).normalized()
    filters = filters or RankFilters()

    enriched = compute_factor_columns(df)
    universe = _apply_filters(enriched, filters)
    if universe.empty:
        return universe

    # VALUE / QUALITY are 2-factor groups. A MISSING sub-factor (e.g. earnings
    # data we couldn't read) is treated as NEUTRAL (0.5), not skipped — so a
    # stock cheap on book value but with UNKNOWN earnings is pulled toward the
    # middle instead of getting a free top score on half the picture. A stock
    # with KNOWN-bad fundamentals (a loss, high debt) still scores low, because
    # its sub-factor is present and simply ranks poorly.
    val = pd.concat([
        _pct_rank(universe["book_yield"], higher_better=True),
        _pct_rank(universe["earnings_yield"], higher_better=True),
    ], axis=1).fillna(0.5).mean(axis=1)
    qual = pd.concat([
        _pct_rank(universe["roe"], higher_better=True),
        _pct_rank(universe["debt_to_equity"], higher_better=False),
    ], axis=1).fillna(0.5).mean(axis=1)
    # data-completeness flag: do we actually know this stock's earnings?
    universe["has_earnings"] = universe["net_income"].notna() if "net_income" in universe.columns else True
    # MOMENTUM: high trailing return
    mom = _pct_rank(universe["mom_12m"], higher_better=True)
    # THEME: strength of the stock's hottest theme (data-driven basket momentum)
    if "theme_raw" in universe.columns:
        theme = _pct_rank(universe["theme_raw"], higher_better=True)
    else:
        theme = pd.Series(np.nan, index=universe.index)

    universe["value_score"] = val
    universe["quality_score"] = qual
    universe["momentum_score"] = mom
    universe["theme_score"] = theme

    # Composite: weighted mean of available group scores (missing group => reweight).
    groups = pd.DataFrame({"value": val, "quality": qual, "momentum": mom, "theme": theme})
    w = pd.Series({"value": weights.value, "quality": weights.quality,
                   "momentum": weights.momentum, "theme": weights.theme})
    mask = groups.notna()
    weighted = (groups.fillna(0) * w).sum(axis=1)
    wsum = (mask * w).sum(axis=1)
    universe["composite"] = np.where(wsum > 0, weighted / wsum, np.nan)

    ranked = universe.sort_values("composite", ascending=False).reset_index(drop=True)
    ranked.insert(0, "rank", np.arange(1, len(ranked) + 1))
    return ranked
