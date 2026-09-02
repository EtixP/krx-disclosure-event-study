"""Event study filtered by deterministic-parser-extracted contract/revenue ratio.

This is the proper version of the event study: only events where the contract
value is at least N% of prior-year revenue (default 8%, per DESIGN.md) are
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
from kdtb.data.benchmarks import require_benchmark_columns
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
    horizon_dates = {
        "ret_1d": "t+1_date",
        "ret_2d": "t+2_date",
        "ret_5d": "t+5_date",
    }

    def _summarize(df: pd.DataFrame, label: str) -> None:
        print(f"\n=== {label}: n={len(df)} ===")
        if len(df) == 0:
            print("  (empty)")
            return
        print(f"{'horizon':>8} {'n':>4} {'raw_gross':>10} {'raw_net':>10} {'abn_gross':>10} {'abn_net':>10} {'abn_win%':>9} {'abn_PF':>8}")
        print("-" * 86)
        for col in ["ret_1d", "ret_2d", "ret_5d"]:
            sub = df.dropna(subset=[col]).copy()
            if len(sub) == 0:
                continue
            dated = ["t0_date", horizon_dates[col], "market"]
            if sub[dated].isna().any().any():
                raise ValueError("dated transaction-cost inputs contain missing values")
            require_benchmark_columns(
                sub,
                tokens=("t0", horizon_dates[col].removesuffix("_date").replace("+", "")),
            )
            abnormal_col = f"abnormal_{col}"
            if abnormal_col not in sub or sub[abnormal_col].isna().any():
                raise ValueError(f"missing benchmark-adjusted returns for {col}")
            raw = sub[col]
            abnormal = sub[abnormal_col]
            costs = model.roundtrip_cost_fractions(
                buy_dates=sub["t0_date"],
                sell_dates=sub[horizon_dates[col]],
                markets=sub["market"],
            )
            raw_net = compute((raw - costs).tolist())
            abnormal_net = compute((abnormal - costs).tolist())
            print(
                f"{col:>8} {len(raw):>4} {raw.mean()*100:>+9.3f}% "
                f"{raw_net.mean_return*100:>+9.3f}% {abnormal.mean()*100:>+9.3f}% "
                f"{abnormal_net.mean_return*100:>+9.3f}% "
                f"{abnormal_net.win_rate*100:>8.1f}% "
                f"{(abnormal_net.profit_factor or 0):>8.3f}"
            )

    _summarize(unfiltered, "ALL TITLE-FILTERED EVENTS (baseline)")
    _summarize(filtered, f"RATIO >= {args.min_ratio:.2f} (proper filter)")

    # Top winners and losers
    if len(filtered) > 0:
        detail_columns = ["corp_name", "stock_code", "event_date", "contract_to_revenue_ratio", "ret_5d", "abnormal_ret_5d"]
        winners = filtered.nlargest(5, "abnormal_ret_5d")[detail_columns]
        losers = filtered.nsmallest(5, "abnormal_ret_5d")[detail_columns]
        print("\n=== TOP 5 5-day ABNORMAL-RETURN WINNERS (filtered) ===")
        print(winners.to_string(index=False))
        print("\n=== BOTTOM 5 5-day ABNORMAL-RETURN LOSERS (filtered) ===")
        print(losers.to_string(index=False))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
