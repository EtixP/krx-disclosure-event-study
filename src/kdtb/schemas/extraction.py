from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

EventType = Literal[
    "major_supply_contract",
    "contract_revision",
    "contract_cancellation",
    "share_buyback",
    "share_cancellation",
    "dilutive_financing",
    "earnings",
    "other",
]

Direction = Literal["positive", "negative", "mixed", "unclear"]

ValidationStatus = Literal["ok", "needs_manual_review", "blocked"]


class Extraction(BaseModel):
    id: Optional[int] = None
    disclosure_id: int
    model_name: str
    prompt_version: str
    event_type: EventType
    direction: Direction
    confidence: float = Field(ge=0.0, le=1.0)
    contract_value_krw: Optional[int] = None
    prior_year_revenue_krw: Optional[int] = None
    contract_to_revenue_ratio: Optional[float] = None
    is_new_contract: Optional[bool] = None
    is_revision: Optional[bool] = None
    is_cancellation: Optional[bool] = None
    counterparty_name: Optional[str] = None
    counterparty_type: Optional[str] = None  # government | large_corp_korean | other_korean_corp | foreign | unknown
    red_flags: list[str] = Field(default_factory=list)
    summary: str = ""
    raw_llm_output: Optional[str] = None
    validation_status: ValidationStatus = "ok"
    validation_errors: list[str] = Field(default_factory=list)
    created_at: Optional[datetime] = None
