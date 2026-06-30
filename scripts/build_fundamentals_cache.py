"""Fetch + cache company fundamentals from DART for the ranker universe.

Resumable: skips companies already cached for the target fiscal year. The
universe is every KOSPI/KOSDAQ company that appears in our disclosures table,
ordered by disclosure activity so --limit takes the most-active names first.

Usage:
    python scripts/build_fundamentals_cache.py                 # full universe
    python scripts/build_fundamentals_cache.py --limit 400     # top-400 by activity
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
from kdtb.logging_setup import setup_logging
from kdtb.ranker.fundamentals import DartFundamentals
from kdtb.storage.db import init_db

UNIVERSE_SQL = """
SELECT corp_code, stock_code,
       MAX(corp_name) AS corp_name, MAX(market) AS market, COUNT(*) AS n
FROM disclosures
WHERE stock_code IS NOT NULL AND stock_code != '' AND market IN ('KOSPI','KOSDAQ')
GROUP BY corp_code, stock_code
ORDER BY n DESC
"""


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--year", default="2025")
    p.add_argument("--fallback-year", default="2024")
    p.add_argument("--sleep", type=float, default=0.1)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    load_dotenv(".env")
    settings = load_settings([Path("config/default.yaml")])
    setup_logging(settings.logging.level, settings.logging.json_format)
    log = logging.getLogger("build_fundamentals")

    api_key = os.getenv("DART_API_KEY")
    if not api_key:
        log.error("DART_API_KEY not set")
        return 1

    conn = init_db(settings.storage.sqlite_path)
    conn.row_factory = sqlite3.Row
    universe = [dict(r) for r in conn.execute(UNIVERSE_SQL).fetchall()]
    if args.limit:
        universe = universe[: args.limit]
    cached = {r[0] for r in conn.execute(
        "SELECT corp_code FROM fundamentals WHERE fiscal_year IN (?, ?)",
        (args.year, args.fallback_year),
    ).fetchall()}
    todo = [u for u in universe if u["corp_code"] not in cached]
    log.info("universe=%d  cached=%d  to fetch=%d", len(universe), len(universe) - len(todo), len(todo))

    fetcher = DartFundamentals(api_key)
    ok = miss = 0
    for i, u in enumerate(todo, 1):
        data = fetcher.fetch(u["corp_code"], years=(args.year, args.fallback_year))
        if data:
            conn.execute(
                """INSERT OR REPLACE INTO fundamentals
                   (corp_code, stock_code, fiscal_year, equity, net_income, revenue, debt, shares, fs_div)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (u["corp_code"], u["stock_code"], data["fiscal_year"], data["equity"],
                 data["net_income"], data["revenue"], data["debt"], data["shares"], data["fs_div"]),
            )
            ok += 1
        else:
            miss += 1
        if i % 50 == 0:
            conn.commit()
            log.info("  %d/%d  cached=%d  no-data=%d", i, len(todo), ok, miss)
        time.sleep(args.sleep)
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM fundamentals").fetchone()[0]
    log.info("DONE  fetched_ok=%d  no_data=%d  total_cached=%d", ok, miss, total)
    fetcher.close()
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
