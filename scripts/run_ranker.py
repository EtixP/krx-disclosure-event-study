"""Multi-factor Korean equity ranker — the watchlist tool.

Loads cached fundamentals (from build_fundamentals_cache.py), fetches the
current price + 12-month momentum per stock, scores each on VALUE / QUALITY /
MOMENTUM, and prints a ranked, explainable watchlist (best first).

Usage:
    python scripts/run_ranker.py                          # default weights
    python scripts/run_ranker.py --top 40 --min-mcap 300  # ₩300B floor, top 40
    python scripts/run_ranker.py --w-value 0.5 --w-quality 0.3 --w-momentum 0.2
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
from datetime import date, datetime
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

from kdtb.config import load_settings
from kdtb.logging_setup import setup_logging
from kdtb.ranker.prices import fetch_price_and_momentum
from kdtb.ranker.ranker import FactorWeights, RankFilters, rank_universe
from kdtb.ranker.theme_context import annotate_hot_themes
from kdtb.ranker.theme_momentum import attach_theme_score, hot_themes, theme_strength
from kdtb.storage.db import init_db

CACHE_SQL = """
SELECT f.corp_code, f.stock_code, f.fiscal_year, f.equity, f.net_income,
       f.revenue, f.debt, f.shares,
       (SELECT MAX(corp_name) FROM disclosures d WHERE d.corp_code = f.corp_code) AS corp_name,
       (SELECT MAX(market) FROM disclosures d WHERE d.corp_code = f.corp_code) AS market
FROM fundamentals f
WHERE f.equity IS NOT NULL AND f.shares IS NOT NULL AND f.shares > 0
"""


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--top", type=int, default=30)
    p.add_argument("--min-mcap", type=float, default=100.0, help="min market cap in ₩billion")
    p.add_argument("--w-value", type=float, default=0.35)
    p.add_argument("--w-quality", type=float, default=0.30)
    p.add_argument("--w-momentum", type=float, default=0.15)
    p.add_argument("--w-theme", type=float, default=0.20, help="data-driven theme-momentum tilt")
    p.add_argument("--llm-context", action="store_true",
                   help="annotate hot themes with LLM current-events context (needs API key)")
    p.add_argument("--limit", type=int, default=None, help="cap how many cached names to price (testing)")
    p.add_argument("--asof", default=None, help="YYYY-MM-DD price date (default today)")
    p.add_argument("--out", default="data/ranker_watchlist.csv")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    load_dotenv(".env")
    settings = load_settings([Path("config/default.yaml")])
    setup_logging(settings.logging.level, settings.logging.json_format)
    log = logging.getLogger("ranker")

    conn = init_db(settings.storage.sqlite_path)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(CACHE_SQL).fetchall()]
    if not rows:
        log.error("no cached fundamentals — run scripts/build_fundamentals_cache.py first")
        return 1
    if args.limit:
        rows = rows[: args.limit]
    log.info("cached fundamentals: %d companies — fetching prices/momentum...", len(rows))

    asof = datetime.strptime(args.asof, "%Y-%m-%d").date() if args.asof else date.today()
    priced = []
    for i, r in enumerate(rows, 1):
        price, mom = fetch_price_and_momentum(r["stock_code"], asof)
        if price is None:
            continue
        priced.append({**r, "price": price, "mom_12m": mom})
        if i % 100 == 0:
            log.info("  priced %d/%d", i, len(rows))
    log.info("priced %d/%d companies", len(priced), len(rows))
    if not priced:
        log.error("no prices fetched (pykrx may be unavailable in this environment)")
        return 1

    df = pd.DataFrame(priced)
    # Data-driven theme layer: map stocks to themes, score each theme by its
    # basket's trailing momentum, attach each stock's hottest-theme strength.
    df = attach_theme_score(df)
    strengths = theme_strength(df)
    hot = hot_themes(strengths)

    weights = FactorWeights(args.w_value, args.w_quality, args.w_momentum, args.w_theme)
    filters = RankFilters(min_market_cap_krw=args.min_mcap * 1e9)
    ranked = rank_universe(df, weights=weights, filters=filters)
    if ranked.empty:
        log.error("no names passed the filters (try a lower --min-mcap)")
        return 1

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    cols = ["rank", "stock_code", "corp_name", "market", "composite",
            "value_score", "quality_score", "momentum_score", "theme_score", "top_theme",
            "pbr", "per", "roe", "debt_to_equity", "mom_12m", "market_cap", "has_earnings", "fiscal_year"]
    ranked[[c for c in cols if c in ranked.columns]].to_csv(args.out, index=False)

    w = weights.normalized()
    print(f"\n=== Korean multi-factor watchlist  "
          f"(value {w.value:.0%} / quality {w.quality:.0%} / momentum {w.momentum:.0%} / theme {w.theme:.0%}) ===")
    print(f"universe after filters: {len(ranked)}   |   min market cap: ₩{args.min_mcap:.0f}B   |   asof {asof}")

    # Hot-themes readout — the data-driven 'what's moving now'
    if hot:
        context = annotate_hot_themes(hot[:6]) if args.llm_context else {}
        print("\nHottest themes now (by basket 12m momentum):")
        for name, strength in hot[:6]:
            line = f"   {name:<10} {strength*100:>+7.1f}%"
            if name in context:
                line += f"   — {context[name]}"
            print(line)
        if args.llm_context and not context:
            print("   (LLM context unavailable — set LLM_PROVIDER + API key in .env)")

    print(f"\n{'#':>3} {'code':>7} {'name':<16} {'mkt':>6} {'score':>6} {'val':>4} {'qual':>4} {'mom':>4} {'thm':>4} "
          f"{'theme':<10} {'PBR':>6} {'PER':>7} {'ROE':>7} {'D/E':>6} {'12m%':>7}")
    print("-" * 122)
    for _, r in ranked.head(args.top).iterrows():
        def f(x, fmt, d="  -  "):
            return format(x, fmt) if pd.notna(x) else d
        name = str(r["corp_name"])[:16]
        thm = (r["theme_score"] * 100) if pd.notna(r.get("theme_score")) else None
        print(f"{int(r['rank']):>3} {r['stock_code']:>7} {name:<16} {str(r['market']):>6} "
              f"{r['composite']:>6.3f} {r['value_score']*100:>3.0f} {r['quality_score']*100:>3.0f} {r['momentum_score']*100:>3.0f} "
              f"{(format(thm,'3.0f') if thm is not None else '  -'):>4} {str(r.get('top_theme',''))[:10]:<10} "
              f"{f(r['pbr'],'6.2f')} {f(r['per'],'7.1f')} {f(r['roe']*100,'6.1f')+'%' if pd.notna(r['roe']) else '   -  '} "
              f"{f(r['debt_to_equity'],'6.2f')} {f(r['mom_12m']*100,'+6.1f')}")
    print(f"\nFull ranked watchlist ({len(ranked)} names) -> {args.out}")
    print("Scores are 0-100 percentiles within the universe. Theme tilt is data-driven (basket momentum);")
    print("--llm-context adds current-events explanations (annotation only, never affects the score).")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
