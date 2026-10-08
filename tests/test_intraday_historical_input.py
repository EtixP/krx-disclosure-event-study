from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import sys

import pandas as pd
import pytest

from scripts import run_intraday_walkforward as module


def test_default_historical_intraday_uses_pinned_times_without_any_db(monkeypatch):
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: pytest.fail("opened DB"))
    frame = module.load_timeaware()
    assert frame.attrs["db_used"] is False
    assert frame.attrs["filing_times_sha256"] == module.PINNED_FILING_TIMES_SHA256
    assert (
        frame["receipt_no"]
        .map(lambda value: isinstance(value, str) and len(value) == 14)
        .all()
    )
    assert (
        frame["stock_code"]
        .map(lambda value: isinstance(value, str) and len(value) == 6)
        .all()
    )
    matched = frame[frame.filing_mins.notna()]
    recorded = json.loads(
        (
            module.PROJECT_ROOT / "artifacts/m0_3/benchmark_adjustment_comparison.json"
        ).read_text()
    )
    assert len(matched) == recorded["buyback_intraday"]["events_with_filing_time"]
    # The documented default really reaches the pinned-data loader with no DB.
    monkeypatch.setattr(sys, "argv", [module.__file__])
    assert module.main() == 0


def test_pinned_input_keeps_leading_zero_identifiers(tmp_path, monkeypatch):
    csv = tmp_path / "events.csv"
    times = tmp_path / "times.csv"
    csv.write_text(
        "id,receipt_no,corp_code,stock_code,event_date,t0_close,t+1_close,t+5_close\n0001,00000000000001,00012345,001234,2024-01-02,1,2,3\n"
    )
    times.write_text("receipt_no,filing_time\n00000000000001,09:05\n")
    monkeypatch.setattr(module, "BUYBACK_CSV", csv)
    monkeypatch.setattr(module, "PINNED_FILING_TIMES", times)
    monkeypatch.setattr(
        module,
        "PINNED_FILING_TIMES_SHA256",
        hashlib.sha256(times.read_bytes()).hexdigest(),
    )
    monkeypatch.setattr(module, "apply_timeaware_returns", lambda frame: frame)
    row = module.load_timeaware().iloc[0]
    assert row.id == "0001" and row.receipt_no == "00000000000001"
    assert row.stock_code == "001234" and row.corp_code == "00012345"
    assert row.filing_mins == 545


def test_changed_pinned_times_fail_without_db_fallback(tmp_path, monkeypatch):
    times = tmp_path / "times.csv"
    times.write_text("receipt_no,filing_time\n20240102000001,09:00\n")
    monkeypatch.setattr(module, "PINNED_FILING_TIMES", times)
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: pytest.fail("opened DB"))
    with pytest.raises(ValueError, match="hash_mismatch"):
        module.load_timeaware()


def test_supply_cli_fails_concisely_without_reading_csv_or_db(monkeypatch, capsys):
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: pytest.fail("opened DB"))
    monkeypatch.setattr(
        pd, "read_csv", lambda *a, **kw: pytest.fail("read CSV before rejection")
    )
    monkeypatch.setattr(sys, "argv", [module.__file__, "--category", "supply_contract"])
    assert module.main() == 1
    output = capsys.readouterr().out
    assert "supply_contract_unversioned_enrichment" in output
    assert "Traceback" not in output


def test_legacy_db_option_is_removed(monkeypatch):
    monkeypatch.setattr(sys, "argv", [module.__file__, "--db", "data/kdtb.db"])
    with pytest.raises(SystemExit) as exc:
        module.parse_args()
    assert exc.value.code == 2
