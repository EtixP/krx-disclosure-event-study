from __future__ import annotations

import json
import sqlite3

from kdtb.events.audit import audit_normalization_prevalence
from kdtb.storage.db import SCHEMA


def test_normalization_audit_counts_updates_without_calling_them_duplicates():
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    rows = [
        ("20250101000001", "00000001", "원본", "2025-01-01T00:00:00", ""),
        ("20250102000001", "00000001", "[기재정정]원본", "2025-01-02T00:00:00", ""),
        ("20250102000002", "00000001", "[첨부추가]원본", "2025-01-02T00:00:00", ""),
        ("20250103000001", "00000001", "철회신고서", "2025-01-03T00:00:00", "철"),
    ]
    conn.executemany(
        """
        INSERT INTO disclosures (
            receipt_no, corp_code, corp_name, report_name,
            receipt_datetime, raw_payload_json
        ) VALUES (?, ?, '회사', ?, ?, ?)
        """,
        [
            (receipt_no, corp_code, title, timestamp, json.dumps({"rm": rm}))
            for receipt_no, corp_code, title, timestamp, rm in rows
        ],
    )

    report = audit_normalization_prevalence(conn)

    assert report["corpus"]["disclosures"] == 4
    assert report["update_prevalence"]["filings"] == 2
    assert report["update_actions"] == {"amendment": 1, "correction": 1}
    assert (
        report["title_based_linkage_diagnostic_only"][
            "updates_with_prior_same_issuer_exact_normalized_title"
        ]
        == 2
    )
    assert report["status_markers"]["title_contains_cancellation_marker"] == 1
    assert report["status_markers"]["dart_rm_contains_withdrawal_flag"] == 1
