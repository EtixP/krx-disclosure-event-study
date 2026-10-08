"""Train the offline learning paper-trader and print its learning curve.

Runs the champion/challenger walk-forward trainer on one event category's
historical mock trades and reports, per half-year test fold:
  - how much training history the model had
  - the model-policy's out-of-sample PnL
  - the always-trade and never-trade baselines
  - whether a new model was promoted that fold

Also prints a cumulative "equity curve" (sum of per-fold OOS PnL) and a final
verdict comparing the learned policy to the baselines.

Usage:
    python scripts/train_learner.py --category supply_contract
    python scripts/train_learner.py --category buyback
    python scripts/train_learner.py --category buyback --synthetic-edge   # sanity demo
"""
from __future__ import annotations

import argparse
import sys

import numpy as np
import pandas as pd

from kdtb.backtest.cost_model import TRADABILITY_BAR_PCT
from kdtb.learning.dataset import load_mock_trades
from kdtb.learning.features import FEATURE_NAMES
from kdtb.learning.walk_forward_trainer import make_folds, run_walk_forward


def _synthetic_edge_df(n_per_half: int = 150, n_halves: int = 8, seed: int = 0) -> pd.DataFrame:
    """A planted-edge dataset to demonstrate the machine CAN learn when edge exists."""
    rng = np.random.RandomState(seed)
    rows = []
    for h in range(n_halves):
        year = 2022 + h // 2
        month = 3 if h % 2 == 0 else 9
        for _ in range(n_per_half):
            feats = rng.rand(len(FEATURE_NAMES))
            feats[0] = 1.0 if rng.rand() > 0.5 else 0.0
            base = 0.02 if feats[0] > 0.5 else -0.02
            ret = base + rng.randn() * 0.004
            rows.append(list(feats) + [f"{year}-{month:02d}-15", ret, int(ret > 0)])
    cols = FEATURE_NAMES + ["event_date", "realized_net_return", "label"]
    df = pd.DataFrame(rows, columns=cols)
    df["event_date"] = pd.to_datetime(df["event_date"])
    return df


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--category", default="supply_contract")
    p.add_argument("--synthetic-edge", action="store_true",
                   help="ignore --category; run on planted-edge synthetic data (sanity demo)")
    p.add_argument("--random-state", type=int, default=0)
    p.add_argument(
        "--return-basis",
        choices=("raw", "abnormal"),
        default="abnormal",
        help="reward attribution for historical mock trades",
    )
    p.add_argument("--out", default=None, help="optional CSV path for the per-fold report")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    if args.synthetic_edge:
        df = _synthetic_edge_df(seed=args.random_state)
        label = "SYNTHETIC (planted edge)"
    else:
        try:
            df = load_mock_trades(args.category, return_basis=args.return_basis)
        except FileNotFoundError as e:
            print(f"error: {e}\nRun scripts/run_event_study.py --category {args.category} first.")
            return 1
        label = args.category

    n_folds = len(make_folds(df))
    print(f"\n=== Learning paper-trader — {label} ===")
    print(f"mock trades: {len(df)}  |  half-year folds: {n_folds}  |  features: {len(FEATURE_NAMES)}")
    basis = "broad-market abnormal" if not args.synthetic_edge and args.return_basis == "abnormal" else "raw"
    print(f"(mock trade = enter T+1 close, exit T+5 close, {basis} return minus dated costs)\n")

    if n_folds < 3:
        print("Not enough history (need >= 3 half-year folds). Ingest/backfill more data.")
        return 1

    report = run_walk_forward(df, random_state=args.random_state)

    # Per-trade averages are the honest unit. The "sum of net returns" (R) is a
    # relative policy-comparison reward, NOT compounded capital growth — printing
    # it as a big "%" would massively oversell a thin edge.
    print(f"{'fold':>10} {'train_n':>8} {'test_n':>7} {'m_trades':>8} {'m_avg/trd':>10} "
          f"{'m_sumR':>8} {'always_avg':>11} {'promoted':>9} {'champ_v':>8}")
    print("-" * 96)
    folds_traded = 0
    traded_periods: list[str] = []
    for f in report.folds:
        if f.model_trades > 0:
            folds_traded += 1
            traded_periods.append(f.period)
        m_avg = (f.model_pnl / f.model_trades * 100) if f.model_trades else 0.0
        a_avg = (f.always_pnl / f.test_n * 100) if f.test_n else 0.0
        promoted = "yes" if f.promoted else ""
        print(f"{f.period:>10} {f.train_n:>8} {f.test_n:>7} {f.model_trades:>8} "
              f"{m_avg:>+9.3f}% {f.model_pnl:>+8.2f} {a_avg:>+10.3f}% {promoted:>9} {f.champion_version:>8}")

    n_testable = len(report.folds)
    model_avg = (report.cumulative_model_pnl / report.total_model_trades * 100) if report.total_model_trades else 0.0
    always_trades = sum(f.test_n for f in report.folds)
    always_avg = (report.cumulative_always_pnl / always_trades * 100) if always_trades else 0.0
    print("-" * 96)
    print(f"{'TOTAL':>10} {'':>8} {'':>7} {report.total_model_trades:>8} "
          f"{model_avg:>+9.3f}% {report.cumulative_model_pnl:>+8.2f} {always_avg:>+10.3f}%")

    # MATCHED baseline: always-trade restricted to the SAME folds the model
    # traded. Comparing the model (measured only on its favorable folds) against
    # always-trade over ALL folds is a baseline-mismatch that credits fold-timing
    # (a regime bet) as if it were skill. The honest comparison is same-period.
    matched_always_pnl = sum(f.always_pnl for f in report.folds if f.model_trades > 0)
    matched_always_n = sum(f.test_n for f in report.folds if f.model_trades > 0)
    matched_always_avg = (matched_always_pnl / matched_always_n * 100) if matched_always_n else 0.0
    selection_lift = model_avg - matched_always_avg  # >0 only if the model beats trade-everything on matched periods
    # %/trade. Single source of truth + derivation: backtest/cost_model.py
    TRADABILITY_BAR = TRADABILITY_BAR_PCT

    print()
    print("=== Verdict ===")
    print(f"  learned policy        : {model_avg:+.3f}% mean net / trade  "
          f"({report.total_model_trades} trades over {folds_traded}/{n_testable} folds, "
          f"{report.n_promotions} promotions)")
    print(f"  always-trade (ALL folds): {always_avg:+.3f}% mean net / trade  ({always_trades} trades)")
    print(f"  always-trade (MATCHED) : {matched_always_avg:+.3f}% mean net / trade  "
          f"(trade-everything on the model's {folds_traded} folds)")
    print(f"  selection lift         : {selection_lift:+.3f}% / trade  "
          f"(model minus matched always — the only number that isolates skill from regime)")
    print()
    breadth = folds_traded / n_testable if n_testable else 0.0
    if report.total_model_trades == 0:
        print("  VERDICT: LEARNED TO ABSTAIN. No model version ever beat 'never trade' on held-out")
        print("  data — the correct outcome when there's no exploitable edge.")
    elif breadth < 0.5:
        print(f"  VERDICT: INSUFFICIENT BREADTH. Traded only {folds_traded}/{n_testable} folds "
              f"({', '.join(traded_periods)}).")
        print("  A policy that only fires in a few periods is fitting a regime, not finding edge.")
        print("  Do NOT trust as validated edge.")
    elif selection_lift <= 0:
        print(f"  VERDICT: NO SELECTION EDGE. The model's gain over all-folds always-trade is pure")
        print(f"  FOLD-TIMING (a regime bet) — on the SAME folds, trade-everything ({matched_always_avg:+.3f}%)")
        print(f"  matches or beats the model ({model_avg:+.3f}%). Within-fold stock selection adds")
        print(f"  {selection_lift:+.3f}%/trade. The machine is sound; this category has no selective edge.")
    elif selection_lift > 0 and model_avg > TRADABILITY_BAR:
        print("  VERDICT: POSITIVE SELECTIVE EDGE. Beats trade-everything on matched periods AND")
        print(f"  clears the {TRADABILITY_BAR}%/trade tradability bar. The strongest honest outcome the data")
        print("  can give — still verify on fresh out-of-sample data + realistic slippage/capacity.")
    else:
        print(f"  VERDICT: MARGINAL SELECTION. Positive selection lift ({selection_lift:+.3f}%/trade) but")
        print(f"  below the {TRADABILITY_BAR}%/trade tradability bar — real-world slippage and the single-")
        print("  position capacity limit would erase it. Not worth trading.")

    if args.out:
        rows = [vars(f) for f in report.folds]
        pd.DataFrame(rows).to_csv(args.out, index=False)
        print(f"\nWrote per-fold report -> {args.out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
