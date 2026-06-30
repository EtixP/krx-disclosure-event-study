"""Multi-factor Korean equity ranker.

Ranks KOSPI/KOSDAQ stocks by a composite of three economically-motivated
factor groups — VALUE (cheap), QUALITY (healthy), MOMENTUM (rising) — and
produces an explainable watchlist sorted by score.

This is a longer-horizon portfolio tool, NOT a short-term trading signal: the
factor premia it targets play out over months, so the capacity / fill problems
that sink fast event-driven strategies do not apply. Factors are standardized
by cross-sectional percentile rank (robust to the fat tails of financial
ratios) and combined with configurable weights.

Data foundation (works without paid feeds):
- DART financials (equity, net income, revenue, debt) + shares outstanding
- per-ticker daily prices (pykrx) for the current price and 12-month momentum
"""
from kdtb.ranker.ranker import (
    FactorWeights,
    RankFilters,
    compute_factor_columns,
    rank_universe,
)

__all__ = [
    "FactorWeights",
    "RankFilters",
    "compute_factor_columns",
    "rank_universe",
]
