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
