"""Event study for new supply-contract disclosures.

Walks all disclosures whose `report_name` starts with '단일판매' and isn't a
revision/cancellation, fetches OHLCV around the disclosure date for each
stock, and computes T+1/T+2/T+5 close-to-close returns measured from the
event-day close. Output: CSV of per-event rows + a printed summary.

This is a *naive* event study — it uses only the disclosure title for
filtering. The proper version (Milestone 3+) requires LLM-extracted contract
value vs prior-year revenue to filter to economically meaningful events.

Usage:
    python scripts/run_event_study.py
    python scripts/run_event_study.py --limit 50    # quick sample
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from kdtb.data.market_data_client import MarketDataClient
from kdtb.logging_setup import setup_logging

SUPPLY_CONTRACT_QUERY = """
SELECT id, receipt_no, corp_code, corp_name, stock_code, report_name,
       DATE(receipt_datetime) AS event_date, market
FROM disclosures
WHERE report_name LIKE '단일판매%'
  AND report_name NOT LIKE '%정정%'
  AND report_name NOT LIKE '%해지%'
  AND stock_code IS NOT NULL
  AND stock_code != ''
  AND market IN ('KOSPI', 'KOSDAQ')
ORDER BY event_date, receipt_no
"""


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default="data/kdtb.db")
    p.add_argument("--out", default="data/event_study_results.csv")
    p.add_argument("--limit", type=int, default=None, help="limit number of unique stocks (testing)")
    p.add_argument("--sleep", type=float, default=0.15, help="seconds between pykrx fetches")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    setup_logging("INFO", False)
    log = logging.getLogger("event_study")

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    events = [dict(r) for r in conn.execute(SUPPLY_CONTRACT_QUERY).fetchall()]
    log.info("Found %d candidate supply-contract events", len(events))

    by_stock: dict[str, list[dict]] = {}
    for ev in events:
        by_stock.setdefault(ev["stock_code"], []).append(ev)
    log.info("Across %d unique stocks", len(by_stock))

    stocks = list(by_stock.items())
    if args.limit:
        stocks = stocks[: args.limit]
        log.info("Limited to first %d stocks for testing", len(stocks))

    client = MarketDataClient()
    rows: list[dict] = []
    failed = 0

    for i, (stock_code, stock_events) in enumerate(stocks, 1):
        event_dates = sorted(
            {datetime.strptime(e["event_date"], "%Y-%m-%d").date() for e in stock_events}
        )
        start = event_dates[0] - timedelta(days=3)
        end = event_dates[-1] + timedelta(days=12)
        try:
            prices = client.fetch_ohlcv(stock_code, start, end)
        except Exception as e:
            log.warning("Fetch failed for %s: %s", stock_code, e)
            failed += 1
            for ev in stock_events:
                rows.append({**ev, "error": f"fetch_failed:{type(e).__name__}"})
            time.sleep(args.sleep)
            continue

        if prices.empty:
            for ev in stock_events:
                rows.append({**ev, "error": "no_price_data"})
        else:
            for ev in stock_events:
                event_date = datetime.strptime(ev["event_date"], "%Y-%m-%d").date()
                returns = MarketDataClient.event_returns(prices, event_date)
                rows.append({**ev, **returns, "error": None})

        time.sleep(args.sleep)
        if i % 25 == 0:
            log.info("  progress: %d/%d stocks (%d events, %d fetch failures)", i, len(stocks), len(rows), failed)
        # Checkpoint every 100 stocks so we don't lose work if the process hangs.
        if i % 100 == 0:
            pd.DataFrame(rows).to_csv(args.out, index=False)
            log.info("  checkpoint saved -> %s", args.out)

    df = pd.DataFrame(rows)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(args.out, index=False)
    log.info("Wrote %s (%d rows)", args.out, len(df))

    print()
    print("=" * 64)
    print(f"EVENT STUDY SUMMARY  |  {len(df)} events, {len(by_stock)} unique stocks")
    print("=" * 64)
    valid = df.dropna(subset=["ret_1d"]) if "ret_1d" in df.columns else pd.DataFrame()
    print(f"events with at least 1-day return: {len(valid)}")
    print(f"events with NO price data:         {(df.get('error') == 'no_price_data').sum() if 'error' in df.columns else 0}")
    print(f"events with fetch failures:        {df.get('error', pd.Series(dtype=object)).fillna('').str.startswith('fetch_failed').sum() if 'error' in df.columns else 0}")
    print()
    print(f"{'horizon':>8} {'n':>5} {'mean':>8} {'median':>8} {'std':>8} {'win%':>6} {'p25':>8} {'p75':>8}")
    print("-" * 64)
    for col in ["ret_1d", "ret_2d", "ret_5d"]:
        if col not in df.columns:
            continue
        s = df[col].dropna()
        if len(s) == 0:
            continue
        print(
            f"{col:>8} {len(s):>5} "
            f"{s.mean()*100:>+7.3f}% {s.median()*100:>+7.3f}% "
            f"{s.std()*100:>7.3f}% {(s>0).mean()*100:>5.1f}% "
            f"{s.quantile(0.25)*100:>+7.3f}% {s.quantile(0.75)*100:>+7.3f}%"
        )
    print("=" * 64)
    print()

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
