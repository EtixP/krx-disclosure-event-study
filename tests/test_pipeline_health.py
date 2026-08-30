"""Tests for the daily monitor orchestration logic.

The monitor talks to DART, the parser, the strategy, the risk engine, and SQLite.
For unit tests we exercise the per-piece functions with an in-memory SQLite +
synthetic disclosures and extractions, no network calls.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, time

import pytest

import scripts.check_pipeline_health as monitor
from kdtb.config import StrategyParams
from kdtb.risk import EventBlacklist, RiskEngine
from kdtb.risk.limits import RiskLimits
from kdtb.storage.db import SCHEMA
from kdtb.strategy.major_supply_contract import MajorSupplyContractStrategy


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript(SCHEMA)
    return c


def _insert(conn, disc_id, receipt_no, stock_code, report_name, when, market="KOSPI"):
    conn.execute(
        "INSERT INTO disclosures (id, receipt_no, corp_code, corp_name, stock_code, report_name, receipt_datetime, market) "
        "VALUES (?, ?, 'C', 'Corp', ?, ?, ?, ?)",
        (disc_id, receipt_no, stock_code, report_name, when.isoformat(), market),
    )


def _insert_extraction(conn, ext_id, disc_id, **fields):
    defaults = dict(
        model_name="deterministic_supply_contract_v1",
        prompt_version="N/A",
        event_type="major_supply_contract",
        direction="positive",
        confidence=0.95,
        contract_value_krw=10_000_000_000,
        prior_year_revenue_krw=80_000_000_000,
        contract_to_revenue_ratio=0.125,
        is_new_contract=1,
        is_revision=0,
        is_cancellation=0,
        counterparty_name="삼성전자",
        counterparty_type="large_corp_korean",
        red_flags_json="[]",
        summary="test",
        validation_status="ok",
        validation_errors_json="[]",
    )
    defaults.update(fields)
    cols = ", ".join(["id", "disclosure_id"] + list(defaults.keys()))
    placeholders = ", ".join(["?"] * (len(defaults) + 2))
    conn.execute(
        f"INSERT INTO extractions ({cols}) VALUES ({placeholders})",
        (ext_id, disc_id, *defaults.values()),
    )
    conn.commit()


def _strategy() -> MajorSupplyContractStrategy:
    return MajorSupplyContractStrategy(StrategyParams(
        enabled=True,
        min_contract_to_revenue_ratio=0.08,
        min_llm_confidence=0.80,
        max_price_move_after_disclosure_pct=0.08,
        stop_loss_pct=0.02,
        take_profit_pct=0.04,
        kospi_only=True,
        excluded_counterparty_types=["government"],
    ))


def _engine(conn) -> RiskEngine:
    limits = RiskLimits(
        max_order_value_krw=30_000,
        max_daily_loss_krw=10_000,
        max_open_positions=1,
        max_trades_per_day=3,
        min_llm_confidence=0.80,
        max_disclosure_age_minutes=24 * 60,
    )
    return RiskEngine(limits, blacklist=EventBlacklist(conn))


def test_evaluate_signals_emits_approved_when_clean(conn):
    when = datetime(2026, 6, 26, 10, 0)
    _insert(conn, 1, "20260626100001", "005930", "단일판매ㆍ공급계약체결", when)
    _insert_extraction(conn, 1, 1)
    outcomes = monitor._evaluate_signals(conn, date(2026, 6, 26), _strategy(), _engine(conn))
    assert len(outcomes) == 1
    assert outcomes[0].stock_code == "005930"
    assert outcomes[0].risk_decision.approved


def test_evaluate_signals_filters_out_kosdaq(conn):
    when = datetime(2026, 6, 26)
    _insert(conn, 1, "20260626100001", "066970", "단일판매ㆍ공급계약체결", when, market="KOSDAQ")
    _insert_extraction(conn, 1, 1)
    outcomes = monitor._evaluate_signals(conn, date(2026, 6, 26), _strategy(), _engine(conn))
    assert outcomes == []  # KOSPI-only filter strips it before risk eval


def test_evaluate_signals_filters_out_government_counterparty(conn):
    when = datetime(2026, 6, 26)
    _insert(conn, 1, "20260626100001", "005930", "단일판매ㆍ공급계약체결", when)
    _insert_extraction(conn, 1, 1, counterparty_type="government", counterparty_name="국민건강보험공단")
    outcomes = monitor._evaluate_signals(conn, date(2026, 6, 26), _strategy(), _engine(conn))
    assert outcomes == []


def test_evaluate_signals_blocked_by_blacklist(conn):
    target = date(2026, 6, 26)
    # Insert a recent shareholder_change for the same stock — should trip the blacklist
    _insert(conn, 1, "20260620100001", "005930", "최대주주변경", datetime(2026, 6, 20))
    _insert(conn, 2, "20260626100002", "005930", "단일판매ㆍ공급계약체결", datetime(2026, 6, 26, 10))
    _insert_extraction(conn, 1, 2)
    outcomes = monitor._evaluate_signals(conn, target, _strategy(), _engine(conn))
    assert len(outcomes) == 1
    assert not outcomes[0].risk_decision.approved
    assert any("recent_negative_event" in r for r in outcomes[0].risk_decision.rejection_reasons)


def test_evaluate_signals_ratio_below_threshold_filtered(conn):
    when = datetime(2026, 6, 26)
    _insert(conn, 1, "20260626100001", "005930", "단일판매ㆍ공급계약체결", when)
    _insert_extraction(conn, 1, 1, contract_to_revenue_ratio=0.05)
    outcomes = monitor._evaluate_signals(conn, date(2026, 6, 26), _strategy(), _engine(conn))
    assert outcomes == []


def test_evaluate_signals_other_dates_ignored(conn):
    _insert(conn, 1, "20260615100001", "005930", "단일판매ㆍ공급계약체결", datetime(2026, 6, 15))
    _insert_extraction(conn, 1, 1)
    outcomes = monitor._evaluate_signals(conn, date(2026, 6, 26), _strategy(), _engine(conn))
    assert outcomes == []


def test_supply_contract_disclosures_needing_parse_returns_unparsed(conn):
    _insert(conn, 1, "20260626100001", "005930", "단일판매ㆍ공급계약체결", datetime(2026, 6, 26))
    _insert(conn, 2, "20260626100002", "012345", "단일판매ㆍ공급계약체결", datetime(2026, 6, 26))
    # Only the first one has an extraction
    _insert_extraction(conn, 99, 1)
    pending = monitor._supply_contract_disclosures_needing_parse(conn, date(2026, 6, 26))
    assert len(pending) == 1
    assert pending[0]["receipt_no"] == "20260626100002"


def test_target_date_default_is_today():
    today = monitor._target_date(None)
    assert today == date.today()


def test_target_date_parses_explicit_arg():
    assert monitor._target_date("2026-05-15") == date(2026, 5, 15)
