from __future__ import annotations

import sqlite3

import pytest

from kdtb.schemas.signal import Signal
from kdtb.storage.db import SCHEMA
from kdtb.storage.signal_store import SignalStore


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.executescript(SCHEMA)
    c.execute(
        "INSERT INTO disclosures (id, receipt_no, corp_code, corp_name, report_name, receipt_datetime) "
        "VALUES (1, '20260527000001', 'C1', 'Corp', 'rpt', '2026-05-27')"
    )
    c.execute(
        "INSERT INTO extractions (id, disclosure_id, model_name, prompt_version, event_type, direction, confidence, validation_status) "
        "VALUES (1, 1, 'parser', 'v1', 'major_supply_contract', 'positive', 0.95, 'ok')"
    )
    return c


def _signal(**overrides) -> Signal:
    defaults = dict(
        signal_id="20260527000001-major_supply_contract_v1",
        disclosure_id=1,
        extraction_id=1,
        stock_code="005930",
        strategy_name="major_supply_contract_v1",
        direction="long",
        strength=0.7,
        reason_codes=["ratio=0.14", "confidence=0.95"],
        stop_loss_pct=0.02,
        take_profit_pct=0.04,
    )
    defaults.update(overrides)
    return Signal(**defaults)


def test_upsert_inserts(conn):
    store = SignalStore(conn)
    rowid = store.upsert(_signal())
    assert rowid > 0
    assert store.count() == 1


def test_upsert_replaces_same_signal_id(conn):
    store = SignalStore(conn)
    store.upsert(_signal(strength=0.5))
    store.upsert(_signal(strength=0.9))
    assert store.count() == 1
    row = conn.execute("SELECT strength FROM signals").fetchone()
    assert row[0] == 0.9


def test_clear_strategy(conn):
    store = SignalStore(conn)
    store.upsert(_signal(signal_id="a", strategy_name="strat_a"))
    store.upsert(_signal(signal_id="b", strategy_name="strat_b"))
    cleared = store.clear_strategy("strat_a")
    assert cleared == 1
    assert store.count() == 1
    assert store.count_by_strategy("strat_b") == 1
