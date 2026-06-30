# tradeforme

Event-driven Korean equity disclosure-trading **research** system. Monitors
official corporate disclosures via OPEN DART, extracts structured event
features deterministically (and optionally via LLM), evaluates candidate
trades through walk-forward-validated rules + an adversarially-verified
risk engine, and exposes a one-command daily monitor.

> **Research deliverable, not a profitable trading system.**
> Per the 5-year empirical analysis ([RESEARCH_FINDINGS.md](RESEARCH_FINDINGS.md)),
> **no Korean disclosure event category** tested here is tradable long after
> realistic T+1-close execution costs. The strongest candidate (buyback) has
> 10/11 walk-forward windows positive but realistic mean is only +0.16% —
> below the +0.30% tradability threshold. **Shareholder change** is the one
> well-supported negative signal (used as a long-side blacklist in the risk
> engine). The methodology — deterministic pipeline + walk-forward +
> adversarial verification — is the project's primary value.

## What's in the repo

| | |
|---|---|
| **Data scale** | 1.21M DART disclosures, 5 years (Jun 2021 – Jun 2026), 28,979 event-study rows across 7 categories |
| **Tests** | 118 passing, no regressions across 12 loops |
| **Coverage** | M1 skeleton + M2 DART ingest + M3 deterministic extraction (96.6% success) + M4 event study + cost model + M5 strategy engine + M5b risk-engine blacklist + paper-broker scaffolding |
| **Not built** | Live broker integration (M6/M8), real LLM calls (M3 client is scaffolded but no key consumed), Streamlit dashboard (M9) |

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
cp .env.example .env             # add DART_API_KEY at minimum
pytest                            # should be 118/118
```

## Daily usage

```bash
.venv/bin/python scripts/run_daily_monitor.py
```

Ingests today's DART disclosures, parses any new supply contracts, runs the
v2 strategy (KOSPI-only, skip-government, ratio ≥ 0.08), applies the
shareholder-change blacklist (60-day lookback), and prints the candidate
table. **Does not place any orders.**

Example output (2026-06-26 live):

```text
=== Candidate signals ===
 stock  market corp               ratio    value(원)         cp_type  strength  status
082740   KOSPI 한화엔진              0.102 139,297,977,900 large_corp_korean    0.51  APPROVED

=== Negative events today (will block long signals on these stocks for 60d) ===
 340930 유일에너테크          최대주주변경을수반하는주식양수도계약해제ㆍ취소등
 214150 클래시스              최대주주변경을수반하는주식담보제공계약체결
 215480 토박스코리아          최대주주변경을수반하는주식양수도계약체결

Summary: 1 candidate signal(s), 1 approved, 0 blocked.
```

## Reproducing the analyses

```bash
# Bulk ingest a date range
for i in $(seq 1 90); do
  d=$(date -v-${i}d -j +%Y-%m-%d)
  .venv/bin/python scripts/ingest_disclosures.py --date $d
done

# Parse supply contracts (deterministic regex parser)
.venv/bin/python scripts/parse_supply_contracts.py [--limit N] [--skip-existing]

# Event study for any category
.venv/bin/python scripts/run_event_study.py --category buyback

# Per-category analysis (aggregate, walk-forward, realistic execution)
.venv/bin/python scripts/analyze_event_category.py --category buyback

# Cross-category comparison table
.venv/bin/python -m scripts.summarize_all_categories

# v2 strategy walk-forward (4 six-month windows)
.venv/bin/python scripts/walk_forward.py

# Realistic-execution paper backtest
.venv/bin/python scripts/run_paper_backtest.py

# Backup the SQLite DB
./scripts/backup_db.sh
```

Supported event categories (`--category` flag):
`supply_contract`, `buyback`, `bonus_issue`, `rights_offering`,
`convertible_bond`, `halt_resumption`, `shareholder_change`.

## Learning paper-trader

An offline self-improving paper-trader lives in [src/kdtb/learning/](src/kdtb/learning/).
It learns from historical mock trades (enter T+1 close, exit T+5 close, minus
cost) and only adopts a new model version when a **challenger beats the
incumbent champion on a validation fold it did not train on** — the honest
version of "tries mock trades, learns, makes better choices."

```bash
.venv/bin/python scripts/train_learner.py --synthetic-edge      # sanity: machine learns planted edge
.venv/bin/python scripts/train_learner.py --category buyback     # real data
```

Two independent claims, kept separate:

- **The machine works** — TRUE. Leak-free (training data always strictly
  precedes the test fold, enforced structurally + tested), seed-stable, and on
  planted-edge synthetic data it learns and earns **+2.02%/trade** with a
  **+2.16% selection lift** over trade-everything.
- **Any Korean category is tradable** — FALSE. On real data the machine
  correctly finds **no selective edge**. Buyback looked positive
  (+0.30%/trade) but adversarial verification showed it was a baseline-mismatch
  + regime artifact: on matched periods, naive trade-everything (+0.315%)
  *beats* the model, and within-fold stock selection is **negative**
  (−0.017%/trade). The tool now reports a **selection-lift** metric that
  isolates skill from regime, so it can't make that mistake again.

This is the whole point: a learning system can only optimize edge that
exists. Pointed at near-efficient data, the honest outcome is "learns to
abstain / no selective edge" — which is exactly what it reports. See
[RESEARCH_FINDINGS.md](RESEARCH_FINDINGS.md#the-learning-paper-trader).

## Multi-factor stock ranker (watchlist tool)

A longer-horizon screener in [src/kdtb/ranker/](src/kdtb/ranker/) that ranks
KOSPI/KOSDAQ stocks by a composite of three economically-grounded factor groups
and prints an explainable watchlist. Unlike the event-driven work, this targets
factor premia that play out over *months*, so the capacity/fill problems that
sink fast strategies don't apply.

```bash
# one-time: cache DART fundamentals for the universe (resumable)
.venv/bin/python scripts/build_fundamentals_cache.py            # or --limit N

# run the ranker (fetches live prices + momentum, prints the watchlist)
.venv/bin/python scripts/run_ranker.py --top 30 --min-mcap 100
.venv/bin/python scripts/run_ranker.py --w-value 0.5 --w-quality 0.3 --w-momentum 0.2
```

| Group | Means | Built from |
|---|---|---|
| **Value** | cheap | book yield (1/PBR) + earnings yield (1/PER); losses → worst |
| **Quality** | healthy | ROE + low debt-to-equity |
| **Momentum** | rising | trailing 12-month return |
| **Theme** | in a hot theme | trailing momentum of the stock's market *theme* basket (defense, nuclear, batteries, shipbuilding, AI, bio, …) |

Each factor is standardized by **cross-sectional percentile rank** (0–100,
robust to the fat tails of valuation ratios), groups are averaged, then combined
with configurable weights. Every name shows *why* it ranks where it does, and a
**data-driven "hottest themes now" readout** surfaces which sectors are moving:

```text
=== Korean multi-factor watchlist  (value 35% / quality 30% / momentum 15% / theme 20%) ===

Hottest themes now (by basket 12m momentum):
   반도체     +442.4%      조선  +131.6%     2차전지  +94.1%     방산  +48.2%

  #    code name        mkt  score  val qual  mom  thm theme       PBR    PER    ROE    D/E   12m%
  1  004800 효성        KOSPI  0.765  69  77  91   -            1.04    7.0  14.8%  0.89 +212.7
  5  000270 기아        KOSPI  0.727  71  79  70  67 자동차       0.88    7.2  12.3%  0.62  +57.2
 16  000660 SK하이닉스   KOSPI  0.695  21  93 100  97 반도체      16.17   45.4  35.6%  0.46 +1220
```

### Current-events / theme layer

The user asked for valuations that reflect current sociopolitical events. The
honest, measurable way to do that: **"which themes are hot" is read from the
real price momentum of each theme's stock basket** — a fact, not an opinion.
A stock inherits its hottest theme's strength as a bounded, transparent tilt
(default 20% weight, `--w-theme`), so a fundamentally-sound *laggard in a hot
theme* gets surfaced. Set `--w-theme 0` for pure fundamentals.

An optional LLM layer (`--llm-context`) **annotates** each hot theme with a
one-line current-events explanation. Per the project's design rule, the LLM only
*explains* — it never changes a score; the tilt stays 100% data-driven.

You don't have to pay for this. Set `LLM_PROVIDER` in `.env` to one of:
- `ollama` — **free, local, no key** (install [Ollama](https://ollama.com),
  `ollama pull qwen2.5`); the default model is `qwen2.5`
- `anthropic` / `openai` — paid APIs (both require a key; there is **no free
  Claude API**), set `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`

(Theme membership is currently a curated ticker map; an LLM also lets it extend
theme classification to the full universe — a natural next enhancement.)

## Visual dashboard (localhost)

A Streamlit dashboard makes the watchlist explorable in the browser:

```bash
python scripts/run_ranker.py                       # refresh the data
.venv/bin/streamlit run src/kdtb/dashboard/app.py  # opens http://localhost:8501
```

It gives you, interactively:
- **Live weight sliders** — drag value / quality / momentum / theme and the
  ranking re-sorts instantly (re-weighting cached percentiles, no re-fetch).
- **Hottest-themes bar chart** — each theme's median basket momentum.
- **Value-vs-quality landscape** — a scatter of the whole universe (bubble =
  market cap, colour = theme); top-right is cheap *and* healthy.
- **Ranked table** with colour-graded factor scores.
- **Per-stock drill-down** — a factor-percentile bar + the raw PBR/PER/ROE/
  debt/momentum for any name you pick.

Powered by `data/ranker_watchlist.csv`; re-run `run_ranker.py` to refresh prices.

Data: **DART financial statements + shares** (equity, net income, debt, common
shares) and **per-ticker prices** (pykrx) — chosen because the bulk KRX
fundamentals endpoints are unreliable; DART is the authoritative source. The
universe is the 2,661 KOSPI/KOSDAQ companies in the local disclosures table.

**Honest caveats.** (1) This ranks by sound, economically-motivated factors —
it does **not** yet prove those factors *outperform in Korea after costs*; that
backtest is a separate, deliberate next step. (2) Financials/holding companies
have structurally high debt-to-equity, so the quality factor penalizes them —
consider excluding financials or sector-neutral scoring. (3) Momentum and PBR/PER
depend on your live price feed.

## Architecture

```
DART list.json → Disclosure ↘
                              SQLite (data/kdtb.db)
DART document.xml → Parser → Extraction ↗

Extraction + Disclosure → Strategy → Signal → Risk engine (+ blacklist) → Decision
```

The **strategy** layer is deterministic Python (no ML in the order-placing
path). The **risk engine** consults the `EventBlacklist` to reject longs
when a blacklisted negative event has occurred for the same stock within
the lookback window.

See [CLAUDE.md](CLAUDE.md) for the full design spec and milestone definitions.
See [NEXT_STEPS.pdf](NEXT_STEPS.pdf) for the running action log and the
unresolved decisions waiting on the user.

## Warnings

- Experimental software. Not financial advice. May lose money if you ignore the
  research findings and trade anyway.
- Default `trading.mode: PAPER` in [config/default.yaml](config/default.yaml).
  Live trading requires both `allow_live_orders: true` in YAML AND
  `ENABLE_LIVE_TRADING=true` in env (see
  [src/kdtb/config.py](src/kdtb/config.py) for the guard).
- Korean Securities Transaction Tax (0.18% on sale) plus broker commission
  and slippage adds ~0.31% roundtrip drag — built into
  [src/kdtb/backtest/cost_model.py](src/kdtb/backtest/cost_model.py).
