"""Slice the event-study results by various categorical/numeric features
to check if any subgroup shows edge that the aggregate hides.

Honest read: most quant strategies that "don't work in aggregate" don't work
in any subgroup either. But this is cheap to check and falsifies the
"maybe sub-X works?" hypothesis directly.

Subgroups:
- Market (KOSPI vs KOSDAQ)
- Ratio buckets (0.08-0.15, 0.15-0.30, 0.30-0.50, 0.50+)
- Contract value buckets (₩1B–10B, 10B–100B, 100B+)
- Day of week
- Calendar quarter
- Counterparty type (regex: 정부/공단/공사 = government, 주식회사 = corporate, else = other)
- Recent KOSPI volatility (low/med/high — proxied by close-to-close vol over event-window)

Output:
- Per-subgroup table to stdout
- Heatmaps to data/subgroup_*.csv
- Highlights any subgroup with statistically interesting numbers
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path

import pandas as pd

from kdtb.backtest.cost_model import CostModel
from kdtb.logging_setup import setup_logging


SUPPLY_QUERY = """
SELECT e.disclosure_id, e.contract_value_krw, e.prior_year_revenue_krw,
       e.contract_to_revenue_ratio, e.summary, e.validation_status
FROM extractions e
WHERE e.model_name = 'deterministic_supply_contract_v1'
  AND e.validation_status = 'ok'
"""


def classify_counterparty(summary: str | None) -> str:
    """Heuristic classification from the parser's summary (which contains the counterparty)."""
    if not summary:
        return "unknown"
    s = summary
    # Extract counterparty section after 'counterparty='
    m = re.search(r"counterparty=([^=]+?)(?:\s+\w+=|$)", s)
    if not m:
        return "unknown"
    cp = m.group(1).strip()
    if not cp or cp in ("None", "-"):
        return "unknown"
    # Government/public
    if re.search(r"(공단|공사|정부|시청|구청|도청|국립|보건|연구원)", cp):
        return "government"
    # Foreign
    if re.search(r"(Co\.|Ltd|Inc|Corp|LLC|GmbH|S\.A\.|미국|일본|중국|독일|영국)", cp, re.IGNORECASE):
        return "foreign"
    # Large Korean corporates (heuristic — chaebol affiliates and major listed companies)
    if re.search(r"(삼성|현대|LG|SK|롯데|한화|두산|포스코|GS|CJ|KT|네이버|카카오)", cp):
        return "large_corp_korean"
    # Default Korean
    if "주식회사" in cp or "㈜" in cp or "(주)" in cp:
        return "other_korean_corp"
    return "unknown"


def summarize(df: pd.DataFrame, label: str, cost: float) -> dict | None:
    """Compute key stats for a subgroup. Returns None if too small to be meaningful."""
    if len(df) < 20:
        return None
    t5 = df["ret_5d"].dropna()
    if len(t5) < 20:
        return None
    net = t5 - cost
    wins = net[net > 0]
    losses = -net[net < 0]
    return {
        "subgroup": label,
        "n": len(t5),
        "t1_net_pct": (df["ret_1d"].dropna() - cost).mean() * 100 if len(df["ret_1d"].dropna()) > 0 else None,
        "t5_net_pct": net.mean() * 100,
        "t5_med_pct": net.median() * 100,
        "t5_win_pct": (net > 0).mean() * 100,
        "t5_std_pct": net.std() * 100,
        "profit_factor_5d": (wins.sum() / losses.sum()) if len(losses) > 0 and losses.sum() > 0 else None,
        "sharpe_ish": (net.mean() / net.std()) if net.std() > 0 else None,
    }


def print_table(rows: list[dict], title: str) -> None:
    if not rows:
        print(f"\n=== {title}: no subgroups had n >= 20 ===")
        return
    print(f"\n=== {title} (n>=20 only) ===")
    print(f"{'subgroup':>28} {'n':>5} {'T+1_net':>10} {'T+5_net':>10} {'T+5_med':>10} {'T+5_win%':>10} {'pf_5d':>8} {'sharpe-ish':>11}")
    print("-" * 100)
    for r in sorted(rows, key=lambda x: -x["t5_net_pct"]):
        sharp = f"{r['sharpe_ish']:+.4f}" if r['sharpe_ish'] is not None else "—"
        pf = f"{r['profit_factor_5d']:.3f}" if r['profit_factor_5d'] is not None else "—"
        t1 = f"{r['t1_net_pct']:+.3f}%" if r['t1_net_pct'] is not None else "—"
        print(f"{r['subgroup'][:28]:>28} {r['n']:>5} {t1:>10} {r['t5_net_pct']:>+9.3f}% {r['t5_med_pct']:>+9.3f}% {r['t5_win_pct']:>9.1f}% {pf:>8} {sharp:>11}")


def main() -> int:
    setup_logging("INFO", False)
    log = logging.getLogger("subgroup_analysis")

    log.info("Loading event study results + extractions...")
    df = pd.read_csv("data/event_study_results.csv")
    conn = sqlite3.connect("data/kdtb.db")
    ex = pd.read_sql_query(SUPPLY_QUERY, conn)
    m = df.merge(ex, left_on="id", right_on="disclosure_id", how="inner")
    m = m.dropna(subset=["ret_5d"])
    log.info("Joined: %d events with both prices and parser output", len(m))

    cost = CostModel().roundtrip_cost(1.0)

    # === Subgroup 1: Market ===
    rows = []
    for market, sub in m.groupby("market"):
        r = summarize(sub, str(market), cost)
        if r:
            rows.append(r)
    print_table(rows, "Market")

    # === Subgroup 2: Ratio bucket ===
    bins = [0, 0.05, 0.10, 0.15, 0.30, 0.50, 1.0, 1000.0]
    labels = ["<0.05", "0.05-0.10", "0.10-0.15", "0.15-0.30", "0.30-0.50", "0.50-1.00", "1.00+"]
    m["ratio_bucket"] = pd.cut(m["contract_to_revenue_ratio"], bins=bins, labels=labels, right=False)
    rows = []
    for bucket, sub in m.groupby("ratio_bucket", observed=True):
        r = summarize(sub, f"ratio {bucket}", cost)
        if r:
            rows.append(r)
    print_table(rows, "Ratio bucket")

    # === Subgroup 3: Contract value bucket (KRW) ===
    bins_v = [0, 1e9, 1e10, 1e11, 1e12, 1e15]
    labels_v = ["<1B", "1B-10B", "10B-100B", "100B-1T", "1T+"]
    m["value_bucket"] = pd.cut(m["contract_value_krw"], bins=bins_v, labels=labels_v, right=False)
    rows = []
    for bucket, sub in m.groupby("value_bucket", observed=True):
        r = summarize(sub, f"value {bucket}", cost)
        if r:
            rows.append(r)
    print_table(rows, "Contract value bucket (KRW)")

    # === Subgroup 4: Day of week ===
    m["event_dt"] = pd.to_datetime(m["event_date"])
    m["dow"] = m["event_dt"].dt.day_name()
    rows = []
    for dow, sub in m.groupby("dow"):
        r = summarize(sub, str(dow), cost)
        if r:
            rows.append(r)
    print_table(rows, "Day of week (event_date)")

    # === Subgroup 5: Calendar quarter ===
    m["quarter"] = m["event_dt"].dt.to_period("Q").astype(str)
    rows = []
    for q, sub in m.groupby("quarter"):
        r = summarize(sub, str(q), cost)
        if r:
            rows.append(r)
    print_table(rows, "Calendar quarter (regime check)")

    # === Subgroup 6: Counterparty type (regex from parser summary) ===
    m["counterparty_type"] = m["summary"].apply(classify_counterparty)
    rows = []
    for ct, sub in m.groupby("counterparty_type"):
        r = summarize(sub, ct, cost)
        if r:
            rows.append(r)
    print_table(rows, "Counterparty type (regex heuristic)")

    # === Subgroup 7: Cross of market × ratio bucket (the most interesting cut) ===
    print("\n=== Market × Ratio (≥0.15 only, n>=20) ===")
    print(f"{'market':>8} {'ratio_bucket':>15} {'n':>5} {'T+5_net':>10} {'T+5_win%':>10} {'pf_5d':>8}")
    print("-" * 70)
    for (mkt, bucket), sub in m.groupby(["market", "ratio_bucket"], observed=True):
        if bucket not in ["0.15-0.30", "0.30-0.50", "0.50-1.00", "1.00+"]:
            continue
        r = summarize(sub, f"{mkt}/{bucket}", cost)
        if r:
            print(f"{mkt:>8} {str(bucket):>15} {r['n']:>5} {r['t5_net_pct']:>+9.3f}% {r['t5_win_pct']:>9.1f}% {r['profit_factor_5d'] or 0:>8.3f}")

    # Save consolidated CSV
    out_csv = Path("data/subgroup_analysis.csv")
    m.to_csv(out_csv, index=False)
    log.info("Wrote merged dataset -> %s", out_csv)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
