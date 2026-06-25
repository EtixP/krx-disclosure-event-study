"""Event study filtered by deterministic-parser-extracted contract/revenue ratio.

This is the proper version of the event study: only events where the contract
value is at least N% of prior-year revenue (default 8%, per CLAUDE.md) are
included. Compares gross vs net-of-costs returns to the unfiltered baseline.

Reuses cached prices from data/event_study_results.csv if present (the
unfiltered study already fetched them all).

Usage:
    python scripts/run_filtered_event_study.py
    python scripts/run_filtered_event_study.py --min-ratio 0.15
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
from pathlib import Path

import pandas as pd

from kdtb.backtest.cost_model import CostModel
from kdtb.backtest.metrics import compute
from kdtb.logging_setup import setup_logging


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default="data/kdtb.db")
    p.add_argument("--unfiltered-csv", default="data/event_study_results.csv")
    p.add_argument("--out", default="data/event_study_filtered.csv")
    p.add_argument("--min-ratio", type=float, default=0.08)
    p.add_argument("--parser-model", default="deterministic_supply_contract_v1")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    setup_logging("INFO", False)
    log = logging.getLogger("filtered_event_study")

    if not Path(args.unfiltered_csv).exists():
        log.error("%s not found — run scripts/run_event_study.py first", args.unfiltered_csv)
        return 1

    unfiltered = pd.read_csv(args.unfiltered_csv)
    log.info("Loaded %d unfiltered events", len(unfiltered))

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    extractions = pd.read_sql_query(
        f"""
        SELECT e.disclosure_id, e.contract_value_krw, e.prior_year_revenue_krw,
               e.contract_to_revenue_ratio, e.validation_status
        FROM extractions e
        WHERE e.model_name = '{args.parser_model}'
        """,
        conn,
    )
    conn.close()
    log.info("Loaded %d extractions (model=%s)", len(extractions), args.parser_model)
    log.info("  ok=%d, review=%d, blocked=%d",
             (extractions["validation_status"] == "ok").sum(),
             (extractions["validation_status"] == "needs_manual_review").sum(),
             (extractions["validation_status"] == "blocked").sum())

    merged = unfiltered.merge(
        extractions, left_on="id", right_on="disclosure_id", how="left", suffixes=("", "_ext")
    )
    log.info("Merged: %d events have an extraction", merged["disclosure_id"].notna().sum())

    filtered = merged[
        (merged["validation_status"] == "ok")
        & (merged["contract_to_revenue_ratio"] >= args.min_ratio)
    ].copy()
    log.info("Filtered to ratio >= %.2f: %d events", args.min_ratio, len(filtered))

    filtered.to_csv(args.out, index=False)
    log.info("Wrote %s", args.out)

    model = CostModel()
    cost = model.roundtrip_cost(1.0)

    def _summarize(df: pd.DataFrame, label: str) -> None:
        print(f"\n=== {label}: n={len(df)} ===")
        if len(df) == 0:
            print("  (empty)")
            return
        print(f"{'horizon':>8} {'n':>4} {'gross_mean':>11} {'net_mean':>10} {'gross_win%':>10} {'net_win%':>9} {'profit_factor':>14} {'sharpe-ish':>11}")
        print("-" * 88)
        for col in ["ret_1d", "ret_2d", "ret_5d"]:
            s = df[col].dropna()
            if len(s) == 0:
                continue
            net = (s - cost).tolist()
            m = compute(net)
            print(f"{col:>8} {len(s):>4} {s.mean()*100:>+10.3f}% {m.mean_return*100:>+9.3f}% "
                  f"{(s>0).mean()*100:>9.1f}% {m.win_rate*100:>8.1f}% "
                  f"{(m.profit_factor or 0):>14.3f} {(m.sharpe_like or 0):>+11.4f}")

    _summarize(unfiltered, "ALL TITLE-FILTERED EVENTS (baseline)")
    _summarize(filtered, f"RATIO >= {args.min_ratio:.2f} (proper filter)")

    # Top winners and losers
    if len(filtered) > 0:
        winners = filtered.nlargest(5, "ret_5d")[["corp_name", "stock_code", "event_date", "contract_to_revenue_ratio", "ret_1d", "ret_5d"]]
        losers = filtered.nsmallest(5, "ret_5d")[["corp_name", "stock_code", "event_date", "contract_to_revenue_ratio", "ret_1d", "ret_5d"]]
        print("\n=== TOP 5 5-day WINNERS (filtered) ===")
        print(winners.to_string(index=False))
        print("\n=== BOTTOM 5 5-day LOSERS (filtered) ===")
        print(losers.to_string(index=False))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
