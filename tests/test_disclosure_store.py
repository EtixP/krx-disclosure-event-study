from __future__ import annotations

import sqlite3

import pytest

from kdtb.data.disclosure_store import DisclosureStore, dart_record_to_disclosure
from kdtb.storage.db import SCHEMA


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.executescript(SCHEMA)
    return c


def _sample_record(rcept_no: str = "20260528000001", corp_cls: str = "Y") -> dict:
    return {
        "corp_code": "00126380",
        "corp_name": "삼성전자",
        "stock_code": "005930",
        "corp_cls": corp_cls,
        "report_nm": "단일판매·공급계약체결",
        "rcept_no": rcept_no,
        "rcept_dt": "20260528",
    }


def test_dart_record_maps_to_disclosure():
    d = dart_record_to_disclosure(_sample_record())
    assert d.receipt_no == "20260528000001"
    assert d.corp_name == "삼성전자"
    assert d.stock_code == "005930"
    assert d.market == "KOSPI"
    assert d.receipt_datetime.year == 2026
    assert d.raw_payload["rcept_no"] == "20260528000001"


def test_market_mapping_kosdaq():
    d = dart_record_to_disclosure(_sample_record(corp_cls="K"))
    assert d.market == "KOSDAQ"


def test_market_unknown_falls_back_to_other():
    d = dart_record_to_disclosure(_sample_record(corp_cls="Z"))
    assert d.market == "OTHER"


def test_empty_stock_code_becomes_none():
    rec = _sample_record()
    rec["stock_code"] = ""
    d = dart_record_to_disclosure(rec)
    assert d.stock_code is None


def test_upsert_deduplicates(conn):
    store = DisclosureStore(conn)
    new, total = store.ingest_records([_sample_record(), _sample_record()])
    assert total == 2
    assert new == 1  # second one dedup'd
    assert store.count() == 1


def test_upsert_multiple_distinct(conn):
    store = DisclosureStore(conn)
    new, total = store.ingest_records(
        [_sample_record("20260528000001"), _sample_record("20260528000002")]
    )
    assert total == 2
    assert new == 2
    assert store.count() == 2
