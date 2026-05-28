from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from kdtb.risk import RiskContext, RiskEngine
from kdtb.risk.limits import RiskLimits
from kdtb.schemas import Extraction, Signal


@pytest.fixture
def limits() -> RiskLimits:
    return RiskLimits(
        max_order_value_krw=30000,
        max_daily_loss_krw=10000,
        max_open_positions=1,
        max_trades_per_day=3,
        min_llm_confidence=0.80,
        max_disclosure_age_minutes=30,
    )


def _make_extraction(**overrides) -> Extraction:
    defaults = dict(
        disclosure_id=1,
        model_name="test",
        prompt_version="v1",
        event_type="major_supply_contract",
        direction="positive",
        confidence=0.9,
        contract_value_krw=85_000_000_000,
        prior_year_revenue_krw=600_000_000_000,
        contract_to_revenue_ratio=0.142,
        is_new_contract=True,
        is_revision=False,
        is_cancellation=False,
        validation_status="ok",
    )
    defaults.update(overrides)
    return Extraction(**defaults)


def _make_signal(**overrides) -> Signal:
    defaults = dict(
        signal_id="sig-1",
        disclosure_id=1,
        extraction_id=1,
        stock_code="005930",
        strategy_name="major_supply_contract_v1",
        direction="long",
        strength=0.9,
        stop_loss_pct=0.02,
        take_profit_pct=0.04,
        notional_krw=25_000,
    )
    defaults.update(overrides)
    return Signal(**defaults)


def _ctx(limits, **overrides) -> RiskContext:
    now = datetime(2026, 5, 28, 10, 20)
    defaults = dict(
        signal=_make_signal(),
        extraction=_make_extraction(),
        disclosure_time=now - timedelta(minutes=10),
        now=now,
        current_daily_loss_krw=0,
        open_positions=0,
        trades_today=0,
    )
    defaults.update(overrides)
    return RiskContext(**defaults)


def test_approves_clean_signal(limits):
    engine = RiskEngine(limits)
    decision = engine.evaluate(_ctx(limits))
    assert decision.approved
    assert decision.rejection_reasons == []


def test_rejects_low_confidence(limits):
    engine = RiskEngine(limits)
    ctx = _ctx(limits, extraction=_make_extraction(confidence=0.5))
    decision = engine.evaluate(ctx)
    assert not decision.approved
    assert any("llm_confidence_below_threshold" in r for r in decision.rejection_reasons)


def test_rejects_oversize_order(limits):
    engine = RiskEngine(limits)
    ctx = _ctx(limits, signal=_make_signal(notional_krw=50_000))
    decision = engine.evaluate(ctx)
    assert not decision.approved
    assert any("order_value_exceeds_cap" in r for r in decision.rejection_reasons)


def test_rejects_stale_disclosure(limits):
    engine = RiskEngine(limits)
    now = datetime(2026, 5, 28, 11, 0)
    ctx = _ctx(limits, now=now, disclosure_time=now - timedelta(minutes=120))
    decision = engine.evaluate(ctx)
    assert not decision.approved
    assert any("disclosure_too_old" in r for r in decision.rejection_reasons)


def test_rejects_when_daily_loss_hit(limits):
    engine = RiskEngine(limits)
    ctx = _ctx(limits, current_daily_loss_krw=10_000)
    decision = engine.evaluate(ctx)
    assert not decision.approved
    assert any("daily_loss_limit_hit" in r for r in decision.rejection_reasons)


def test_rejects_when_position_already_open(limits):
    engine = RiskEngine(limits)
    ctx = _ctx(limits, open_positions=1)
    decision = engine.evaluate(ctx)
    assert not decision.approved
    assert any("max_open_positions_hit" in r for r in decision.rejection_reasons)


def test_rejects_cancellation_event(limits):
    engine = RiskEngine(limits)
    ctx = _ctx(
        limits,
        extraction=_make_extraction(
            is_new_contract=False, is_cancellation=True, event_type="contract_cancellation"
        ),
    )
    decision = engine.evaluate(ctx)
    assert not decision.approved
    assert any("disclosure_is_cancellation" in r for r in decision.rejection_reasons)


def test_rejects_short_when_disabled(limits):
    engine = RiskEngine(limits)
    ctx = _ctx(limits, signal=_make_signal(direction="short"))
    decision = engine.evaluate(ctx)
    assert not decision.approved
    assert any("short_selling_disabled" in r for r in decision.rejection_reasons)


def test_rejects_blocked_extraction(limits):
    engine = RiskEngine(limits)
    ctx = _ctx(limits, extraction=_make_extraction(validation_status="blocked"))
    decision = engine.evaluate(ctx)
    assert not decision.approved
    assert any("extraction_not_validated" in r for r in decision.rejection_reasons)
