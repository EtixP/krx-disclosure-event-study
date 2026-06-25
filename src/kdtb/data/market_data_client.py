"""Korean equity OHLCV via pykrx (public scraping, no auth required).

pykrx prints `KRX 로그인 실패` on import — harmless, we use the public path.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

import pandas as pd
from pykrx.stock import get_market_ohlcv_by_date

logger = logging.getLogger(__name__)


def _fmt(d: date) -> str:
    return d.strftime("%Y%m%d")


class MarketDataClient:
    """Thin wrapper over pykrx for daily OHLCV."""

    def fetch_ohlcv(
        self,
        stock_code: str,
        start: date,
        end: date,
    ) -> pd.DataFrame:
        """Returns DataFrame indexed by date with columns: open, high, low, close, volume.

        pykrx returns Korean column names; we rename to English. Empty frame if no data.
        """
        df = get_market_ohlcv_by_date(_fmt(start), _fmt(end), stock_code)
        if df is None or df.empty:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        df = df.rename(
            columns={"시가": "open", "고가": "high", "저가": "low", "종가": "close", "거래량": "volume"}
        )
        keep = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
        df = df[keep].copy()
        df.index = pd.to_datetime(df.index).normalize()
        df.index.name = "date"
        return df

    def fetch_window_around(
        self,
        stock_code: str,
        event_date: date,
        days_before: int = 5,
        days_after: int = 5,
    ) -> pd.DataFrame:
        """OHLCV spanning [event_date - days_before, event_date + days_after] (calendar days).

        Note: weekends/holidays are absent in the result (KRX is closed).
        """
        start = event_date - timedelta(days=days_before)
        end = event_date + timedelta(days=days_after)
        return self.fetch_ohlcv(stock_code, start, end)

    @staticmethod
    def event_returns(prices: pd.DataFrame, event_date: date) -> dict[str, Optional[float]]:
        """Compute simple post-event returns relative to the event-day close.

        Returns a dict with keys: t0_close, t+1_close, t+2_close, t+5_close, ret_1d, ret_2d, ret_5d.
        Missing values are None (stock didn't trade that day).
        """
        event_ts = pd.Timestamp(event_date)
        out: dict[str, Optional[float]] = {
            "t0_close": None, "t+1_close": None, "t+2_close": None, "t+5_close": None,
            "ret_1d": None, "ret_2d": None, "ret_5d": None,
        }
        if prices.empty:
            return out
        on_or_after = prices.loc[prices.index >= event_ts]
        if on_or_after.empty:
            return out
        t0_close = float(on_or_after.iloc[0]["close"])
        out["t0_close"] = t0_close

        def _close_at_offset(n: int) -> Optional[float]:
            if len(on_or_after) <= n:
                return None
            return float(on_or_after.iloc[n]["close"])

        for label, n in (("t+1_close", 1), ("t+2_close", 2), ("t+5_close", 5)):
            out[label] = _close_at_offset(n)
        for label, n in (("ret_1d", 1), ("ret_2d", 2), ("ret_5d", 5)):
            c = _close_at_offset(n)
            if c is not None and t0_close:
                out[label] = (c / t0_close) - 1.0
        return out
