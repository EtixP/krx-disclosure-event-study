from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

PositionStatus = Literal["open", "closed"]


class Position(BaseModel):
    id: Optional[int] = None
    stock_code: str
    quantity: int = Field(ge=0)
    average_price: float = Field(ge=0)
    opened_at: datetime
    closed_at: Optional[datetime] = None
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    status: PositionStatus = "open"
