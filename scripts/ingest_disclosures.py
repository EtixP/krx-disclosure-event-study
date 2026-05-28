"""Ingest OPEN DART disclosures for a single date into SQLite.

Usage:
    python scripts/ingest_disclosures.py --date 2026-05-28
    python scripts/ingest_disclosures.py --date 2026-05-28 --corp-cls Y  # KOSPI only
"""
from __future__ import annotations

import argparse
import logging
import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from kdtb.config import load_settings
from kdtb.data.dart_client import DartClient
from kdtb.data.disclosure_store import DisclosureStore
from kdtb.logging_setup import setup_logging
from kdtb.storage.db import init_db


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--date", required=True, help="YYYY-MM-DD")
    p.add_argument(
        "--corp-cls",
        choices=["Y", "K", "N", "E"],
        default=None,
        help="Y=KOSPI, K=KOSDAQ, N=KONEX, E=other. Default: all markets.",
    )
    p.add_argument("--config", default="config/default.yaml")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    load_dotenv(".env")
    settings = load_settings([Path(args.config)])
    setup_logging(settings.logging.level, settings.logging.json_format)
    log = logging.getLogger("ingest_disclosures")

    api_key = os.getenv("DART_API_KEY")
    if not api_key:
        log.error("DART_API_KEY not set. Add it to .env (see .env.example).")
        return 1

    target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
    conn = init_db(settings.storage.sqlite_path)
    store = DisclosureStore(conn)
    before = store.count()

    log.info("fetching disclosures for %s corp_cls=%s", target_date, args.corp_cls or "ALL")
    with DartClient(api_key) as client:
        records = list(client.list_disclosures(target_date, corp_cls=args.corp_cls))

    new, total = store.ingest_records(records)
    after = store.count()
    log.info("fetched=%d new=%d duplicates=%d db_total=%d (was %d)", total, new, total - new, after, before)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
