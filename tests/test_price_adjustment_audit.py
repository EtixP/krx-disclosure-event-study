from __future__ import annotations

import ast
import json
import sqlite3
from importlib.metadata import version
from pathlib import Path

import pytest

from kdtb.research.baseline import sha256_file, write_json
from scripts.audit_price_adjustments import build_audit, capture_right_drop_calendar


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = PROJECT_ROOT / "artifacts/m0_4/price_adjustment_audit.json"
RIGHT_DROP_CALENDAR = PROJECT_ROOT / "artifacts/m0_4/dart_right_drop_calendar.json"


def test_committed_price_adjustment_audit_regenerates_and_reconciles(tmp_path):
    recorded = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    regenerated = tmp_path / "price_adjustment_audit.json"
    write_json(regenerated, build_audit())
    assert json.loads(regenerated.read_text(encoding="utf-8")) == recorded
    assert recorded["schema_version"] == 2

    assert recorded["methodology"] == {
        "provider_route": "NAVER_FINANCE_VIA_PYKRX",
        "pykrx_adjusted_argument": True,
        "reason": (
            "announcement returns use a continuity-preserving adjusted series; "
            "stored observations pin the provider vintage"
        ),
        "research_policy": "vendor_adjusted",
        "pykrx_version_pin": "1.2.8",
    }
    assert version("pykrx") == recorded["methodology"]["pykrx_version_pin"]
    categories = {row["category"]: row for row in recorded["categories"]}
    assert categories.keys() == {"bonus_issue", "rights_offering"}
    assert categories["bonus_issue"]["m0_3_headline_unchanged"] is True
    assert categories["rights_offering"]["m0_3_headline_unchanged"] is True
    summary = recorded["right_drop_calendar_summary"]
    assert summary["committed_category_union"] == {
        "records": 441,
        "crossed_complete_windows": 70,
        "crossed_complete_windows_by_category": {
            "bonus_issue": 4,
            "rights_offering": 66,
        },
    }
    assert summary["pinned_local_dart"]["records"] == 499
    assert summary["pinned_local_dart"]["crossed_complete_windows"] == 73
    assert summary["pinned_local_dart"][
        "crossed_complete_windows_by_category"
    ] == {"bonus_issue": 4, "rights_offering": 69}
    assert summary["comparison"] == {
        "dart_records_absent_from_committed_categories": 58,
        "dart_crossed_windows_beyond_committed_union": 3,
    }

    for category, expected in {
        "bonus_issue": (4, 4),
        "rights_offering": (66, 69),
    }.items():
        crossings = categories[category]["right_drop_crossings"]
        assert len(crossings["committed_category_union"]) == expected[0]
        assert len(crossings["pinned_local_dart"]) == expected[1]

    rights_crossings = categories["rights_offering"]["right_drop_crossings"]
    for calendar_name in ("committed_category_union", "pinned_local_dart"):
        omitted_regression = [
            row
            for row in rights_crossings[calendar_name]
            if row["receipt_no"] == "20260219900633"
        ]
        assert len(omitted_regression) == 1
        assert [
            event["receipt_no"]
            for event in omitted_regression[0]["right_drop_events"]
        ] == ["20260226901417"]

    cases = {
        row["case_id"]: row
        for row in recorded["provider_observation_summary"]["cases"]
    }
    revision = cases["later_consolidation_uniform_revision"]
    assert revision["relationship"] == "uniform_scale_revision"
    assert revision["scale_factor"] == 5.0
    assert revision["max_absolute_return_change"] == 0.0
    cross_category_revision = cases[
        "cross_category_rights_window_uniform_revision_300120"
    ]
    assert cross_category_revision["matched_categories"] == ["rights_offering"]
    assert cross_category_revision["relationship"] == "uniform_scale_revision"
    assert cross_category_revision["scale_factor"] == 5.0
    assert cross_category_revision["max_absolute_return_change"] == 0.0
    assert sum(row["matched_category_rows"] for row in cases.values()) == 9
    assert all(row["max_absolute_return_change"] == 0 for row in cases.values())

    for group in ("inputs", "generator_sources"):
        for record in recorded[group]:
            assert sha256_file(PROJECT_ROOT / record["path"]) == record["sha256"]


def test_right_drop_calendar_capture_is_source_ordered_and_lossless(tmp_path):
    database = tmp_path / "dart.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE disclosures (
                receipt_no TEXT,
                stock_code TEXT,
                report_name TEXT,
                receipt_datetime TEXT
            )
            """
        )
        connection.executemany(
            "INSERT INTO disclosures VALUES (?, ?, ?, ?)",
            [
                ("2", "300120", "권리락(무상증자)", "2026-02-26T00:00:00"),
                ("1", "033310", "권리락(유상증자)", "2021-09-10T00:00:00"),
                ("3", "000001", "다른공시", "2026-03-01T00:00:00"),
            ],
        )

    captured = capture_right_drop_calendar(database)
    assert captured["source_snapshot"]["table_rows"] == 3
    assert captured["events"] == [
        {
            "event_date": "2021-09-10",
            "receipt_datetime": "2021-09-10T00:00:00",
            "receipt_no": "1",
            "report_name": "권리락(유상증자)",
            "stock_code": "033310",
        },
        {
            "event_date": "2026-02-26",
            "receipt_datetime": "2026-02-26T00:00:00",
            "receipt_no": "2",
            "report_name": "권리락(무상증자)",
            "stock_code": "300120",
        },
    ]


def test_audit_rejects_a_pinned_calendar_missing_committed_events(tmp_path):
    calendar = json.loads(RIGHT_DROP_CALENDAR.read_text(encoding="utf-8"))
    calendar["events"] = [
        event
        for event in calendar["events"]
        if event["receipt_no"] != "20260226901417"
    ]
    incomplete = tmp_path / "incomplete_calendar.json"
    write_json(incomplete, calendar)

    with pytest.raises(ValueError, match="omits committed right-drop receipts"):
        build_audit(right_drop_calendar_path=incomplete)


def test_production_market_data_calls_choose_adjustment_explicitly():
    calls = []
    for root in (PROJECT_ROOT / "src", PROJECT_ROOT / "scripts"):
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = getattr(node.func, "attr", None) or getattr(
                    node.func, "id", None
                )
                if name not in {"fetch_ohlcv", "get_market_ohlcv_by_date"}:
                    continue
                keyword_names = {keyword.arg for keyword in node.keywords}
                calls.append((path.relative_to(PROJECT_ROOT), name, keyword_names))

    assert calls
    for path, name, keyword_names in calls:
        required = "adjustment" if name == "fetch_ohlcv" else "adjusted"
        assert required in keyword_names, f"{path}: {name} omits {required}"
