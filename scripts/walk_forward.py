"""Walk-forward validation of the v0/v1/v2 strategy variants.

Splits the 24 months of supply-contract events into 4 non-overlapping 6-month
windows and reports per-window net-of-cost T+5 returns for each variant.
Aggregate counts how many windows each variant was positive in — that's the
real test of whether an "edge" is robust or curve-fit.

Usage:
    python scripts/walk_forward.py
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pandas as pd

from kdtb.backtest.cost_model import CostModel
from kdtb.data.benchmarks import require_benchmark_columns
from kdtb.logging_setup import setup_logging


WINDOWS: list[tuple[str, str, str]] = [
    ("2024-07-01", "2025-01-01", "Jul24-Dec24"),
    ("2025-01-01", "2025-07-01", "Jan25-Jun25"),
    ("2025-07-01", "2026-01-01", "Jul25-Dec25"),
    ("2026-01-01", "2026-07-01", "Jan26-Jun26"),
]


def _v0(df: pd.DataFrame) -> pd.DataFrame:
    return df


def _v1(df: pd.DataFrame) -> pd.DataFrame:
    return df[df["contract_to_revenue_ratio"] >= 0.08]


def _v2(df: pd.DataFrame) -> pd.DataFrame:
    return df[
        (df["contract_to_revenue_ratio"] >= 0.08)
        & (df["market"] == "KOSPI")
        & (df["counterparty_type"] != "government")
    ]


VARIANTS = {
    "v0 (no filters)": _v0,
    "v1 (ratio>=0.08)": _v1,
    "v2 (ratio>=0.08 + KOSPI + skip-gov)": _v2,
}


def _stats(t5_net: pd.Series) -> dict:
    wins = t5_net[t5_net > 0].sum()
    losses = -t5_net[t5_net < 0].sum() if (t5_net < 0).any() else 0
    pf = wins / losses if losses > 0 else float("inf")
    return {
        "n": len(t5_net),
        "mean_pct": t5_net.mean() * 100,
        "median_pct": t5_net.median() * 100,
        "win_pct": (t5_net > 0).mean() * 100,
        "pf": pf,
    }


def main() -> int:
    setup_logging("INFO", False)
    log = logging.getLogger("walk_forward")

    log.info("Loading data...")
    conn = sqlite3.connect("data/kdtb.db")
    ex = pd.read_sql_query(
        """
        SELECT disclosure_id, contract_to_revenue_ratio, counterparty_type
        FROM extractions
        WHERE model_name='deterministic_supply_contract_v1' AND validation_status='ok'
        """,
        conn,
    )
    conn.close()
    prices = pd.read_csv("data/event_study_results.csv")
    m = prices.merge(ex, left_on="id", right_on="disclosure_id", how="inner").dropna(subset=["ret_5d"])
    m["event_dt"] = pd.to_datetime(m["event_date"])
    log.info("Joined: %d events", len(m))

    model = CostModel()
    require_benchmark_columns(m, tokens=("t0", "t5"))
    required = ["t0_date", "t+5_date", "market"]
    if m[required].isna().any().any():
        raise ValueError("dated transaction-cost inputs contain missing values")
    m["_t5_net"] = m["ret_5d"] - model.roundtrip_cost_fractions(
        buy_dates=m["t0_date"], sell_dates=m["t+5_date"], markets=m["market"]
    )
    m["_t5_abnormal_net"] = m["abnormal_ret_5d"] - model.roundtrip_cost_fractions(
        buy_dates=m["t0_date"], sell_dates=m["t+5_date"], markets=m["market"]
    )

    print()
    print(f"{'window':>16} | {'v0_n':>5} {'v0_raw':>9} {'v0_abn':>9} | {'v1_n':>5} {'v1_raw':>9} {'v1_abn':>9} | {'v2_n':>5} {'v2_raw':>9} {'v2_abn':>9}")
    print("-" * 114)

    agg: dict[str, dict] = {name: {"n": 0, "sum_raw": 0.0, "sum_abnormal": 0.0, "wins": 0.0, "losses": 0.0, "win_count": 0, "pos_windows": 0} for name in VARIANTS}

    for start, end, label in WINDOWS:
        window = m[(m["event_dt"] >= start) & (m["event_dt"] < end)]
        parts: list[str] = []
        for vname, vfn in VARIANTS.items():
            sub = vfn(window)
            t5_raw = sub["_t5_net"].dropna()
            t5_abnormal = sub["_t5_abnormal_net"].dropna()
            if len(t5_abnormal) < 5:
                parts.append(f"{len(t5_abnormal):>5} {'—':>9} {'—':>9}")
                continue
            agg[vname]["n"] += len(t5_abnormal)
            agg[vname]["sum_raw"] += t5_raw.sum()
            agg[vname]["sum_abnormal"] += t5_abnormal.sum()
            agg[vname]["wins"] += t5_abnormal[t5_abnormal > 0].sum()
            agg[vname]["losses"] += -t5_abnormal[t5_abnormal < 0].sum() if (t5_abnormal < 0).any() else 0
            agg[vname]["win_count"] += int((t5_abnormal > 0).sum())
            if t5_abnormal.mean() > 0:
                agg[vname]["pos_windows"] += 1
            parts.append(f"{len(t5_abnormal):>5} {t5_raw.mean()*100:>+8.3f}% {t5_abnormal.mean()*100:>+8.3f}%")
        print(f"{label:>16} | {parts[0]} | {parts[1]} | {parts[2]}")

    print()
    print(f"{'variant':>40} {'n':>5} {'raw mean':>10} {'abn mean':>10} {'abn win%':>9} {'abn PF':>8} {'abn pos_windows':>16}")
    for vname, a in agg.items():
        if a["n"] == 0:
            continue
        raw_mean = a["sum_raw"] / a["n"]
        abnormal_mean = a["sum_abnormal"] / a["n"]
        pf = a["wins"] / a["losses"] if a["losses"] > 0 else float("inf")
        winp = a["win_count"] / a["n"] * 100
        print(f"{vname:>40} {a['n']:>5} {raw_mean * 100:>+9.3f}% {abnormal_mean * 100:>+9.3f}% {winp:>8.1f}% {pf:>8.3f} {a['pos_windows']:>8}/4")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
