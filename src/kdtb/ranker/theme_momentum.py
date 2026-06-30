"""Measure theme strength from the price momentum of each theme's basket.

"Which themes are hot right now" is a FACT we can measure — the average trailing
momentum of the stocks in a theme — not an LLM opinion. Each stock then inherits
a `theme_raw` score (the strength of its themes), which the ranker turns into a
bounded, transparent theme-tilt factor.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from kdtb.ranker.themes import map_stock_to_themes, theme_name


def attach_themes(df: pd.DataFrame) -> pd.DataFrame:
    """Add a `themes` column (list of theme keys) per stock."""
    out = df.copy()
    out["themes"] = [
        map_stock_to_themes(r["stock_code"], r.get("corp_name", ""))
        for _, r in out.iterrows()
    ]
    return out


def theme_strength(df: pd.DataFrame, min_members: int = 3, agg: str = "mean") -> dict[str, float]:
    """theme_key -> basket trailing-momentum aggregate (themes with too few members dropped)."""
    if "themes" not in df.columns:
        df = attach_themes(df)
    rows: dict[str, list[float]] = {}
    for _, r in df.iterrows():
        mom = r.get("mom_12m")
        if mom is None or (isinstance(mom, float) and np.isnan(mom)):
            continue
        for k in r["themes"]:
            rows.setdefault(k, []).append(float(mom))
    out: dict[str, float] = {}
    for k, vals in rows.items():
        if len(vals) < min_members:
            continue
        out[k] = float(np.median(vals) if agg == "median" else np.mean(vals))
    return out


def attach_theme_score(df: pd.DataFrame, strengths: dict[str, float] | None = None,
                       min_members: int = 3) -> pd.DataFrame:
    """Add `theme_raw` (strength of a stock's best theme) and `top_theme` label.

    A stock's theme_raw is the MAX strength among its themes (a laggard in a hot
    theme still gets the hot-theme tilt). Stocks in no measured theme get NaN,
    which the ranker treats as neutral (reweighted out).
    """
    out = attach_themes(df) if "themes" not in df.columns else df.copy()
    strengths = theme_strength(out, min_members=min_members) if strengths is None else strengths

    theme_raw, top_theme = [], []
    for _, r in out.iterrows():
        scored = [(k, strengths[k]) for k in r["themes"] if k in strengths]
        if scored:
            best = max(scored, key=lambda kv: kv[1])
            theme_raw.append(best[1])
            top_theme.append(theme_name(best[0]))
        else:
            theme_raw.append(np.nan)
            top_theme.append("")
    out["theme_raw"] = theme_raw
    out["top_theme"] = top_theme
    return out


def hot_themes(strengths: dict[str, float], top_n: int = 8) -> list[tuple[str, float]]:
    """Return (theme_name_kr, strength) sorted strongest-first — the data-driven
    'what's hot right now' readout."""
    ranked = sorted(strengths.items(), key=lambda kv: kv[1], reverse=True)
    return [(theme_name(k), v) for k, v in ranked[:top_n]]
