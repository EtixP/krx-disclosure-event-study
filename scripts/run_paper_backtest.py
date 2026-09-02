"""Realized-PnL backtest of the v2 strategy under three execution assumptions.

The event study so far has measured returns relative to event-day close. In
real-world execution, you usually can't buy at the event-day close — the
disclosure may hit after hours, the market may have already moved. This script
re-prices the v2 signals under more realistic entry/exit assumptions.

Three scenarios, all using the existing event_study_results.csv close prices
(no new fetches needed):

  scenario       buy_at        sell_at        hold (trading days)
  ----------------------------------------------------------------
  idealized      t0_close      t+5_close      5  (matches event study)
  realistic      t+1_close     t+5_close      4
  conservative   t+2_close     t+5_close      3

Costs use the statutory rate for each exact exit date and market.

For each scenario, computes:
  - Per-trade realized return (gross + net)
  - Aggregate stats (mean, median, win%, profit factor)
  - Cumulative PnL on a hypothetical ₩30k/trade single-position account

Output: data/paper_backtest_v2.csv (per-trade rows for analysis).

Note: this is still a SIMULATION — it doesn't account for daily H/L stop-loss
triggers (Loop 10 will add OHLC fetching for that). But it accurately reflects
the cost of "we can't always trade at event-day close".
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pandas as pd

from kdtb.backtest.cost_model import CostModel
from kdtb.backtest.metrics import compute
from kdtb.data.benchmarks import require_benchmark_columns
from kdtb.logging_setup import setup_logging


SIGNAL_QUERY = """
SELECT s.signal_id, s.disclosure_id, s.stock_code, s.strength,
       e.contract_to_revenue_ratio, e.counterparty_type,
       d.receipt_no, d.market, d.receipt_datetime
FROM signals s
JOIN disclosures d ON s.disclosure_id = d.id
JOIN extractions e ON s.extraction_id = e.id
WHERE s.strategy_name = 'major_supply_contract_v1'
ORDER BY d.receipt_datetime
"""

NOTIONAL_PER_TRADE_KRW = 30_000


def _scenario(
    prices: pd.DataFrame,
    buy_col: str,
    sell_col: str,
    cost_model: CostModel,
) -> pd.DataFrame:
    """Compute per-trade gross+net returns for an entry/exit pair."""
    buy_date_col = buy_col.replace("close", "date")
    sell_date_col = sell_col.replace("close", "date")
    out = prices.dropna(subset=[buy_col, sell_col]).copy()
    dated = [buy_date_col, sell_date_col, "market"]
    if out[dated].isna().any().any():
        raise ValueError("dated transaction-cost inputs contain missing values")
    out["gross_return"] = (out[sell_col] - out[buy_col]) / out[buy_col]
    buy_token = buy_col.removesuffix("_close").replace("+", "")
    sell_token = sell_col.removesuffix("_close").replace("+", "")
    require_benchmark_columns(out, tokens=(buy_token, sell_token))
    benchmark_buy = f"benchmark_{buy_token}_close"
    benchmark_sell = f"benchmark_{sell_token}_close"
    out["benchmark_return"] = (
        (out[benchmark_sell] - out[benchmark_buy]) / out[benchmark_buy]
    )
    out["abnormal_gross_return"] = out["gross_return"] - out["benchmark_return"]
    out["cost_fraction"] = cost_model.roundtrip_cost_fractions(
        buy_dates=out[buy_date_col],
        sell_dates=out[sell_date_col],
        markets=out["market"],
    )
    out["net_return"] = out["gross_return"] - out["cost_fraction"]
    out["abnormal_net_return"] = (
        out["abnormal_gross_return"] - out["cost_fraction"]
    )
    return out


def _summarize(label: str, rs: pd.Series, notional: int) -> dict:
    n = len(rs)
    if n == 0:
        return {"scenario": label, "n": 0}
    m = compute(rs.tolist())
    # Translate to KRW PnL on a fixed-notional, single-position account
    total_krw = (rs * notional).sum()
    return {
        "scenario": label,
        "n": n,
        "mean_pct": m.mean_return * 100,
        "median_pct": m.median_return * 100,
        "win_pct": m.win_rate * 100,
        "pf": m.profit_factor,
        "sharpe_ish": m.sharpe_like,
        "max_dd_pct": m.max_drawdown * 100,
        "total_krw": total_krw,
        "krw_per_trade": rs.mean() * notional,
    }


def main() -> int:
    setup_logging("INFO", False)
    log = logging.getLogger("paper_backtest")

    conn = sqlite3.connect("data/kdtb.db")
    signals = pd.read_sql_query(SIGNAL_QUERY, conn)
    conn.close()
    log.info("Loaded %d v2 signals", len(signals))

    prices = pd.read_csv("data/event_study_results.csv")
    needed = [
        "id", "stock_code", "event_date",
        "t0_close", "t+1_close", "t+2_close", "t+5_close",
        "t0_date", "t+1_date", "t+2_date", "t+5_date",
        "benchmark_source", "benchmark_symbol", "benchmark_alignment",
        "benchmark_t0_close", "benchmark_t1_close", "benchmark_t2_close",
        "benchmark_t5_close",
    ]
    missing = [c for c in needed if c not in prices.columns]
    if missing:
        log.error("event_study_results.csv missing columns: %s", missing)
        return 1
    log.info("Loaded %d price rows", len(prices))

    # Use only the columns we need from prices; rename to avoid clash with signals.stock_code
    price_subset = prices[needed].rename(columns={"stock_code": "stock_code_price"})
    merged = signals.merge(price_subset, left_on="disclosure_id", right_on="id", how="inner")
    log.info("Joined: %d signals with prices", len(merged))

    cost_model = CostModel()
    print("\nCost model: exact sell-date/market statutory schedule\n")

    scenarios = [
        ("idealized   (buy t0_close → sell t+5_close)", "t0_close",  "t+5_close"),
        ("realistic   (buy t+1_close → sell t+5_close)", "t+1_close", "t+5_close"),
        ("conservative(buy t+2_close → sell t+5_close)", "t+2_close", "t+5_close"),
    ]

    rows = []
    per_trade_outputs = {}
    for label, buy, sell in scenarios:
        df = _scenario(merged, buy, sell, cost_model)
        per_trade_outputs[label] = df
        summary = _summarize(label, df["net_return"], NOTIONAL_PER_TRADE_KRW)
        abnormal = _summarize(
            label, df["abnormal_net_return"], NOTIONAL_PER_TRADE_KRW
        )
        summary["abnormal_mean_pct"] = abnormal.get("mean_pct")
        summary["abnormal_pf"] = abnormal.get("pf")
        rows.append(summary)

    print(f"=== Realized-PnL backtest of v2 strategy on {len(merged)} signals ===")
    print(f"  notional per trade: ₩{NOTIONAL_PER_TRADE_KRW:,}")
    print()
    print(f"{'scenario':>52} {'n':>4} {'raw_mean':>9} {'abn_mean':>9} {'raw_med':>9} {'raw_win%':>9} {'raw_PF':>7} {'abn_PF':>7} {'raw KRW/trd':>12}")
    print("-" * 135)
    for r in rows:
        if r["n"] == 0:
            continue
        pf_s = f"{r['pf']:.3f}" if r['pf'] is not None and r['pf'] != float('inf') else "—"
        abnormal_pf_s = f"{r['abnormal_pf']:.3f}" if r['abnormal_pf'] is not None and r['abnormal_pf'] != float('inf') else "—"
        print(
            f"{r['scenario']:>52} {r['n']:>4} "
            f"{r['mean_pct']:>+8.3f}% {r['abnormal_mean_pct']:>+8.3f}% "
            f"{r['median_pct']:>+8.3f}% {r['win_pct']:>8.1f}% "
            f"{pf_s:>7} {abnormal_pf_s:>7} ₩{r['krw_per_trade']:>+10.0f}"
        )

    # Save the realistic scenario per-trade for further analysis
    realistic_label = "realistic   (buy t+1_close → sell t+5_close)"
    realistic = per_trade_outputs[realistic_label]
    out_csv = Path("data/paper_backtest_v2.csv")
    realistic[["signal_id", "stock_code", "market", "counterparty_type",
               "contract_to_revenue_ratio", "event_date", "t+1_close",
               "t+5_close", "t+1_date", "t+5_date", "cost_fraction",
               "benchmark_symbol", "benchmark_t1_close", "benchmark_t5_close",
               "gross_return", "benchmark_return", "abnormal_gross_return",
               "net_return", "abnormal_net_return"]].to_csv(out_csv, index=False)
    log.info("Wrote per-trade detail -> %s", out_csv)

    # Tally exit-type breakdown for realistic scenario
    print(f"\n=== Realistic scenario, distribution of net returns ===")
    bins = [-1.0, -0.05, -0.02, -0.005, 0.005, 0.02, 0.05, 1.0]
    labels = ["<-5%", "-5%..-2%", "-2%..-0.5%", "-0.5%..+0.5%", "+0.5%..+2%", "+2%..+5%", ">+5%"]
    realistic_buckets = pd.cut(realistic["net_return"], bins=bins, labels=labels)
    counts = realistic_buckets.value_counts().reindex(labels)
    for lab, c in counts.items():
        pct = c / len(realistic) * 100
        bar = "#" * int(pct)
        print(f"  {lab:>15} {c:>4} ({pct:>5.1f}%) {bar}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
