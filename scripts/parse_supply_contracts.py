"""Fetch DART document XML for every supply-contract disclosure, run the
deterministic parser, and store the resulting extraction in SQLite.

Usage:
    python scripts/parse_supply_contracts.py              # all 533 events
    python scripts/parse_supply_contracts.py --limit 20   # test on 20
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import time
from pathlib import Path

from dotenv import load_dotenv

from kdtb.config import load_settings
from kdtb.data.dart_client import DartClient
from kdtb.interpretation.deterministic_parser import parse_supply_contract, PARSER_VERSION
from kdtb.logging_setup import setup_logging
from kdtb.storage.db import init_db
from kdtb.storage.extraction_store import ExtractionStore


CANDIDATE_QUERY = """
SELECT id, receipt_no, corp_name, stock_code, report_name
FROM disclosures
WHERE report_name LIKE '단일판매%'
  AND report_name NOT LIKE '%정정%'
  AND report_name NOT LIKE '%해지%'
  AND stock_code IS NOT NULL
  AND stock_code != ''
  AND market IN ('KOSPI', 'KOSDAQ')
ORDER BY receipt_no DESC
"""


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--sleep", type=float, default=0.15)
    p.add_argument("--skip-existing", action="store_true", help="Skip rows already parsed by this version")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    load_dotenv(".env")
    settings = load_settings([Path("config/default.yaml")])
    setup_logging(settings.logging.level, settings.logging.json_format)
    log = logging.getLogger("parse_supply_contracts")

    api_key = os.getenv("DART_API_KEY")
    if not api_key:
        log.error("DART_API_KEY not set")
        return 1

    conn = init_db(settings.storage.sqlite_path)
    conn.row_factory = sqlite3.Row
    candidates = list(conn.execute(CANDIDATE_QUERY).fetchall())
    if args.limit:
        candidates = candidates[: args.limit]
    log.info("Found %d candidates to parse", len(candidates))

    store = ExtractionStore(conn)
    existing_ids: set[int] = set()
    if args.skip_existing:
        rows = conn.execute(
            "SELECT disclosure_id FROM extractions WHERE model_name = ?", (PARSER_VERSION,)
        ).fetchall()
        existing_ids = {r[0] for r in rows}
        log.info("%d already parsed — will skip", len(existing_ids))

    stats = {"ok": 0, "needs_manual_review": 0, "blocked": 0, "fetch_failed": 0, "skipped": 0}

    with DartClient(api_key) as client:
        for i, row in enumerate(candidates, 1):
            if row["id"] in existing_ids:
                stats["skipped"] += 1
                continue
            try:
                text = client.fetch_document_text(row["receipt_no"])
            except Exception as e:
                log.warning("Fetch failed %s: %s", row["receipt_no"], e)
                stats["fetch_failed"] += 1
                time.sleep(args.sleep)
                continue
            ext = parse_supply_contract(text, disclosure_id=row["id"], report_name=row["report_name"])
            store.upsert(ext)
            stats[ext.validation_status] += 1
            time.sleep(args.sleep)
            if i % 25 == 0:
                conn.commit()
                log.info(
                    "  %d/%d  ok=%d review=%d blocked=%d fetch_fail=%d",
                    i, len(candidates),
                    stats["ok"], stats["needs_manual_review"], stats["blocked"], stats["fetch_failed"],
                )
    conn.commit()

    log.info("=" * 60)
    log.info("DONE  ok=%d  review=%d  blocked=%d  fetch_failed=%d  skipped=%d",
             stats["ok"], stats["needs_manual_review"], stats["blocked"],
             stats["fetch_failed"], stats["skipped"])
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
