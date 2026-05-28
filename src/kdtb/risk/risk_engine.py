from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from kdtb.risk import checks
from kdtb.risk.limits import RiskLimits
from kdtb.schemas import Extraction, Signal


class RiskContext(BaseModel):
    """Inputs the risk engine evaluates against."""

    signal: Signal
    extraction: Extraction
    disclosure_time: datetime
    now: datetime
    current_daily_loss_krw: int = Field(default=0, ge=0)
    open_positions: int = Field(default=0, ge=0)
    trades_today: int = Field(default=0, ge=0)


class RiskDecision(BaseModel):
    approved: bool
    rejection_reasons: list[str] = []

    @classmethod
    def approve(cls) -> "RiskDecision":
        return cls(approved=True, rejection_reasons=[])

    @classmethod
    def reject(cls, reasons: list[str]) -> "RiskDecision":
        return cls(approved=False, rejection_reasons=reasons)


class RiskEngine:
    def __init__(self, limits: RiskLimits) -> None:
        self.limits = limits

    def evaluate(self, ctx: RiskContext) -> RiskDecision:
        reasons: list[Optional[str]] = [
            checks.check_extraction_validated(ctx.extraction),
            checks.check_confidence(ctx.extraction, self.limits),
            checks.check_disclosure_age(ctx.disclosure_time, ctx.now, self.limits),
            checks.check_direction_allowed(ctx.signal, self.limits),
            checks.check_order_value(ctx.signal, self.limits),
            checks.check_daily_loss(ctx.current_daily_loss_krw, self.limits),
            checks.check_open_positions(ctx.open_positions, self.limits),
            checks.check_trades_today(ctx.trades_today, self.limits),
            checks.check_revision_or_cancellation(ctx.extraction),
        ]
        failures = [r for r in reasons if r is not None]
        if failures:
            return RiskDecision.reject(failures)
        return RiskDecision.approve()
