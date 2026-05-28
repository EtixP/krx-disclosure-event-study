from __future__ import annotations

from datetime import datetime

from kdtb.risk.limits import RiskLimits
from kdtb.schemas import Extraction, Signal


def check_extraction_validated(extraction: Extraction) -> str | None:
    if extraction.validation_status != "ok":
        return f"extraction_not_validated:{extraction.validation_status}"
    return None


def check_confidence(extraction: Extraction, limits: RiskLimits) -> str | None:
    if extraction.confidence < limits.min_llm_confidence:
        return f"llm_confidence_below_threshold:{extraction.confidence:.2f}<{limits.min_llm_confidence:.2f}"
    return None


def check_disclosure_age(
    disclosure_time: datetime, now: datetime, limits: RiskLimits
) -> str | None:
    age_min = (now - disclosure_time).total_seconds() / 60.0
    if age_min < 0:
        return f"disclosure_in_future:{age_min:.1f}min"
    if age_min > limits.max_disclosure_age_minutes:
        return f"disclosure_too_old:{age_min:.1f}min>{limits.max_disclosure_age_minutes}min"
    return None


def check_direction_allowed(signal: Signal, limits: RiskLimits) -> str | None:
    if limits.no_short_selling and signal.direction == "short":
        return "short_selling_disabled"
    return None


def check_order_value(signal: Signal, limits: RiskLimits) -> str | None:
    if signal.notional_krw is None:
        return "notional_unknown"
    if signal.notional_krw > limits.max_order_value_krw:
        return f"order_value_exceeds_cap:{signal.notional_krw}>{limits.max_order_value_krw}"
    return None


def check_daily_loss(current_daily_loss_krw: int, limits: RiskLimits) -> str | None:
    if current_daily_loss_krw >= limits.max_daily_loss_krw:
        return f"daily_loss_limit_hit:{current_daily_loss_krw}>={limits.max_daily_loss_krw}"
    return None


def check_open_positions(open_positions: int, limits: RiskLimits) -> str | None:
    if open_positions >= limits.max_open_positions:
        return f"max_open_positions_hit:{open_positions}>={limits.max_open_positions}"
    return None


def check_trades_today(trades_today: int, limits: RiskLimits) -> str | None:
    if trades_today >= limits.max_trades_per_day:
        return f"max_trades_per_day_hit:{trades_today}>={limits.max_trades_per_day}"
    return None


def check_revision_or_cancellation(extraction: Extraction) -> str | None:
    if extraction.is_cancellation:
        return "disclosure_is_cancellation"
    if extraction.is_revision and extraction.is_new_contract is False:
        return "disclosure_is_revision_not_new"
    return None
