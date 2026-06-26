from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest

from kdtb.risk.event_blacklist import EventBlacklist, _matches_category
from kdtb.storage.db import SCHEMA


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.executescript(SCHEMA)
    return c


def _insert_disclosure(
    conn: sqlite3.Connection,
    receipt_no: str,
    stock_code: str,
    report_name: str,
    when: datetime,
    corp_code: str = "C1",
    corp_name: str = "Corp",
) -> None:
    conn.execute(
        """
        INSERT INTO disclosures
          (receipt_no, corp_code, corp_name, stock_code, report_name, receipt_datetime, market)
        VALUES (?, ?, ?, ?, ?, ?, 'KOSPI')
        """,
        (receipt_no, corp_code, corp_name, stock_code, report_name, when.isoformat()),
    )
    conn.commit()


def test_returns_none_when_no_recent_event(conn):
    bl = EventBlacklist(conn)
    now = datetime(2026, 6, 26, 10, 0)
    assert bl.has_recent_negative_event("005930", now, lookback_days=30) is None


def test_halt_resumption_NOT_in_default_blacklist(conn):
    """Loop 12 (iter 2/5) removed halt_resumption — refuted on all 3 adversarial lenses
    (regime_consistency 60% < 70%, sharpe-ish in noise band, structurally heterogeneous).
    Inserting a halt event for a stock should NOT trigger the default blacklist."""
    when = datetime(2026, 6, 20, 14, 0)
    _insert_disclosure(conn, "20260620100001", "005930", "매매거래정지(중요사항 공시 등)", when)
    bl = EventBlacklist(conn)
    now = datetime(2026, 6, 26, 10, 0)
    assert bl.has_recent_negative_event("005930", now, lookback_days=30) is None


def test_detects_halt_resumption_when_explicitly_added(conn):
    """halt_resumption is removed from the default blacklist as of Loop 12, but
    users can still add it explicitly if they want."""
    when = datetime(2026, 6, 20, 14, 0)
    _insert_disclosure(conn, "20260620100001", "005930", "매매거래정지(중요사항 공시 등)", when)
    bl = EventBlacklist(conn, categories=["shareholder_change", "halt_resumption"])
    now = datetime(2026, 6, 26, 10, 0)
    assert bl.has_recent_negative_event("005930", now, lookback_days=30) == "halt_resumption"


def test_detects_거래재개_when_explicitly_added(conn):
    when = datetime(2026, 6, 25, 14, 0)
    _insert_disclosure(conn, "20260625100002", "005930", "[기재정정]주식의 거래재개", when)
    bl = EventBlacklist(conn, categories=["halt_resumption"])
    hit = bl.has_recent_negative_event("005930", datetime(2026, 6, 26), lookback_days=7)
    assert hit == "halt_resumption"


def test_detects_shareholder_change(conn):
    when = datetime(2026, 6, 1, 9, 30)
    _insert_disclosure(conn, "20260601100003", "005930", "최대주주변경", when)
    bl = EventBlacklist(conn)
    hit = bl.has_recent_negative_event("005930", datetime(2026, 6, 26), lookback_days=30)
    assert hit == "shareholder_change"


def test_lookback_window_respected(conn):
    # Event 45 days ago, lookback 30 -> should NOT match
    when = datetime(2026, 5, 11, 9, 30)
    _insert_disclosure(conn, "20260511100004", "005930", "최대주주변경", when)
    bl = EventBlacklist(conn)
    assert bl.has_recent_negative_event("005930", datetime(2026, 6, 26), lookback_days=30) is None
    # Same call with 60-day lookback should match
    assert bl.has_recent_negative_event("005930", datetime(2026, 6, 26), lookback_days=60) == "shareholder_change"


def test_different_stock_does_not_match(conn):
    when = datetime(2026, 6, 20)
    _insert_disclosure(conn, "20260620100005", "005930", "매매거래정지", when)
    bl = EventBlacklist(conn)
    assert bl.has_recent_negative_event("999999", datetime(2026, 6, 26), lookback_days=30) is None


def test_revision_of_shareholder_change_not_flagged(conn):
    """Revisions of negative events shouldn't trigger — they're administrative noise."""
    when = datetime(2026, 6, 20)
    _insert_disclosure(conn, "20260620100006", "005930", "[기재정정]최대주주변경", when)
    bl = EventBlacklist(conn)
    # Pattern excludes 정정 — so revision shouldn't match shareholder_change.
    assert bl.has_recent_negative_event("005930", datetime(2026, 6, 26), lookback_days=30) is None


def test_supply_contract_not_in_default_blacklist(conn):
    """The blacklist is for NEGATIVE event categories only, not supply contracts."""
    when = datetime(2026, 6, 20)
    _insert_disclosure(conn, "20260620100007", "005930", "단일판매ㆍ공급계약체결", when)
    bl = EventBlacklist(conn)
    assert bl.has_recent_negative_event("005930", datetime(2026, 6, 26), lookback_days=30) is None


def test_custom_blacklist_categories(conn):
    """If user adds supply_contract to the blacklist, it should match."""
    when = datetime(2026, 6, 20)
    _insert_disclosure(conn, "20260620100008", "005930", "단일판매ㆍ공급계약체결", when)
    bl = EventBlacklist(conn, categories=["supply_contract", "halt_resumption"])
    assert bl.has_recent_negative_event("005930", datetime(2026, 6, 26), lookback_days=30) == "supply_contract"


def test_unknown_category_raises(conn):
    with pytest.raises(ValueError, match="Unknown blacklist categories"):
        EventBlacklist(conn, categories=["not_a_real_category"])


def test_empty_stock_code_returns_none(conn):
    when = datetime(2026, 6, 20)
    _insert_disclosure(conn, "20260620100009", "005930", "매매거래정지", when)
    bl = EventBlacklist(conn)
    assert bl.has_recent_negative_event("", datetime(2026, 6, 26), lookback_days=30) is None


def test_zero_lookback_returns_none(conn):
    when = datetime(2026, 6, 25)
    _insert_disclosure(conn, "20260625100010", "005930", "매매거래정지", when)
    bl = EventBlacklist(conn)
    assert bl.has_recent_negative_event("005930", datetime(2026, 6, 26), lookback_days=0) is None


def test_future_event_not_counted(conn):
    """A disclosure dated after `now` should NOT be returned — would be look-ahead."""
    future_when = datetime(2026, 6, 27)  # tomorrow
    _insert_disclosure(conn, "20260627100011", "005930", "매매거래정지", future_when)
    bl = EventBlacklist(conn)
    assert bl.has_recent_negative_event("005930", datetime(2026, 6, 26), lookback_days=30) is None


def test_picks_most_recent_when_multiple(conn):
    """Both halt_resumption and shareholder_change in scope; expects shareholder_change
    because halt is no longer in the default blacklist (Loop 12). Without halt in the
    default categories, the older halt event isn't even considered."""
    older = datetime(2026, 6, 10)
    newer = datetime(2026, 6, 24)
    _insert_disclosure(conn, "20260610100012", "005930", "매매거래정지", older)
    _insert_disclosure(conn, "20260624100013", "005930", "최대주주변경", newer)
    bl = EventBlacklist(conn)  # default = [shareholder_change] only
    hit = bl.has_recent_negative_event("005930", datetime(2026, 6, 26), lookback_days=30)
    assert hit == "shareholder_change"


# --- internal helper tests ---

def test_matches_category_halt():
    assert _matches_category("매매거래정지(중요사항 공시 등)", "halt_resumption")
    assert _matches_category("거래재개", "halt_resumption")
    assert not _matches_category("단일판매ㆍ공급계약체결", "halt_resumption")


def test_matches_category_shareholder_change():
    assert _matches_category("최대주주변경", "shareholder_change")
    assert not _matches_category("[기재정정]최대주주변경", "shareholder_change")
    assert not _matches_category("단일판매ㆍ공급계약체결", "shareholder_change")
