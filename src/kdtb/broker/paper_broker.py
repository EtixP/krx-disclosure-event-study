"""Paper broker: simulated fills against historical (or live) market data.

For backtesting/event-study replay: when given a Signal, we simulate the fill
at the *next* available trading day's open price (most conservative — assumes
we couldn't act on the event-day close), plus a slippage buffer from the cost
model.

For live paper trading later: we'd use real-time bid/ask. That path is not
yet wired in.
"""
from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd

from kdtb.backtest.cost_model import CostModel
from kdtb.broker.base import Broker
from kdtb.data.market_data_client import MarketDataClient
from kdtb.schemas import Order

logger = logging.getLogger(__name__)


class PaperFill:
    """In-memory fill record used by the backtest path."""

    def __init__(
        self,
        broker_order_id: str,
        stock_code: str,
        side: str,
        fill_price: float,
        fill_quantity: int,
        fill_time: datetime,
        notional_krw: float,
        cost_krw: float,
    ) -> None:
        self.broker_order_id = broker_order_id
        self.stock_code = stock_code
        self.side = side
        self.fill_price = fill_price
        self.fill_quantity = fill_quantity
        self.fill_time = fill_time
        self.notional_krw = notional_krw
        self.cost_krw = cost_krw


class HistoricalPaperBroker(Broker):
    """Simulates fills using historical OHLCV. For event-study replay.

    Caches OHLCV per stock to avoid repeated pykrx calls.
    """

    name = "paper_historical"

    def __init__(
        self,
        cost_model: CostModel,
        market_data: Optional[MarketDataClient] = None,
    ) -> None:
        self.cost_model = cost_model
        self.market_data = market_data or MarketDataClient()
        self._cache: dict[str, pd.DataFrame] = {}
        self._positions: dict[str, dict] = {}
        self._fills: list[PaperFill] = []

    def submit_order(self, order: Order) -> Order:
        raise NotImplementedError(
            "HistoricalPaperBroker uses simulate_fill(signal_date, ...) for backtests; "
            "submit_order() is for the live-paper path which isn't wired yet."
        )

    def cancel_order(self, broker_order_id: str) -> bool:
        return False  # backtest path doesn't support cancellation

    def get_position(self, stock_code: str) -> Optional[dict]:
        return self._positions.get(stock_code)

    @property
    def fills(self) -> list[PaperFill]:
        return list(self._fills)

    def _prices_for(self, stock_code: str, around: date) -> pd.DataFrame:
        if stock_code not in self._cache:
            start = around - timedelta(days=5)
            end = around + timedelta(days=15)
            self._cache[stock_code] = self.market_data.fetch_ohlcv(stock_code, start, end)
        return self._cache[stock_code]

    def simulate_fill(
        self,
        stock_code: str,
        side: str,
        signal_date: date,
        notional_krw: float,
    ) -> Optional[PaperFill]:
        """Simulate a fill at the first trading day STRICTLY AFTER signal_date.

        Returns None if no future price data is available (stock didn't trade).
        """
        prices = self._prices_for(stock_code, signal_date)
        if prices.empty:
            return None
        future = prices.loc[prices.index > pd.Timestamp(signal_date)]
        if future.empty:
            return None
        fill_row = future.iloc[0]
        raw_price = float(fill_row["open"])
        slippage = self.cost_model.slippage_bps / 10000.0
        # buy slips up, sell slips down
        if side == "buy":
            fill_price = raw_price * (1 + slippage)
        else:
            fill_price = raw_price * (1 - slippage)
        quantity = max(1, int(notional_krw // fill_price))
        actual_notional = quantity * fill_price
        cost = self.cost_model.buy_cost(actual_notional) if side == "buy" else self.cost_model.sell_cost(actual_notional)
        fill = PaperFill(
            broker_order_id=str(uuid.uuid4()),
            stock_code=stock_code,
            side=side,
            fill_price=fill_price,
            fill_quantity=quantity,
            fill_time=datetime.combine(future.index[0].date(), datetime.min.time()),
            notional_krw=actual_notional,
            cost_krw=cost,
        )
        self._fills.append(fill)
        self._update_position(fill)
        return fill

    def _update_position(self, fill: PaperFill) -> None:
        pos = self._positions.get(fill.stock_code)
        if fill.side == "buy":
            if pos is None:
                self._positions[fill.stock_code] = {
                    "quantity": fill.fill_quantity,
                    "average_price": fill.fill_price,
                }
            else:
                total_q = pos["quantity"] + fill.fill_quantity
                new_avg = (
                    pos["quantity"] * pos["average_price"]
                    + fill.fill_quantity * fill.fill_price
                ) / total_q
                self._positions[fill.stock_code] = {
                    "quantity": total_q,
                    "average_price": new_avg,
                }
        else:  # sell
            if pos is None or pos["quantity"] < fill.fill_quantity:
                logger.warning("Sell without sufficient position: %s", fill.stock_code)
                return
            remaining = pos["quantity"] - fill.fill_quantity
            if remaining == 0:
                del self._positions[fill.stock_code]
            else:
                self._positions[fill.stock_code] = {
                    "quantity": remaining,
                    "average_price": pos["average_price"],
                }
