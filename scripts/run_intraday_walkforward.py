"""Time-aware walk-forward: does entering at the same-day close for
intraday-published events hold an edge ACROSS folds (not just recently)?

Joins scraped filing times onto an event category, assigns each event its
earliest realistically-tradable entry:
  - published intraday (filing_time < 15:20 KST) -> enter at the SAME-DAY close
  - published after close                        -> enter at the T+1 close
exits at the T+5 close, nets the date/market-aware roundtrip cost, then walks forward
across half-year folds. Reports, per fold:
  - always-trade with the OLD uniform T+1 entry
  - always-trade with the TIME-AWARE entry
and whether a learned policy adds selection lift on the time-aware returns.

This is the validation gate the intraday lead must pass: the prior buyback
"edge" died because it was recent-window-only. If the time-aware edge is real
it should be positive across most folds, including the older ones.

Usage:
    python scripts/run_intraday_walkforward.py --category buyback
"""
from __future__ import annotations

import argparse
import sqlite3
import sys

import numpy as np
import pandas as pd

from kdtb.backtest.cost_model import TRADABILITY_BAR_PCT, CostModel
from kdtb.data.benchmarks import require_benchmark_columns
from kdtb.learning.features import FEATURE_NAMES, extract_features
from kdtb.learning.walk_forward_trainer import make_folds, run_walk_forward

INTRADAY_CUTOFF_MIN = 15 * 60 + 20  # 15:20 KST — need a few minutes to reach the close


def _mins(t: str | None):
    if not t or ":" not in str(t):
        return None
    h, m = str(t).split(":")[:2]
    try:
        return int(h) * 60 + int(m)
    except ValueError:
        return None


def classify_entry(filing_mins, t0_close: float, t1_close: float, cutoff: int = INTRADAY_CUTOFF_MIN):
    """Return (entry_price, mode) for the earliest REALISTICALLY tradable entry.

    Integrity rule (no look-ahead): an after-close (or unknown-time) event must
    NOT use the same-day close — that price was set before the announcement.
    Only an event published intraday (before `cutoff`) may use the same-day
    close, because the announcement preceded the close.
    """
    if filing_mins is not None and filing_mins < cutoff:
        return t0_close, "intraday"
    return t1_close, "afterclose"


def apply_timeaware_returns(
    df: pd.DataFrame,
    cost_model: CostModel | None = None,
    *,
    return_basis: str = "abnormal",
) -> pd.DataFrame:
    """Apply dated costs and raw/abnormal attribution to both entry rules."""

    required = ["t0_date", "t+1_date", "t+5_date", "market"]
    missing = [column for column in required if column not in df.columns]
    if missing:
        raise ValueError(
            "dated transaction costs require event-study columns: "
            + ", ".join(missing)
        )
    if df[required].isna().any().any():
        raise ValueError("dated transaction-cost inputs contain missing values")

    out = df.copy()
    if return_basis not in {"raw", "abnormal"}:
        raise ValueError(f"unsupported intraday return basis: {return_basis}")
    if return_basis == "abnormal":
        require_benchmark_columns(out, tokens=("t0", "t1", "t5"))
    model = cost_model or CostModel()
    intraday = out["filing_mins"].notna() & (
        out["filing_mins"] < INTRADAY_CUTOFF_MIN
    )
    entry = np.where(intraday, out["t0_close"], out["t+1_close"])
    entry_dates = np.where(intraday, out["t0_date"], out["t+1_date"])
    uniform_costs = model.roundtrip_cost_fractions(
        buy_dates=out["t+1_date"],
        sell_dates=out["t+5_date"],
        markets=out["market"],
    )
    timeaware_costs = model.roundtrip_cost_fractions(
        buy_dates=entry_dates,
        sell_dates=out["t+5_date"],
        markets=out["market"],
    )
    out["ret_uniform_raw"] = (
        (out["t+5_close"] - out["t+1_close"]) / out["t+1_close"]
        - uniform_costs
    )
    out["entry_mode"] = np.where(intraday, "intraday", "afterclose")
    out["ret_timeaware_raw"] = (
        (out["t+5_close"] - entry) / entry - timeaware_costs
    )
    if return_basis == "abnormal":
        benchmark_entry = np.where(
            intraday, out["benchmark_t0_close"], out["benchmark_t1_close"]
        )
        benchmark_uniform = (
            (out["benchmark_t5_close"] - out["benchmark_t1_close"])
            / out["benchmark_t1_close"]
        )
        benchmark_timeaware = (
            (out["benchmark_t5_close"] - benchmark_entry) / benchmark_entry
        )
        out["ret_uniform_abnormal"] = out["ret_uniform_raw"] - benchmark_uniform
        out["ret_timeaware_abnormal"] = (
            out["ret_timeaware_raw"] - benchmark_timeaware
        )
        out["ret_uniform"] = out["ret_uniform_abnormal"]
        out["ret_timeaware"] = out["ret_timeaware_abnormal"]
    else:
        out["ret_uniform_abnormal"] = float("nan")
        out["ret_timeaware_abnormal"] = float("nan")
        out["ret_uniform"] = out["ret_uniform_raw"]
        out["ret_timeaware"] = out["ret_timeaware_raw"]
    out["return_basis"] = return_basis
    return out


def load_timeaware(category: str, db_path: str) -> pd.DataFrame:
    csv = "data/event_study_supply_contract.csv" if category == "supply_contract" else f"data/event_study_{category}.csv"
    df = pd.read_csv(csv)
    df = df.dropna(subset=["t0_close", "t+1_close", "t+5_close"]).copy()
    df = df[df["t+1_close"] > 0]
    df["receipt_no"] = df["receipt_no"].astype(str)

    conn = sqlite3.connect(db_path)
    times = dict(conn.execute(
        "SELECT receipt_no, filing_time FROM disclosures WHERE filing_time IS NOT NULL"
    ).fetchall())
    # join extraction features for supply_contract enrichment
    if category == "supply_contract":
        ex = pd.read_sql_query(
            "SELECT disclosure_id, contract_to_revenue_ratio, contract_value_krw, counterparty_type "
            "FROM extractions WHERE model_name='deterministic_supply_contract_v1' AND validation_status='ok'",
            conn,
        )
        df = df.merge(ex, left_on="id", right_on="disclosure_id", how="left")
    conn.close()

    df["filing_time"] = df["receipt_no"].map(times)
    df["filing_mins"] = df["filing_time"].map(_mins)
    df = apply_timeaware_returns(df)
    df["event_date"] = pd.to_datetime(df["event_date"])
    return df


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--category", default="buyback")
    p.add_argument("--db", default="data/kdtb.db")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    df = load_timeaware(args.category, args.db)
    have_time = df["filing_mins"].notna()
    matched = df[have_time].copy()
    print(f"\n=== Time-aware walk-forward — {args.category} (benchmark-adjusted) ===")
    print(f"events with prices: {len(df)}  |  with filing_time: {have_time.sum()} "
          f"({have_time.mean()*100:.0f}% coverage)")
    if have_time.sum() < 100:
        print("Too few matched filing times yet — run scripts/backfill_disclosure_times.py first.")
        return 1
    intr = (matched["entry_mode"] == "intraday").mean()
    print(f"intraday-published: {intr*100:.0f}%   after-close: {(1-intr)*100:.0f}%")

    folds = make_folds(matched.rename(columns={"ret_timeaware": "realized_net_return"}))

    print(f"\n{'fold':>9} {'n':>5} {'always_uniform':>15} {'always_timeaware':>17} {'intraday%':>10} {'delta':>8}")
    print("-" * 72)
    pos_uniform = pos_timeaware = 0
    cu = ct = 0.0
    for period, sub in folds:
        if len(sub) < 5:
            continue
        au = sub["ret_uniform"].mean() if "ret_uniform" in sub else np.nan
        at = sub["realized_net_return"].mean()
        intr_f = (sub["entry_mode"] == "intraday").mean()
        cu += sub["ret_uniform"].sum(); ct += sub["realized_net_return"].sum()
        pos_uniform += int(au > 0); pos_timeaware += int(at > 0)
        print(f"{period:>9} {len(sub):>5} {au*100:>+14.3f}% {at*100:>+16.3f}% {intr_f*100:>9.0f}% {(at-au)*100:>+7.3f}%")

    n_folds = sum(1 for _, s in folds if len(s) >= 5)
    print("-" * 72)
    um = cu / len(matched) * 100
    tm = ct / len(matched) * 100
    print(f"{'MEAN/trade':>9} {len(matched):>5} {um:>+14.3f}% {tm:>+16.3f}% {intr*100:>9.0f}% {(tm-um):>+7.3f}%")
    print(f"\npositive folds: uniform {pos_uniform}/{n_folds}  |  time-aware {pos_timeaware}/{n_folds}")

    # Learned-policy selection lift on TIME-AWARE returns (does a selector add value?)
    feat_df = pd.DataFrame([extract_features(r) for r in matched.to_dict("records")],
                           columns=FEATURE_NAMES, index=matched.index)
    learn_df = pd.concat([feat_df, matched[["event_date"]].copy()], axis=1)
    learn_df["realized_net_return"] = matched["ret_timeaware"].values
    learn_df["label"] = (learn_df["realized_net_return"] > 0).astype(int)
    report = run_walk_forward(learn_df.sort_values("event_date").reset_index(drop=True), random_state=0)
    model_trades = report.total_model_trades
    if model_trades:
        model_avg = report.cumulative_model_pnl / model_trades * 100
        matched_always_pnl = sum(f.always_pnl for f in report.folds if f.model_trades > 0)
        matched_always_n = sum(f.test_n for f in report.folds if f.model_trades > 0)
        matched_always_avg = matched_always_pnl / matched_always_n * 100 if matched_always_n else 0.0
        lift = model_avg - matched_always_avg
    else:
        model_avg = matched_always_avg = lift = 0.0

    print()
    print("=== Verdict ===")
    print(f"  Always-trade, time-aware entry : {tm:+.3f}%/trade, {pos_timeaware}/{n_folds} folds positive")
    print(f"  Always-trade, old uniform T+1  : {um:+.3f}%/trade, {pos_uniform}/{n_folds} folds positive")
    print(f"  Learned selector on time-aware : {model_avg:+.3f}%/trade vs matched always {matched_always_avg:+.3f}%  "
          f"=> selection lift {lift:+.3f}%")
    print()
    edge = tm
    if pos_timeaware >= 0.7 * n_folds and edge > TRADABILITY_BAR_PCT:
        print(f"  VERDICT: TIME-AWARE EDGE HOLDS across folds and clears the "
              f"+{TRADABILITY_BAR_PCT:.2f}%/trade bar.")
        print("  This is the first effect to survive walk-forward AND adversarial review. It is\n  characterized, not deployed: capacity and closing-auction fill realism (see\n  INTRADAY_FEASIBILITY.md) put the realizable level below the bar.")
    elif pos_timeaware >= 0.6 * n_folds and edge > 0:
        print("  VERDICT: PROMISING but sub-threshold/borderline. Time-aware entry helps, but the edge")
        print("  is thin after costs — verify with realistic slippage + live forward data before trusting.")
    else:
        print("  VERDICT: DOES NOT HOLD across folds. The recent-window POC was regime-dependent, like")
        print("  the earlier buyback leads. Time-aware entry alone is not a durable edge.")
    print("\n  (Reminder: same-day-close fill assumes you can transact at the close after an intraday")
    print("   disclosure; real slippage and the single-position capacity limit still apply.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
