from __future__ import annotations

from datetime import date

import pandas as pd

from kdtb.data.market_data_client import MarketDataClient


def _make_prices(rows: list[tuple[str, float]]) -> pd.DataFrame:
    idx = pd.to_datetime([r[0] for r in rows]).normalize()
    df = pd.DataFrame({"close": [r[1] for r in rows]}, index=idx)
    df.index.name = "date"
    return df


def test_event_returns_basic():
    prices = _make_prices([
        ("2026-05-26", 100.0),
        ("2026-05-27", 110.0),  # t0
        ("2026-05-28", 115.5),  # t+1 → +5%
        ("2026-05-29", 121.0),  # t+2 → +10%
        ("2026-06-01", 99.0),
        ("2026-06-02", 121.0),
        ("2026-06-03", 132.0),  # t+5 → +20%
    ])
    out = MarketDataClient.event_returns(prices, date(2026, 5, 27))
    assert out["t0_close"] == 110.0
    assert abs(out["ret_1d"] - 0.05) < 1e-9
    assert abs(out["ret_2d"] - 0.10) < 1e-9
    assert abs(out["ret_5d"] - 0.20) < 1e-9


def test_event_returns_handles_event_on_non_trading_day():
    # event date is Saturday; first trading day after is Monday
    prices = _make_prices([
        ("2026-05-29", 100.0),  # Fri before
        ("2026-06-01", 105.0),  # Mon — first on-or-after Saturday
        ("2026-06-02", 110.0),  # Tue
    ])
    out = MarketDataClient.event_returns(prices, date(2026, 5, 30))  # Sat
    assert out["t0_close"] == 105.0
    assert abs(out["ret_1d"] - (110.0 / 105.0 - 1)) < 1e-9


def test_event_returns_with_missing_future_data():
    prices = _make_prices([
        ("2026-05-27", 100.0),  # only t0
    ])
    out = MarketDataClient.event_returns(prices, date(2026, 5, 27))
    assert out["t0_close"] == 100.0
    assert out["ret_1d"] is None
    assert out["ret_5d"] is None


def test_event_returns_on_empty_frame():
    out = MarketDataClient.event_returns(pd.DataFrame(), date(2026, 5, 27))
    assert all(v is None for v in out.values())


def test_event_returns_event_before_all_data():
    prices = _make_prices([("2026-05-27", 100.0), ("2026-05-28", 105.0)])
    # Event date is before the first row → first on-or-after is the first row
    out = MarketDataClient.event_returns(prices, date(2026, 5, 26))
    assert out["t0_close"] == 100.0
    assert abs(out["ret_1d"] - 0.05) < 1e-9
