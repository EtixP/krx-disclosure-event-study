from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from kdtb.schemas import (
    Disclosure,
    Extraction,
    Order,
    Position,
    Signal,
)


def test_disclosure_minimal():
    d = Disclosure(
        receipt_no="20260528000123",
        corp_code="00126380",
        corp_name="Samsung Electronics",
        report_name="단일판매·공급계약체결",
        receipt_datetime=datetime(2026, 5, 28, 10, 12),
    )
    assert d.market == "OTHER"
    assert d.source == "DART"


def test_extraction_rejects_invalid_confidence():
    with pytest.raises(ValidationError):
        Extraction(
            disclosure_id=1,
            model_name="x",
            prompt_version="v1",
            event_type="major_supply_contract",
            direction="positive",
            confidence=1.5,
        )


def test_signal_requires_positive_stops():
    with pytest.raises(ValidationError):
        Signal(
            signal_id="s1",
            disclosure_id=1,
            extraction_id=1,
            stock_code="005930",
            strategy_name="x",
            direction="long",
            strength=0.9,
            stop_loss_pct=0,
            take_profit_pct=0.04,
        )


def test_order_quantity_must_be_positive():
    with pytest.raises(ValidationError):
        Order(
            signal_id="s1",
            broker="kis",
            account_mode="paper",
            stock_code="005930",
            side="buy",
            order_type="limit",
            quantity=0,
        )


def test_position_defaults():
    p = Position(
        stock_code="005930",
        quantity=10,
        average_price=70000.0,
        opened_at=datetime(2026, 5, 28, 10, 15),
    )
    assert p.status == "open"
    assert p.realized_pnl == 0.0
