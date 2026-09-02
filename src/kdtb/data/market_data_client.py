"""Korean equity OHLCV via pykrx with an explicit adjustment policy."""
from __future__ import annotations

import logging
from datetime import date, timedelta
from enum import Enum
from typing import Optional

import pandas as pd
from pykrx.stock import get_market_ohlcv_by_date

logger = logging.getLogger(__name__)


class PriceAdjustment(str, Enum):
    """The two materially different pykrx daily-price routes.

    ``VENDOR_ADJUSTED`` maps to pykrx ``adjusted=True`` and, in pykrx 1.2.8,
    retrieves Naver Finance's retrospectively adjusted series. ``UNADJUSTED``
    maps to ``adjusted=False`` and the KRX raw-price route. Callers must choose;
    this wrapper deliberately has no implicit default.
    """

    VENDOR_ADJUSTED = "vendor_adjusted"
    UNADJUSTED = "unadjusted"

    @property
    def pykrx_adjusted(self) -> bool:
        return self is PriceAdjustment.VENDOR_ADJUSTED

    @property
    def source(self) -> str:
        if self is PriceAdjustment.VENDOR_ADJUSTED:
            return "NAVER_FINANCE_VIA_PYKRX"
        return "KRX_VIA_PYKRX"


# M0.4 fixes this before looking at revised research results. The historical
# event study measures announcement returns, not mechanical split/consolidation
# gaps, so it uses the continuity-preserving vendor-adjusted series. Stored
# observations remain the reproducible vintage because that series can revise.
RESEARCH_PRICE_ADJUSTMENT = PriceAdjustment.VENDOR_ADJUSTED


def _fmt(d: date) -> str:
    return d.strftime("%Y%m%d")


class MarketDataClient:
    """Thin wrapper over pykrx for daily OHLCV."""

    def fetch_ohlcv(
        self,
        stock_code: str,
        start: date,
        end: date,
        *,
        adjustment: PriceAdjustment,
    ) -> pd.DataFrame:
        """Returns DataFrame indexed by date with columns: open, high, low, close, volume.

        pykrx returns Korean column names; we rename to English. Empty frame if no data.
        The caller must select the adjustment route explicitly.
        """
        if not isinstance(adjustment, PriceAdjustment):
            raise TypeError("adjustment must be a PriceAdjustment")
        df = get_market_ohlcv_by_date(
            _fmt(start),
            _fmt(end),
            stock_code,
            freq="d",
            adjusted=adjustment.pykrx_adjusted,
            name_display=False,
        )
        if df is None or df.empty:
            normalized = pd.DataFrame(
                columns=["open", "high", "low", "close", "volume"]
            )
            normalized.attrs.update(
                price_adjustment=adjustment.value,
                price_source=adjustment.source,
            )
            return normalized
        df = df.rename(
            columns={"시가": "open", "고가": "high", "저가": "low", "종가": "close", "거래량": "volume"}
        )
        keep = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
        df = df[keep].copy()
        df.index = pd.to_datetime(df.index).normalize()
        df.index.name = "date"
        df.attrs.update(
            price_adjustment=adjustment.value,
            price_source=adjustment.source,
        )
        return df

    def fetch_window_around(
        self,
        stock_code: str,
        event_date: date,
        *,
        adjustment: PriceAdjustment,
        days_before: int = 5,
        days_after: int = 5,
    ) -> pd.DataFrame:
        """OHLCV spanning [event_date - days_before, event_date + days_after] (calendar days).

        Note: weekends/holidays are absent in the result (KRX is closed).
        """
        start = event_date - timedelta(days=days_before)
        end = event_date + timedelta(days=days_after)
        return self.fetch_ohlcv(
            stock_code,
            start,
            end,
            adjustment=adjustment,
        )

    @staticmethod
    def event_returns(
        prices: pd.DataFrame, event_date: date
    ) -> dict[str, Optional[float] | Optional[str]]:
        """Compute simple post-event returns relative to the event-day close.

        Returns prices, returns, and the exact trading date associated with
        each price observation. Missing values are None.
        """
        event_ts = pd.Timestamp(event_date)
        out: dict[str, Optional[float] | Optional[str]] = {
            "t0_date": None, "t+1_date": None, "t+2_date": None, "t+5_date": None,
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
        out["t0_date"] = on_or_after.index[0].date().isoformat()

        def _close_at_offset(n: int) -> Optional[float]:
            if len(on_or_after) <= n:
                return None
            return float(on_or_after.iloc[n]["close"])

        for label, n in (("t+1_close", 1), ("t+2_close", 2), ("t+5_close", 5)):
            out[label] = _close_at_offset(n)
        for label, n in (("t+1_date", 1), ("t+2_date", 2), ("t+5_date", 5)):
            if len(on_or_after) > n:
                out[label] = on_or_after.index[n].date().isoformat()
        for label, n in (("ret_1d", 1), ("ret_2d", 2), ("ret_5d", 5)):
            c = _close_at_offset(n)
            if c is not None and t0_close:
                out[label] = (c / t0_close) - 1.0
        return out
