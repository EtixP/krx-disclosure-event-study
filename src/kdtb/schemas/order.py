from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

OrderSide = Literal["buy", "sell"]
OrderType = Literal["market", "limit", "marketable_limit"]
OrderStatus = Literal["pending", "submitted", "partial", "filled", "cancelled", "rejected"]
AccountMode = Literal["paper", "live_tiny", "live"]


class Order(BaseModel):
    id: Optional[int] = None
    signal_id: str
    broker: str
    account_mode: AccountMode
    stock_code: str
    side: OrderSide
    order_type: OrderType
    quantity: int = Field(gt=0)
    limit_price: Optional[float] = None
    submitted_at: Optional[datetime] = None
    broker_order_id: Optional[str] = None
    status: OrderStatus = "pending"
    raw_response: dict[str, Any] = Field(default_factory=dict)
