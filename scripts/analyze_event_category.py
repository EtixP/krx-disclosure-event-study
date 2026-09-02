"""Comprehensive raw and benchmark-adjusted analysis of one event category.

Used as a uniform analyzer across all event types so results are directly
comparable. Operates on the CSV that scripts/run_event_study.py produced for
that category (data/event_study_<category>.csv).

Usage:
    python scripts/analyze_event_category.py --category buyback
    python scripts/analyze_event_category.py --category buyback --json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

from kdtb.backtest.cost_model import (
    MIN_TRADABLE_PF,
    MIN_TRADABLE_WINDOW_FRACTION,
    TRADABILITY_BAR_PCT,
    CostModel,
)
from kdtb.backtest.metrics import compute
from kdtb.data.benchmarks import require_benchmark_columns


def _auto_windows(df: pd.DataFrame, months_per_window: int = 6) -> list[tuple[str, str, str]]:
    """Generate non-overlapping windows from data's date range, half-year aligned.

    Half-year aligned (Jan 1 / Jul 1) so windows are deterministic across runs.
    Drops trailing partial windows (< 4 weeks of data) since they have too few events.
    """
    if "event_dt" in df.columns:
        first_dt = df["event_dt"].min()
        last_dt = df["event_dt"].max()
    else:
        first_dt = pd.to_datetime(df["event_date"].min())
        last_dt = pd.to_datetime(df["event_date"].max())
    if pd.isna(first_dt) or pd.isna(last_dt):
        return []
    start_year = int(first_dt.year)
    start_half = 1 if first_dt.month <= 6 else 7
    cur = pd.Timestamp(year=start_year, month=start_half, day=1)
    if cur > first_dt:
        cur = pd.Timestamp(year=start_year - 1 if start_half == 1 else start_year,
                           month=7 if start_half == 1 else 1, day=1)
    out: list[tuple[str, str, str]] = []
    one_day = pd.Timedelta(1, unit="D")
    while cur < last_dt:
        nxt = cur + pd.DateOffset(months=months_per_window)
        last_day = nxt - one_day
        # Drop trailing partial windows where we don't have enough data to fill
        if (min(nxt, last_dt + one_day) - cur).days >= 28:
            mlabel = "Jan" if cur.month == 1 else "Jul"
            # Window ends at (nxt - 1 day). If that's in Jun -> 'Jun'; if in Dec -> 'Dec'.
            mnxt = "Dec" if last_day.month == 12 else "Jun"
            label = f"{mlabel}{cur.year % 100:02d}-{mnxt}{last_day.year % 100:02d}"
            out.append((cur.strftime("%Y-%m-%d"), nxt.strftime("%Y-%m-%d"), label))
        cur = nxt
    return out


# Legacy fixed windows — kept for back-compat with scripts/walk_forward.py
WINDOWS: list[tuple[str, str, str]] = [
    ("2024-07-01", "2025-01-01", "Jul24-Dec24"),
    ("2025-01-01", "2025-07-01", "Jan25-Jun25"),
    ("2025-07-01", "2026-01-01", "Jul25-Dec25"),
    ("2026-01-01", "2026-07-01", "Jan26-Jun26"),
]


# A walk-forward window with fewer than this many events is not scored: its
# mean is noise. Such windows are excluded from BOTH the numerator and the
# denominator of the 'windows positive' count, so the denominator is the
# number of windows actually SCORED, not the number generated. Reporting it
# the other way (scored/generated) is what made an earlier hand-written
# summary disagree with this script -- see RESEARCH_FINDINGS.md.
MIN_WINDOW_EVENTS = 5


def _stats(series: pd.Series) -> dict:
    s = series.dropna()
    if len(s) == 0:
        return {"n": 0}
    m = compute(s.tolist())
    return {
        "n": len(s),
        "mean_pct": round(m.mean_return * 100, 4),
        "median_pct": round(m.median_return * 100, 4),
        "win_pct": round(m.win_rate * 100, 2),
        "std_pct": round(m.std_return * 100, 4),
        "pf": round(m.profit_factor, 4) if m.profit_factor is not None and m.profit_factor != float("inf") else None,
        "sharpe_ish": round(m.sharpe_like, 6) if m.sharpe_like is not None else None,
    }


def _cost_series(
    df: pd.DataFrame,
    *,
    buy_close: str,
    sell_close: str,
    cost_model: CostModel,
    flat_cost_fraction: float | None,
) -> pd.Series:
    if flat_cost_fraction is not None:
        return pd.Series(float(flat_cost_fraction), index=df.index, dtype=float)
    buy_date = buy_close.replace("close", "date")
    sell_date = sell_close.replace("close", "date")
    missing = [
        column
        for column in (buy_date, sell_date, "market")
        if column not in df.columns
    ]
    if missing:
        raise ValueError(
            "dated transaction costs require event-study columns: "
            + ", ".join(missing)
        )
    if df[[buy_date, sell_date, "market"]].isna().any().any():
        raise ValueError("dated transaction-cost inputs contain missing values")
    return pd.Series(
        cost_model.roundtrip_cost_fractions(
            buy_dates=df[buy_date],
            sell_dates=df[sell_date],
            markets=df["market"],
        ),
        index=df.index,
        dtype=float,
    )


def _walk_forward(
    df: pd.DataFrame,
    windows: list[tuple[str, str, str]] | None = None,
    *,
    return_column: str = "_t5_net",
) -> list[dict]:
    if windows is None:
        windows = _auto_windows(df)
    out = []
    for start, end, label in windows:
        sub = df[(df["event_dt"] >= start) & (df["event_dt"] < end)]
        s = _stats(sub[return_column])
        s["window"] = label
        out.append(s)
    return out


def _split_by_market(df: pd.DataFrame, *, return_column: str = "_t5_net") -> dict:
    out = {}
    for market, sub in df.groupby("market"):
        out[str(market)] = _stats(sub[return_column])
    return out


def _realistic_scenarios(
    df: pd.DataFrame,
    *,
    cost_model: CostModel,
    flat_cost_fraction: float | None,
    benchmark_adjusted: bool = False,
) -> dict:
    out = {}
    scenarios = [
        ("idealized",    "t0_close",  "t+5_close"),
        ("realistic",    "t+1_close", "t+5_close"),
        ("conservative", "t+2_close", "t+5_close"),
    ]
    for name, buy, sell in scenarios:
        if buy not in df.columns or sell not in df.columns:
            out[name] = {"n": 0, "skipped": "missing column"}
            continue
        sub = df.dropna(subset=[buy, sell]).copy()
        sub["gross"] = (sub[sell] - sub[buy]) / sub[buy]
        if benchmark_adjusted:
            buy_token = buy.removesuffix("_close").replace("+", "")
            sell_token = sell.removesuffix("_close").replace("+", "")
            benchmark_buy = f"benchmark_{buy_token}_close"
            benchmark_sell = f"benchmark_{sell_token}_close"
            if sub[[benchmark_buy, benchmark_sell]].isna().any().any():
                raise ValueError(
                    f"benchmark-adjusted scenario {name} has missing exact-date closes"
                )
            benchmark_gross = (
                (sub[benchmark_sell] - sub[benchmark_buy]) / sub[benchmark_buy]
            )
            sub["gross"] = sub["gross"] - benchmark_gross
        sub["cost"] = _cost_series(
            sub,
            buy_close=buy,
            sell_close=sell,
            cost_model=cost_model,
            flat_cost_fraction=flat_cost_fraction,
        )
        sub["net"] = sub["gross"] - sub["cost"]
        out[name] = _stats(sub["net"])
    return out


def _verdict(walk_forward: list[dict], realistic: dict) -> str:
    """Categorize the finding using percentages so it scales to any number of windows.

    positive_robust: >=MIN_TRADABLE_WINDOW_FRACTION of windows positive
                     AND realistic_mean > TRADABILITY_BAR_PCT
                     AND realistic_pf > MIN_TRADABLE_PF
                     (see backtest/cost_model.py for where the bar comes from)
    positive_noisy:  >=60% of windows positive AND realistic_mean > +0.10% (but doesn't clear robust bar)
    negative:        <=20% positive AND realistic_mean < -0.10%
    neutral:         everything else
    insufficient_data: <4 valid windows
    """
    valid = [w for w in walk_forward if w.get("n", 0) >= MIN_WINDOW_EVENTS]
    if len(valid) < 4:
        return "insufficient_data"
    pos_windows = sum(1 for w in valid if w.get("mean_pct", 0) > 0)
    pos_pct = pos_windows / len(valid)
    realistic_block = realistic.get("realistic", {}) or {}
    realistic_mean = realistic_block.get("mean_pct", 0) or 0
    realistic_pf = realistic_block.get("pf") or 0
    if (pos_pct >= MIN_TRADABLE_WINDOW_FRACTION
            and realistic_mean > TRADABILITY_BAR_PCT
            and realistic_pf > MIN_TRADABLE_PF):
        return "positive_robust"
    if pos_pct >= 0.60 and realistic_mean > 0.10:
        return "positive_noisy"
    if pos_pct <= 0.20 and realistic_mean < -0.10:
        return "negative"
    return "neutral"


def analyze(
    category: str,
    csv_path: str | None = None,
    *,
    flat_cost_fraction: float | None = None,
    include_abnormal: bool = True,
) -> dict:
    if csv_path is None:
        csv_path = f"data/event_study_{category}.csv"
    if not Path(csv_path).exists():
        return {"category": category, "error": f"CSV not found: {csv_path}"}

    df = pd.read_csv(csv_path)
    df = df.dropna(subset=["ret_5d"])
    if len(df) == 0:
        return {"category": category, "error": "no rows with ret_5d"}
    df["event_dt"] = pd.to_datetime(df["event_date"])

    cost_model = CostModel()
    df["_t1_cost"] = _cost_series(
        df,
        buy_close="t0_close",
        sell_close="t+1_close",
        cost_model=cost_model,
        flat_cost_fraction=flat_cost_fraction,
    )
    df["_t5_cost"] = _cost_series(
        df,
        buy_close="t0_close",
        sell_close="t+5_close",
        cost_model=cost_model,
        flat_cost_fraction=flat_cost_fraction,
    )
    df["_t1_net"] = df["ret_1d"] - df["_t1_cost"]
    df["_t5_net"] = df["ret_5d"] - df["_t5_cost"]

    aggregate = {
        "t1_gross": _stats(df["ret_1d"]),
        "t1_net":   _stats(df["_t1_net"]),
        "t5_gross": _stats(df["ret_5d"]),
        "t5_net":   _stats(df["_t5_net"]),
    }

    walk_forward = _walk_forward(df)
    by_market = _split_by_market(df)
    realistic = _realistic_scenarios(
        df,
        cost_model=cost_model,
        flat_cost_fraction=flat_cost_fraction,
    )
    raw_verdict = _verdict(walk_forward, realistic)

    if include_abnormal:
        require_benchmark_columns(df)
        if df[["abnormal_ret_1d", "abnormal_ret_5d"]].isna().any().any():
            raise ValueError("benchmark-adjusted returns contain missing values")
        df["_t1_abnormal_net"] = df["abnormal_ret_1d"] - df["_t1_cost"]
        df["_t5_abnormal_net"] = df["abnormal_ret_5d"] - df["_t5_cost"]
        aggregate.update(
            {
                "t1_abnormal_gross": _stats(df["abnormal_ret_1d"]),
                "t1_abnormal_net": _stats(df["_t1_abnormal_net"]),
                "t5_abnormal_gross": _stats(df["abnormal_ret_5d"]),
                "t5_abnormal_net": _stats(df["_t5_abnormal_net"]),
            }
        )
        walk_forward_abnormal = _walk_forward(
            df, return_column="_t5_abnormal_net"
        )
        by_market_abnormal = _split_by_market(
            df, return_column="_t5_abnormal_net"
        )
        realistic_abnormal = _realistic_scenarios(
            df,
            cost_model=cost_model,
            flat_cost_fraction=flat_cost_fraction,
            benchmark_adjusted=True,
        )
        abnormal_verdict = _verdict(
            walk_forward_abnormal, realistic_abnormal
        )
    else:
        walk_forward_abnormal = None
        by_market_abnormal = None
        realistic_abnormal = None
        abnormal_verdict = None

    result = {
        "category": category,
        "csv_path": csv_path,
        "n_events": len(df),
        "n_unique_stocks": int(df["stock_code"].nunique()) if "stock_code" in df.columns else None,
        "aggregate": aggregate,
        "walk_forward": walk_forward,
        "by_market": by_market,
        "realistic": realistic,
        "cost_fraction": {
            "t1_min": float(df["_t1_cost"].min()),
            "t1_max": float(df["_t1_cost"].max()),
            "t5_min": float(df["_t5_cost"].min()),
            "t5_max": float(df["_t5_cost"].max()),
        },
        "verdict": abnormal_verdict if include_abnormal else raw_verdict,
    }
    if include_abnormal:
        result.update(
            {
                "walk_forward_abnormal": walk_forward_abnormal,
                "by_market_abnormal": by_market_abnormal,
                "realistic_abnormal": realistic_abnormal,
                "verdict_raw": raw_verdict,
                "verdict_basis": "benchmark_adjusted_net",
            }
        )
    return result


def _human_report(result: dict) -> str:
    if "error" in result:
        return f"=== {result['category']}: ERROR — {result['error']} ==="
    a = result["aggregate"]
    lines = [
        f"=== {result['category']}  |  n={result['n_events']}  unique stocks={result['n_unique_stocks']} ===",
        f"  aggregate:",
        f"    T+1 raw net:       mean={a['t1_net'].get('mean_pct', 0):+.3f}%  median={a['t1_net'].get('median_pct', 0):+.3f}%  win%={a['t1_net'].get('win_pct', 0):.1f}  PF={a['t1_net'].get('pf', 'n/a')}",
        f"    T+1 abnormal net:  mean={a['t1_abnormal_net'].get('mean_pct', 0):+.3f}%  median={a['t1_abnormal_net'].get('median_pct', 0):+.3f}%  win%={a['t1_abnormal_net'].get('win_pct', 0):.1f}  PF={a['t1_abnormal_net'].get('pf', 'n/a')}",
        f"    T+5 raw net:       mean={a['t5_net'].get('mean_pct', 0):+.3f}%  median={a['t5_net'].get('median_pct', 0):+.3f}%  win%={a['t5_net'].get('win_pct', 0):.1f}  PF={a['t5_net'].get('pf', 'n/a')}",
        f"    T+5 abnormal net:  mean={a['t5_abnormal_net'].get('mean_pct', 0):+.3f}%  median={a['t5_abnormal_net'].get('median_pct', 0):+.3f}%  win%={a['t5_abnormal_net'].get('win_pct', 0):.1f}  PF={a['t5_abnormal_net'].get('pf', 'n/a')}",
        f"  realistic execution (T+1 close -> T+5 close), raw / abnormal:",
        f"    raw mean={result['realistic'].get('realistic', {}).get('mean_pct', 'n/a')}%  abnormal mean={result['realistic_abnormal'].get('realistic', {}).get('mean_pct', 'n/a')}%",
        f"  walk-forward (T+5 abnormal net mean / pos_windows):",
    ]
    pos = 0
    valid = 0
    for w in result["walk_forward_abnormal"]:
        if w.get("n", 0) >= MIN_WINDOW_EVENTS:
            valid += 1
            if w.get("mean_pct", 0) > 0:
                pos += 1
        m = w.get("mean_pct", "n/a")
        m_str = f"{m:+.3f}%" if isinstance(m, (int, float)) else m
        lines.append(f"    {w['window']:>15}  n={w.get('n', 0):>4}  T+5 abnormal net={m_str}")
    skipped = len(result["walk_forward_abnormal"]) - valid
    note = f"  ({skipped} window(s) skipped: fewer than {MIN_WINDOW_EVENTS} events)" if skipped else ""
    lines.append(f"    => {pos}/{valid} scored windows positive{note}")
    lines.append(f"  by market:")
    for mk, st in result["by_market_abnormal"].items():
        if st.get("n", 0) >= 5:
            lines.append(f"    {mk:>8}  n={st['n']:>4}  T+5 abnormal net={st.get('mean_pct', 0):+.3f}%  win%={st.get('win_pct', 0):.1f}  PF={st.get('pf', 'n/a')}")
    lines.append(f"  verdict (benchmark-adjusted): {result['verdict']}")
    lines.append(f"  raw-return verdict (diagnostic): {result['verdict_raw']}")
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--category", required=True)
    p.add_argument("--csv", default=None, help="override CSV path")
    p.add_argument("--json", action="store_true", help="emit JSON only")
    args = p.parse_args()

    result = analyze(args.category, args.csv)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(_human_report(result))
    return 0 if "error" not in result else 1


if __name__ == "__main__":
    sys.exit(main())
