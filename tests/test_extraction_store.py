from __future__ import annotations

import json
import sqlite3

import pytest

from kdtb.schemas.extraction import Extraction
from kdtb.storage.db import SCHEMA
from kdtb.storage.extraction_store import ExtractionStore


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.executescript(SCHEMA)
    # extractions FK to disclosures, so add a parent row
    c.execute(
        "INSERT INTO disclosures (id, receipt_no, corp_code, corp_name, report_name, receipt_datetime) "
        "VALUES (1, '20260527000001', 'C1', 'Corp', 'rpt', '2026-05-27')"
    )
    return c


def _extraction(**overrides) -> Extraction:
    defaults = dict(
        disclosure_id=1,
        model_name="deterministic_supply_contract_v1",
        prompt_version="N/A",
        event_type="major_supply_contract",
        direction="positive",
        confidence=0.95,
        contract_value_krw=50_000_000_000,
        prior_year_revenue_krw=400_000_000_000,
        contract_to_revenue_ratio=0.125,
        is_new_contract=True,
        is_revision=False,
        is_cancellation=False,
        red_flags=[],
        summary="",
        validation_status="ok",
        validation_errors=[],
    )
    defaults.update(overrides)
    return Extraction(**defaults)


def test_upsert_inserts_row(conn):
    store = ExtractionStore(conn)
    rowid = store.upsert(_extraction())
    assert rowid > 0
    assert store.count_by_model("deterministic_supply_contract_v1") == 1


def test_upsert_replaces_same_model(conn):
    store = ExtractionStore(conn)
    store.upsert(_extraction(contract_value_krw=1))
    store.upsert(_extraction(contract_value_krw=2))
    assert store.count_by_model("deterministic_supply_contract_v1") == 1
    row = conn.execute("SELECT contract_value_krw FROM extractions").fetchone()
    assert row[0] == 2


def test_upsert_different_models_coexist(conn):
    store = ExtractionStore(conn)
    store.upsert(_extraction(model_name="deterministic_supply_contract_v1"))
    store.upsert(_extraction(model_name="claude-3-5-sonnet"))
    assert store.count_by_model("deterministic_supply_contract_v1") == 1
    assert store.count_by_model("claude-3-5-sonnet") == 1


def test_upsert_stores_json_fields(conn):
    store = ExtractionStore(conn)
    store.upsert(_extraction(red_flags=["x", "y"], validation_errors=["z"]))
    row = conn.execute("SELECT red_flags_json, validation_errors_json FROM extractions").fetchone()
    assert json.loads(row[0]) == ["x", "y"]
    assert json.loads(row[1]) == ["z"]
