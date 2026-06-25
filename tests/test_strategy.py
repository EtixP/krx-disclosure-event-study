from __future__ import annotations

from datetime import datetime

import pytest

from kdtb.config import StrategyParams
from kdtb.schemas import Disclosure, Extraction
from kdtb.strategy.major_supply_contract import MajorSupplyContractStrategy


@pytest.fixture
def params() -> StrategyParams:
    return StrategyParams(
        enabled=True,
        min_contract_to_revenue_ratio=0.08,
        min_llm_confidence=0.80,
        max_price_move_after_disclosure_pct=0.08,
        stop_loss_pct=0.02,
        take_profit_pct=0.04,
    )


def _disclosure(**overrides) -> Disclosure:
    defaults = dict(
        id=1,
        receipt_no="20260527000001",
        corp_code="00126380",
        corp_name="Samsung Electronics",
        stock_code="005930",
        report_name="단일판매ㆍ공급계약체결",
        receipt_datetime=datetime(2026, 5, 27, 10, 15),
        market="KOSPI",
    )
    defaults.update(overrides)
    return Disclosure(**defaults)


def _extraction(**overrides) -> Extraction:
    defaults = dict(
        id=1,
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


def test_emits_signal_when_all_rules_pass(params):
    s = MajorSupplyContractStrategy(params)
    sig = s.evaluate(_extraction(), _disclosure())
    assert sig is not None
    assert sig.direction == "long"
    assert sig.stock_code == "005930"
    assert sig.signal_id.startswith("20260527000001-")
    assert sig.strength > 0
    assert any("ratio" in r for r in sig.reason_codes)


def test_no_signal_when_disabled(params):
    params.enabled = False
    s = MajorSupplyContractStrategy(params)
    assert s.evaluate(_extraction(), _disclosure()) is None


def test_no_signal_when_ratio_below_threshold(params):
    s = MajorSupplyContractStrategy(params)
    assert s.evaluate(_extraction(contract_to_revenue_ratio=0.05), _disclosure()) is None


def test_no_signal_when_confidence_too_low(params):
    s = MajorSupplyContractStrategy(params)
    assert s.evaluate(_extraction(confidence=0.5), _disclosure()) is None


def test_no_signal_for_cancellation(params):
    s = MajorSupplyContractStrategy(params)
    assert s.evaluate(_extraction(is_cancellation=True), _disclosure()) is None


def test_no_signal_for_pure_revision(params):
    s = MajorSupplyContractStrategy(params)
    ex = _extraction(is_new_contract=False, is_revision=True)
    assert s.evaluate(ex, _disclosure()) is None


def test_no_signal_when_event_type_wrong(params):
    s = MajorSupplyContractStrategy(params)
    assert s.evaluate(_extraction(event_type="earnings"), _disclosure()) is None


def test_no_signal_when_ratio_missing(params):
    s = MajorSupplyContractStrategy(params)
    assert s.evaluate(_extraction(contract_to_revenue_ratio=None), _disclosure()) is None


def test_no_signal_when_no_stock_code(params):
    s = MajorSupplyContractStrategy(params)
    assert s.evaluate(_extraction(), _disclosure(stock_code=None)) is None


def test_strength_caps_at_one(params):
    s = MajorSupplyContractStrategy(params)
    sig = s.evaluate(_extraction(contract_to_revenue_ratio=0.5), _disclosure())
    assert sig is not None
    assert sig.strength == 1.0
