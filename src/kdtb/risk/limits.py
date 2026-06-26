from __future__ import annotations

from pydantic import BaseModel, Field


class RiskLimits(BaseModel):
    """Hard limits enforced by the risk engine."""

    max_order_value_krw: int = Field(gt=0)
    max_daily_loss_krw: int = Field(gt=0)
    max_open_positions: int = Field(ge=1)
    max_trades_per_day: int = Field(ge=1)
    min_llm_confidence: float = Field(ge=0, le=1)
    max_disclosure_age_minutes: int = Field(ge=0)
    no_short_selling: bool = True
    no_margin: bool = True
    no_derivatives: bool = True
    # Negative-event blacklist (updated Loop 12 iter 2/5 after re-validation).
    # If the subject stock has had a disclosure in one of these categories within
    # `blacklist_lookback_days`, the risk engine rejects the long signal.
    #
    # Lookback extended from 30 to 60 days because the negative effect of
    # shareholder_change persists and intensifies across the 4 most-recent
    # half-year windows (-2.41% to -3.36%), suggesting the depressive impact
    # lasts well beyond 30 days.
    blacklist_lookback_days: int = Field(default=60, ge=0)
    blacklisted_event_categories: list[str] = Field(
        default_factory=lambda: ["shareholder_change"]
    )
