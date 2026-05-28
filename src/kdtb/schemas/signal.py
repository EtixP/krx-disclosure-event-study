from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

SignalDirection = Literal["long", "short"]
EntryType = Literal["market", "marketable_limit", "limit"]


class Signal(BaseModel):
    id: Optional[int] = None
    signal_id: str
    disclosure_id: int
    extraction_id: int
    stock_code: str
    strategy_name: str
    direction: SignalDirection
    strength: float = Field(ge=0.0, le=1.0)
    reason_codes: list[str] = Field(default_factory=list)
    entry_type: EntryType = "marketable_limit"
    max_entry_price: Optional[float] = None
    stop_loss_pct: float = Field(gt=0)
    take_profit_pct: float = Field(gt=0)
    time_exit: str = "market_close"
    notional_krw: Optional[int] = Field(default=None, ge=0)
    created_at: Optional[datetime] = None
