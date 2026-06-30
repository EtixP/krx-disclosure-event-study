"""tradeforme — Korean equity watchlist dashboard (localhost).

Visual, interactive view of the multi-factor ranker output. Move the weight
sliders to re-rank live, see which themes are hot, explore the value/quality
landscape, and drill into any stock's factor breakdown.

Run:
    .venv/bin/streamlit run src/kdtb/dashboard/app.py

Data: reads data/ranker_watchlist.csv (generate/refresh with
`python scripts/run_ranker.py`).
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

from kdtb.dashboard.reweight import filter_universe, recompute_composite, theme_table

WATCHLIST = Path("data/ranker_watchlist.csv")

st.set_page_config(page_title="tradeforme · KR watchlist", layout="wide", page_icon="📈")


@st.cache_data
def load_watchlist(path: str, mtime: float) -> pd.DataFrame:
    return pd.read_csv(path)


def main() -> None:
    st.title("📈 tradeforme — Korean Equity Watchlist")

    if not WATCHLIST.exists():
        st.error(
            f"No watchlist found at `{WATCHLIST}`.\n\n"
            "Generate it first:\n\n"
            "```\npython scripts/build_fundamentals_cache.py   # one-time\n"
            "python scripts/run_ranker.py\n```"
        )
        st.stop()

    df = load_watchlist(str(WATCHLIST), WATCHLIST.stat().st_mtime)
    st.caption(
        f"{len(df)} stocks · fiscal year {df['fiscal_year'].dropna().mode().iat[0] if 'fiscal_year' in df else '?'} "
        f"· factor scores are 0–100 percentiles within the universe · refresh with `python scripts/run_ranker.py`"
    )

    # ---- sidebar controls ----
    sb = st.sidebar
    sb.header("Factor weights")
    wv = sb.slider("Value (cheap)", 0.0, 1.0, 0.35, 0.05)
    wq = sb.slider("Quality (healthy)", 0.0, 1.0, 0.30, 0.05)
    wm = sb.slider("Momentum (rising)", 0.0, 1.0, 0.15, 0.05)
    wt = sb.slider("Theme (hot sector)", 0.0, 1.0, 0.20, 0.05)
    sb.divider()
    sb.header("Filters")
    min_mcap_b = sb.slider("Min market cap (₩B)", 0, 5000, 100, 50)
    markets = sb.multiselect("Market", sorted(df["market"].dropna().unique()),
                             default=sorted(df["market"].dropna().unique()))
    top_n = sb.slider("Show top N", 10, 100, 30, 5)

    # ---- recompute live ----
    view = filter_universe(df, min_market_cap_krw=min_mcap_b * 1e9, markets=markets)
    if view.empty:
        st.warning("No stocks pass the current filters — lower the market-cap floor.")
        st.stop()
    ranked = recompute_composite(view, wv, wq, wm, wt)

    tot = wv + wq + wm + wt
    norm = f"{wv/tot:.0%} / {wq/tot:.0%} / {wm/tot:.0%} / {wt/tot:.0%}" if tot else "—"
    c1, c2, c3 = st.columns(3)
    c1.metric("Stocks ranked", len(ranked))
    c2.metric("Weights (V/Q/M/T)", norm)
    c3.metric("Themes represented", int((ranked.get("top_theme", pd.Series(dtype=str)).astype(str).str.len() > 0).sum()))

    # ---- hottest themes ----
    themes = theme_table(view)
    if not themes.empty:
        st.subheader("🔥 Hottest themes now")
        st.caption("Median trailing momentum of each theme's members — data-driven, not opinion.")
        fig = px.bar(themes, x="median_mom", y="theme", orientation="h",
                     labels={"median_mom": "median 12m return", "theme": ""},
                     text=themes["median_mom"].map(lambda x: f"{x*100:+.0f}%"))
        fig.update_layout(yaxis=dict(autorange="reversed"), height=max(200, 40 * len(themes)),
                          margin=dict(l=10, r=10, t=10, b=10))
        fig.update_traces(marker_color="#0b3d91")
        st.plotly_chart(fig, use_container_width=True)

    # ---- landscape scatter ----
    st.subheader("🗺️ The landscape — value vs quality")
    st.caption("Top-right = cheap AND healthy. Bubble size = market cap, colour = theme.")
    sc = ranked.copy()
    sc["theme_lbl"] = sc.get("top_theme", "").fillna("").replace("", "(none)")
    fig2 = px.scatter(
        sc, x="value_score", y="quality_score", size="market_cap", color="theme_lbl",
        hover_name="corp_name",
        hover_data={"composite": ":.3f", "pbr": ":.2f", "per": ":.1f", "roe": ":.2%",
                    "mom_12m": ":.1%", "value_score": False, "quality_score": False, "market_cap": False},
        labels={"value_score": "Value percentile", "quality_score": "Quality percentile", "theme_lbl": "Theme"},
        size_max=40,
    )
    fig2.update_layout(height=480, margin=dict(l=10, r=10, t=10, b=10))
    st.plotly_chart(fig2, use_container_width=True)

    # ---- ranked table ----
    st.subheader(f"🏆 Top {top_n}")
    show = ranked.head(top_n).copy()
    show["market_cap_₩B"] = (show["market_cap"] / 1e9).round(0)
    table_cols = ["rank", "stock_code", "corp_name", "market", "composite",
                  "value_score", "quality_score", "momentum_score", "theme_score", "top_theme",
                  "pbr", "per", "roe", "debt_to_equity", "mom_12m", "market_cap_₩B"]
    table_cols = [c for c in table_cols if c in show.columns]
    st.dataframe(
        show[table_cols].style.background_gradient(
            subset=[c for c in ["composite", "value_score", "quality_score", "momentum_score", "theme_score"] if c in show.columns],
            cmap="Greens"
        ).format({"composite": "{:.3f}", "value_score": "{:.2f}", "quality_score": "{:.2f}",
                  "momentum_score": "{:.2f}", "theme_score": "{:.2f}", "pbr": "{:.2f}", "per": "{:.1f}",
                  "roe": "{:.1%}", "debt_to_equity": "{:.2f}", "mom_12m": "{:+.1%}", "market_cap_₩B": "{:,.0f}"},
                na_rep="–"),
        use_container_width=True, height=min(640, 38 * (top_n + 1)),
    )

    # ---- stock detail ----
    st.subheader("🔎 Stock detail")
    names = ranked["corp_name"] + "  (" + ranked["stock_code"].astype(str) + ")"
    pick = st.selectbox("Pick a stock", names.tolist())
    row = ranked.iloc[names.tolist().index(pick)]
    d1, d2 = st.columns([1, 1])
    with d1:
        factors = pd.DataFrame({
            "factor": ["Value", "Quality", "Momentum", "Theme"],
            "percentile": [row.get("value_score"), row.get("quality_score"),
                           row.get("momentum_score"), row.get("theme_score")],
        }).dropna()
        figf = px.bar(factors, x="percentile", y="factor", orientation="h", range_x=[0, 1],
                      text=factors["percentile"].map(lambda x: f"{x*100:.0f}"))
        figf.update_traces(marker_color="#0b3d91")
        figf.update_layout(height=240, margin=dict(l=10, r=10, t=10, b=10), yaxis=dict(autorange="reversed"))
        st.plotly_chart(figf, use_container_width=True)
    with d2:
        def s(x, f, d="–"):
            return format(x, f) if pd.notna(x) else d
        st.markdown(
            f"**{row['corp_name']}** ({row['stock_code']}, {row['market']})  \n"
            f"Composite **{row['composite']:.3f}** · rank **{int(row['rank'])}**  \n"
            f"Theme: **{row.get('top_theme') or '–'}**  \n\n"
            f"| | |\n|---|---|\n"
            f"| PBR | {s(row.get('pbr'), '.2f')} |\n"
            f"| PER | {s(row.get('per'), '.1f')} |\n"
            f"| ROE | {s(row.get('roe')*100, '.1f')+'%' if pd.notna(row.get('roe')) else '–'} |\n"
            f"| Debt/Equity | {s(row.get('debt_to_equity'), '.2f')} |\n"
            f"| 12m return | {s(row.get('mom_12m')*100, '+.1f')+'%' if pd.notna(row.get('mom_12m')) else '–'} |\n"
            f"| Market cap | ₩{s(row.get('market_cap')/1e9, ',.0f')+'B' if pd.notna(row.get('market_cap')) else '–'} |\n"
        )

    st.caption(
        "Factor scores are percentiles within the loaded universe; sliders re-weight them live. "
        "This is a research/education tool, not investment advice."
    )


main()
