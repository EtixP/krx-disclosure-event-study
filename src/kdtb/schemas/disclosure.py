from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

Market = Literal["KOSPI", "KOSDAQ", "KONEX", "OTHER"]


class Disclosure(BaseModel):
    id: Optional[int] = None
    receipt_no: str
    corp_code: str
    corp_name: str
    stock_code: Optional[str] = None
    report_name: str
    receipt_datetime: datetime
    market: Market = "OTHER"
    source: str = "DART"
    raw_url: Optional[str] = None
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    raw_text: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
