from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from typing import Iterable

from kdtb.data.dart_client import CORP_CLS_TO_MARKET
from kdtb.schemas.disclosure import Disclosure

logger = logging.getLogger(__name__)


def dart_record_to_disclosure(rec: dict) -> Disclosure:
    """Map a raw OPEN DART `list.json` record to a `Disclosure`."""
    rcept_dt = (rec.get("rcept_dt") or "").strip()
    if len(rcept_dt) == 8 and rcept_dt.isdigit():
        receipt_datetime = datetime.strptime(rcept_dt, "%Y%m%d")
    else:
        receipt_datetime = datetime.utcnow()
    return Disclosure(
        receipt_no=rec["rcept_no"],
        corp_code=rec["corp_code"],
        corp_name=(rec.get("corp_name") or "").strip(),
        stock_code=(rec.get("stock_code") or "").strip() or None,
        report_name=(rec.get("report_nm") or "").strip(),
        receipt_datetime=receipt_datetime,
        market=CORP_CLS_TO_MARKET.get((rec.get("corp_cls") or "").strip(), "OTHER"),
        source="DART",
        raw_payload=rec,
    )


class DisclosureStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def upsert(self, disclosure: Disclosure) -> bool:
        """Insert if new (dedupe on receipt_no). Returns True if a row was inserted."""
        cur = self.conn.execute(
            """
            INSERT OR IGNORE INTO disclosures (
                receipt_no, corp_code, corp_name, stock_code, report_name,
                receipt_datetime, market, source, raw_url, raw_payload_json, raw_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                disclosure.receipt_no,
                disclosure.corp_code,
                disclosure.corp_name,
                disclosure.stock_code,
                disclosure.report_name,
                disclosure.receipt_datetime.isoformat(),
                disclosure.market,
                disclosure.source,
                disclosure.raw_url,
                json.dumps(disclosure.raw_payload, ensure_ascii=False),
                disclosure.raw_text,
            ),
        )
        return cur.rowcount > 0

    def ingest_records(self, records: Iterable[dict]) -> tuple[int, int]:
        """Map DART records to Disclosures and upsert. Returns (new_count, total_count)."""
        new = 0
        total = 0
        for rec in records:
            disclosure = dart_record_to_disclosure(rec)
            if self.upsert(disclosure):
                new += 1
            total += 1
        self.conn.commit()
        return new, total

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM disclosures").fetchone()[0]
