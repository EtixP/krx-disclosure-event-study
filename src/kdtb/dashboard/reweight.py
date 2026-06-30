"""Pure, testable helpers behind the dashboard.

The watchlist CSV already stores each stock's per-factor PERCENTILE scores
(value_score, quality_score, momentum_score, theme_score). Changing the factor
weights only changes how those are combined — no re-fetch needed — so the
dashboard can re-rank instantly as the user moves the sliders.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

SCORE_COLS = ["value_score", "quality_score", "momentum_score", "theme_score"]


def recompute_composite(
    df: pd.DataFrame,
    w_value: float,
    w_quality: float,
    w_momentum: float,
    w_theme: float,
) -> pd.DataFrame:
    """Re-weight the stored percentile scores into a fresh composite + rank.

    Missing scores (e.g. no theme) are reweighted out per-row, so a stock is
    never penalized for a factor it simply doesn't have.
    """
    out = df.copy()
    cols = [c for c in SCORE_COLS if c in out.columns]
    weights = np.array([
        {"value_score": w_value, "quality_score": w_quality,
         "momentum_score": w_momentum, "theme_score": w_theme}[c]
        for c in cols
    ], dtype=float)

    S = out[cols].to_numpy(dtype=float)
    mask = ~np.isnan(S)
    num = np.nansum(np.where(mask, S, 0.0) * weights, axis=1)
    den = (mask * weights).sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        out["composite"] = np.where(den > 0, num / den, np.nan)

    out = out.sort_values("composite", ascending=False, na_position="last").reset_index(drop=True)
    out["rank"] = np.arange(1, len(out) + 1)
    return out


def filter_universe(
    df: pd.DataFrame,
    min_market_cap_krw: float = 0.0,
    markets: list[str] | None = None,
) -> pd.DataFrame:
    out = df
    if "market_cap" in out.columns and min_market_cap_krw > 0:
        out = out[out["market_cap"].astype(float) >= min_market_cap_krw]
    if markets and "market" in out.columns:
        out = out[out["market"].isin(markets)]
    return out.copy()


def theme_table(df: pd.DataFrame) -> pd.DataFrame:
    """Reconstruct the 'hottest themes' view from the watchlist: per theme, the
    member count and median trailing momentum."""
    if "top_theme" not in df.columns or "mom_12m" not in df.columns:
        return pd.DataFrame(columns=["theme", "n", "median_mom"])
    d = df[df["top_theme"].astype(str).str.len() > 0]
    if d.empty:
        return pd.DataFrame(columns=["theme", "n", "median_mom"])
    g = d.groupby("top_theme").agg(n=("stock_code", "count"),
                                   median_mom=("mom_12m", "median")).reset_index()
    g = g.rename(columns={"top_theme": "theme"})
    return g.sort_values("median_mom", ascending=False).reset_index(drop=True)
