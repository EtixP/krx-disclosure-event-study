"""Backtest summary metrics over a series of trade returns.

These are *per-trade* metrics, not time-series (no rebalancing assumption).
For a proper Sharpe ratio you'd want a daily-PnL time series; for an
event-study system, win rate / profit factor / mean+std are the
right primitives.
"""
from __future__ import annotations

import math
from typing import Iterable

from pydantic import BaseModel


class TradeMetrics(BaseModel):
    n_trades: int
    win_rate: float
    mean_return: float
    median_return: float
    std_return: float
    p25_return: float
    p75_return: float
    average_win: float
    average_loss: float
    profit_factor: float | None  # None if no losses
    sharpe_like: float | None  # mean/std (not annualized)
    max_drawdown: float  # running cumulative-sum max drawdown
    total_return: float  # sum of returns (simple, not compounded)


def _percentile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = (len(sorted_values) - 1) * q
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    if lo == hi:
        return sorted_values[lo]
    frac = pos - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


def _max_drawdown(returns: list[float]) -> float:
    """Max drawdown over the cumulative sum of returns (simple, not compounded)."""
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in returns:
        cum += r
        peak = max(peak, cum)
        max_dd = min(max_dd, cum - peak)
    return max_dd


def compute(returns: Iterable[float]) -> TradeMetrics:
    """Compute trade metrics from a sequence of per-trade returns (e.g. 0.02 = +2%)."""
    rs = [float(r) for r in returns if r is not None and not math.isnan(r)]
    n = len(rs)
    if n == 0:
        return TradeMetrics(
            n_trades=0, win_rate=0, mean_return=0, median_return=0, std_return=0,
            p25_return=0, p75_return=0, average_win=0, average_loss=0,
            profit_factor=None, sharpe_like=None, max_drawdown=0, total_return=0,
        )
    rs_sorted = sorted(rs)
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r < 0]
    mean = sum(rs) / n
    var = sum((r - mean) ** 2 for r in rs) / n if n > 1 else 0.0
    std = math.sqrt(var)
    total_wins = sum(wins)
    total_losses = abs(sum(losses))
    return TradeMetrics(
        n_trades=n,
        win_rate=len(wins) / n,
        mean_return=mean,
        median_return=_percentile(rs_sorted, 0.5),
        std_return=std,
        p25_return=_percentile(rs_sorted, 0.25),
        p75_return=_percentile(rs_sorted, 0.75),
        average_win=(total_wins / len(wins)) if wins else 0.0,
        average_loss=(sum(losses) / len(losses)) if losses else 0.0,
        profit_factor=(total_wins / total_losses) if total_losses > 0 else None,
        sharpe_like=(mean / std) if std > 0 else None,
        max_drawdown=_max_drawdown(rs),
        total_return=sum(rs),
    )
