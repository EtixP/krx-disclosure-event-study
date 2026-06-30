"""Backfill exact disclosure filing times into the DB by scraping DART.

The OPEN DART OpenAPI gives only the filing date. The DART website daily list
exposes the HH:MM time per receipt number (for historical dates too). This
script scrapes the daily list for every date that has events of interest,
stores filing_time on the disclosures rows, and records the date in
scraped_dates so the run is RESUMABLE — re-running fills only the gaps, which
matters because sustained scraping can hit transient network failures.

Usage:
    python scripts/backfill_disclosure_times.py --category buyback
    python scripts/backfill_disclosure_times.py --category buyback --since 2023-07-01
    python scripts/backfill_disclosure_times.py --all-categories --since 2024-01-01
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import time
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv

from kdtb.config import load_settings
from kdtb.data.disclosure_time_scraper import DisclosureTimeScraper
from kdtb.data.event_categories import CATEGORIES
from kdtb.logging_setup import setup_logging
from kdtb.storage.db import init_db


def target_dates(conn: sqlite3.Connection, where: str, since: str | None, until: str | None) -> list[str]:
    clauses = [where, "stock_code IS NOT NULL", "stock_code != ''", "market IN ('KOSPI','KOSDAQ')"]
    if since:
        clauses.append(f"DATE(receipt_datetime) >= '{since}'")
    if until:
        clauses.append(f"DATE(receipt_datetime) <= '{until}'")
    sql = f"SELECT DISTINCT DATE(receipt_datetime) FROM disclosures WHERE {' AND '.join(clauses)} ORDER BY 1"
    return [r[0] for r in conn.execute(sql).fetchall()]


def already_scraped(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT ymd FROM scraped_dates").fetchall()}


def scrape_one(scraper: DisclosureTimeScraper, d: date, retries: int, backoff: float, log) -> dict[str, str]:
    for attempt in range(retries):
        try:
            return scraper.scrape_date(d)
        except Exception as e:  # httpx errors + DNS OSErrors
            wait = backoff * (2 ** attempt)
            log.warning("scrape %s attempt %d/%d failed (%s); retrying in %.1fs",
                        d, attempt + 1, retries, type(e).__name__, wait)
            time.sleep(wait)
    raise RuntimeError(f"scrape failed for {d} after {retries} retries")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--category", default="buyback", choices=sorted(CATEGORIES.keys()))
    p.add_argument("--all-categories", action="store_true", help="every event date, not just one category")
    p.add_argument("--since", default=None, help="YYYY-MM-DD lower bound")
    p.add_argument("--until", default=None, help="YYYY-MM-DD upper bound")
    p.add_argument("--sleep", type=float, default=0.3, help="seconds between page requests")
    p.add_argument("--date-sleep", type=float, default=0.4, help="seconds between dates")
    p.add_argument("--retries", type=int, default=4)
    p.add_argument("--max-pages", type=int, default=10)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    load_dotenv(".env")
    settings = load_settings([Path("config/default.yaml")])
    setup_logging(settings.logging.level, settings.logging.json_format)
    log = logging.getLogger("backfill_times")

    conn = init_db(settings.storage.sqlite_path)

    where = "1=1" if args.all_categories else CATEGORIES[args.category]
    dates = target_dates(conn, where, args.since, args.until)
    done = already_scraped(conn)
    todo = [d for d in dates if d.replace("-", "") not in done]
    log.info("target dates=%d  already scraped=%d  to do=%d",
             len(dates), len(dates) - len(todo), len(todo))

    scraper = DisclosureTimeScraper(sleep=args.sleep, max_pages=args.max_pages)
    filled = 0
    failed_dates = 0
    for i, dstr in enumerate(todo, 1):
        d = datetime.strptime(dstr, "%Y-%m-%d").date()
        ymd = dstr.replace("-", "")
        try:
            times = scrape_one(scraper, d, args.retries, 1.5, log)
        except Exception as e:
            log.error("giving up on %s: %s", dstr, e)
            failed_dates += 1
            continue
        if times:
            conn.executemany(
                "UPDATE disclosures SET filing_time = ? WHERE receipt_no = ?",
                [(t, rn) for rn, t in times.items()],
            )
        conn.execute("INSERT OR REPLACE INTO scraped_dates (ymd, n_times) VALUES (?, ?)", (ymd, len(times)))
        conn.commit()
        filled += 1
        if i % 25 == 0:
            matched = conn.execute(
                "SELECT COUNT(*) FROM disclosures WHERE filing_time IS NOT NULL"
            ).fetchone()[0]
            log.info("  %d/%d dates  (%d failed)  | disclosures with filing_time so far: %d",
                     i, len(todo), failed_dates, matched)
        time.sleep(args.date_sleep)

    scraper.close()
    total_times = conn.execute("SELECT COUNT(*) FROM disclosures WHERE filing_time IS NOT NULL").fetchone()[0]
    log.info("DONE  scraped_dates_this_run=%d  failed=%d  total disclosures with filing_time=%d",
             filled, failed_dates, total_times)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
