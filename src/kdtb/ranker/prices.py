"""Current price + trailing momentum per ticker (pykrx daily bars).

Uses the per-ticker daily endpoint (get_market_ohlcv_by_date), which works
without the bulk KRX snapshot APIs.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


def fetch_price_and_momentum(stock_code: str, asof: date, lookback_days: int = 400) -> tuple[Optional[float], Optional[float]]:
    """Return (latest_close, trailing_return).

    trailing_return = latest_close / earliest_close_in_window - 1, where the
    window is ~13 months so the reference is roughly 12 months back. None if no
    data. Momentum is a long-horizon factor — exact horizon isn't critical, the
    cross-sectional ordering is what matters.
    """
    from pykrx.stock import get_market_ohlcv_by_date

    start = (asof - timedelta(days=lookback_days)).strftime("%Y%m%d")
    end = asof.strftime("%Y%m%d")
    try:
        df = get_market_ohlcv_by_date(start, end, stock_code)
    except Exception as e:  # pykrx raises bare exceptions on empty/blocked
        logger.warning("price fetch failed for %s: %s", stock_code, e)
        return None, None
    if df is None or df.empty or "종가" not in df.columns:
        return None, None
    closes = df["종가"].astype(float)
    closes = closes[closes > 0]
    if len(closes) < 2:
        price = float(closes.iloc[-1]) if len(closes) else None
        return price, None
    price = float(closes.iloc[-1])
    ref = float(closes.iloc[0])
    mom = price / ref - 1.0 if ref > 0 else None
    return price, mom
