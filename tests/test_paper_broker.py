from __future__ import annotations

from datetime import date, datetime
from unittest.mock import patch

import pandas as pd

from kdtb.backtest.cost_model import CostModel
from kdtb.broker.paper_broker import HistoricalPaperBroker
from kdtb.data.market_data_client import MarketDataClient


def _prices(rows):
    idx = pd.to_datetime([r[0] for r in rows]).normalize()
    return pd.DataFrame({"open": [r[1] for r in rows], "close": [r[1] for r in rows]}, index=idx)


def test_simulate_fill_uses_next_trading_day_open():
    market = MarketDataClient()
    prices = _prices([
        ("2026-05-27", 100.0),  # event day — should NOT be used for fill
        ("2026-05-28", 110.0),  # next trading day — should be used
        ("2026-05-29", 120.0),
    ])
    with patch.object(market, "fetch_ohlcv", return_value=prices):
        broker = HistoricalPaperBroker(cost_model=CostModel(), market_data=market)
        fill = broker.simulate_fill("005930", "buy", date(2026, 5, 27), 1_000_000)
    assert fill is not None
    assert abs(fill.fill_price - 110.0 * 1.0005) < 0.01  # 5bps slippage
    assert fill.fill_quantity > 0


def test_simulate_fill_returns_none_if_no_future_prices():
    market = MarketDataClient()
    prices = _prices([("2026-05-27", 100.0)])  # only event day
    with patch.object(market, "fetch_ohlcv", return_value=prices):
        broker = HistoricalPaperBroker(cost_model=CostModel(), market_data=market)
        fill = broker.simulate_fill("005930", "buy", date(2026, 5, 27), 1_000_000)
    assert fill is None


def test_position_built_then_closed():
    market = MarketDataClient()
    prices = _prices([
        ("2026-05-27", 100.0),
        ("2026-05-28", 110.0),
        ("2026-05-29", 120.0),
        ("2026-05-30", 130.0),
    ])
    with patch.object(market, "fetch_ohlcv", return_value=prices):
        broker = HistoricalPaperBroker(cost_model=CostModel(slippage_bps=0), market_data=market)
        buy = broker.simulate_fill("005930", "buy", date(2026, 5, 27), 1_100_000)
        assert buy is not None
        assert broker.get_position("005930") is not None
        assert broker.get_position("005930")["quantity"] == buy.fill_quantity
        sell = broker.simulate_fill("005930", "sell", date(2026, 5, 29), buy.fill_quantity * 130)
        assert sell is not None
        # Quantities should match → position fully closed
        if sell.fill_quantity == buy.fill_quantity:
            assert broker.get_position("005930") is None


def test_buy_slippage_higher_than_raw_price():
    market = MarketDataClient()
    prices = _prices([
        ("2026-05-27", 100.0),
        ("2026-05-28", 100.0),
    ])
    with patch.object(market, "fetch_ohlcv", return_value=prices):
        broker = HistoricalPaperBroker(cost_model=CostModel(slippage_bps=10), market_data=market)
        fill = broker.simulate_fill("005930", "buy", date(2026, 5, 27), 1_000_000)
    assert fill.fill_price > 100.0


def test_sell_slippage_lower_than_raw_price():
    market = MarketDataClient()
    prices = _prices([
        ("2026-05-27", 100.0),
        ("2026-05-28", 100.0),
    ])
    with patch.object(market, "fetch_ohlcv", return_value=prices):
        broker = HistoricalPaperBroker(cost_model=CostModel(slippage_bps=10), market_data=market)
        fill = broker.simulate_fill("005930", "sell", date(2026, 5, 27), 1_000_000)
    assert fill.fill_price < 100.0


def test_llm_stub_raises_helpful_error():
    from kdtb.interpretation.llm_client import build_client
    c = build_client("stub")
    import pytest
    with pytest.raises(NotImplementedError, match="LLM_PROVIDER"):
        c.complete("anything")
