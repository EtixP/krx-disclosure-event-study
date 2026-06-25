from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from kdtb.schemas import Order


class Broker(ABC):
    """Broker abstraction. All concrete brokers (paper, KIS, etc.) implement this."""

    name: str

    @abstractmethod
    def submit_order(self, order: Order) -> Order:
        """Submit and return the updated Order with broker_order_id and status."""

    @abstractmethod
    def cancel_order(self, broker_order_id: str) -> bool:
        ...

    @abstractmethod
    def get_position(self, stock_code: str) -> Optional[dict]:
        """Returns dict with quantity/average_price, or None if no position."""
