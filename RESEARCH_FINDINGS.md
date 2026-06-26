# Research Findings — tradeforme

This document is the **primary research deliverable** of the project: a
rigorous, intellectually honest answer to the question CLAUDE.md set out
to test, plus the methodology that produced the answer.

**Research question (from CLAUDE.md):** do selected Korean disclosure
event categories — major supply contracts, share buybacks, dilutive
financing, halts, etc. — produce a tradable post-disclosure price
drift after realistic transaction costs?

**Answer:** No. Across 7 categories tested on 4.5 years of data
(28,728 event-study rows, 21 adversarial verifications), zero
categories are tradable long after realistic T+1-close execution.
One category — **shareholder_change** — is a well-supported negative
signal, useful as a long-side risk filter even though Korean retail
accounts can't easily short to monetize it directly.

## Final per-category verdict (5-year sample)

| Category | n | Idealized T+5 net | Realistic T+5 net | Walk-forward + | Lenses survive | Verdict |
|---|---|---|---|---|---|---|
| supply_contract | 8,585 | −0.11% | −0.29% | 5/11 | 3/3 | neutral (reversed from 2yr) |
| **buyback** | 4,801 | **+1.20%** | **+0.16%** | **10/11** | 2/3 | **idealized only — below tradability threshold** |
| bonus_issue | 828 | +1.23% | +0.23% | 7/10 | 1/3 | regime-dependent noise |
| rights_offering | 5,911 | +0.40% | +0.25% | 6/11 | 3/3 | neutral |
| convertible_bond | 4,175 | +0.63% | −0.02% | 7/11 | 3/3 | neutral |
| halt_resumption | 2,570 | −1.18% | +0.05% | 4/11 | 3/3 | neutral (negative-signal claim refuted) |
| **shareholder_change** | **1,858** | **−1.22%** | **−1.09%** | **2/10** | **3/3** | **negative_robust → blacklist** |

Roundtrip cost: **0.313%** (0.015% commission × 2 + 10% VAT + 0.18% sale
tax + 5bps slippage), per [`backtest/cost_model.py`](src/kdtb/backtest/cost_model.py).

## How the methodology evolved

### Phase 1 — Bulk pattern discovery (loops 1–4)

Built the deterministic ingest → parse → strategy pipeline. The initial
3-month sample (533 events) showed an apparent +3.10% T+5 edge at ratio
≥ 0.30 — exciting but small-n.

**Lesson 0**: any single-window backtest on small n can produce a
"discovery" that won't replicate.

### Phase 2 — Scale-out (loops 5–6)

Extended to 24 months (3,531 supply-contract events). The 0.30+ "edge"
**collapsed from +3.10% to +0.50%** with PF 1.93 → 1.21. Walk-forward
across 4 windows revealed regime variance.

But subgroup analysis surfaced two real patterns: **KOSPI > KOSDAQ**
(3/4 windows positive) and **government counterparty → consistently
negative** (3/4 windows from −0.5% to −5%). Both walk-forward validated.

**Lesson 1**: walk-forward across non-overlapping periods is the only
sanity check that distinguishes signal from sampling artifact.

### Phase 3 — Strategy codification + execution realism (loops 7–9)

The KOSPI-only + skip-government v2 strategy aggregated to +0.67% T+5
net mean, PF 1.27, 3/4 walk-forward windows positive — looked publishable.

Then loop 9 added the realistic-execution test: buy at T+1 close (the
earliest realistic entry for a retail trader, not the event-day close
the event study assumes). The +0.67% **collapsed to +0.14% per trade**.
PF 1.27 → 1.05.

**Lesson 2**: the gap between "event-study return" (immediate
event-close fill) and "realistic execution return" (T+1 close entry) is
the single biggest determinant of tradability. Verdicts framed only on
idealized numbers are misleading.

### Phase 4 — Cross-category meta-analysis (loop 10)

Tested 6 additional event types. Adversarial 3-lens verification
(sample_size, execution_realism, regime_stability). Zero categories
passed strict tradable-edge criteria. Bonus_issue (n=273) looked best
on aggregate (+1.89% realistic) but failed sample_size and
regime_stability lenses.

**Lesson 3**: adversarial verification with multiple independent
lenses catches over-claiming that a single-pass analysis misses.

### Phase 5 — Methodology self-correction (loops 11–12)

Loop 11 implemented the recommended halt_resumption + shareholder_change
blacklists in the risk engine.

Loop 12 re-validated on 4.5 years (38 months added):

- **bonus_issue (iter 1)**: confirmed as noise. Mean +3.94% → +1.23%,
  median +1.69% → **−0.47%** (flipped sign).
- **halt_resumption (iter 2)**: REFUTED on all 3 lenses on extended
  data. Idealized −1.18% became realistic +0.05% (slippage artifact).
  **Removed from blacklist.** Adversarial verification self-corrected
  a flaw introduced just one loop earlier.
- **shareholder_change (iter 2)**: held up. Realistic −1.09%, 8/10
  walk-forward windows negative. **Lookback extended to 60 days**.
- **5yr meta (iter 3)**: comprehensive 7-category re-test. Buyback
  weakened to realistic +0.16% (below tradability). Supply_contract
  REVERSED sign. Confirmed shareholder_change as the only blacklist
  survivor.

**Lesson 4**: when a system has the discipline to re-test its own
intermediate conclusions on larger data, it catches recency-bias
artifacts that would otherwise propagate. The 12-loop arc shows
multiple intermediate "discoveries" that didn't survive scale.

## What is well-supported

1. **The 0.313% roundtrip cost figure** for Korean equities is right and
   the single biggest filter on what looks tradable on paper.
2. **shareholder_change is consistently negative** (n=1,858, 8 of 10
   walk-forward windows negative, both KOSPI and KOSDAQ below PF 0.80,
   four most-recent half-years all strongly negative). Used as a
   long-side blacklist with 60-day lookback in
   [`risk/event_blacklist.py`](src/kdtb/risk/event_blacklist.py).
3. **The deterministic parser** extracts contract value + prior-year
   revenue from the standard 단일판매ㆍ공급계약체결 form with **96.6%
   success** across 3,531 events. Both voluntary-disclosure and
   mandatory-disclosure variants supported. Sub-categories of
   "value undisclosed" (literal dash) correctly distinguished from
   "missing field" via the parser's red-flag system.

## What is decisively not supported

1. **No category is tradable long after realistic execution costs**, in
   24-month or 4.5-year data, before or after adversarial verification.
2. **The original 30%+ ratio "edge"** from the early small-sample work
   was sampling noise.
3. **halt_resumption** is not a usable negative signal on extended
   data — the umbrella category mixes structurally different
   sub-events (bonus-issue halts are positive, rumor halts are
   negative); the realistic-execution mean is essentially zero.
4. **bonus_issue** looked like the best survivor on 2-year data but
   collapsed to noise on 5-year data, with median flipping negative.

## Three productive directions if pursued further

Listed in increasing order of effort and uncertainty:

1. **Sub-segment buyback** by liquidity (avg daily volume) and market
   cap. The KOSPI-only buyback subset has the most robust idealized
   pattern. A narrower filter (e.g. mid-cap only, top 30% of recent
   volume) might lift realistic returns above the +0.30% threshold.
2. **Test untested event types**: earnings surprise proxy, large M&A,
   delisting/relisting. CLAUDE.md mentioned these but the work was
   never done. The methodology infrastructure (event-study runner,
   analyzer, walk-forward, adversarial workflow) handles any new
   category with a one-line SQL fragment addition to
   [`data/event_categories.py`](src/kdtb/data/event_categories.py).
3. **Faster-than-T+1 execution**. The data is unambiguous: any
   tradable edge in these events lives in the event-day reaction. A
   retail trader using next-day-close fills will not capture it.
   CLAUDE.md explicitly excludes HFT-level infrastructure, but a
   broker-API event subscription that reacts within seconds of DART
   publication is the only realistic path to capturing the buyback
   idealized edge.

## Reproducibility

Every result in this document is reproducible from the committed code:

```bash
# Verify the dataset
sqlite3 data/kdtb.db "SELECT COUNT(*), MIN(DATE(receipt_datetime)), MAX(DATE(receipt_datetime)) FROM disclosures"
# Expected: 1119270, 2021-12-29, 2026-06-24

# Reproduce per-category analysis
.venv/bin/python scripts/analyze_event_category.py --category buyback

# Reproduce 4-window walk-forward of v0/v1/v2
.venv/bin/python scripts/walk_forward.py

# Reproduce realistic-execution backtest
.venv/bin/python scripts/run_paper_backtest.py

# Reproduce daily monitor on any past date
.venv/bin/python scripts/run_daily_monitor.py --date 2026-05-15 --no-ingest
```

The workflow outputs that drove the synthesis (JSON, agent-level detail)
are committed to [`data/`](data/):

- `multi_event_meta_2026-06-26.json` — Loop 10 (2yr meta-analysis)
- `blacklist_robustness_5yr_2026-06-26.json` — Loop 12 iter 2
- `multi_event_meta_5yr_2026-06-26.json` — Loop 12 iter 3 (5yr meta)
- `daily_monitor_review_2026-06-26.json` — Loop 12 iter 4 (adversarial code review)

## Acknowledgments

This project intentionally produced a strong negative result. CLAUDE.md
explicitly said:

> "The project must remain intellectually honest: the edge may not
> exist. If backtesting and live paper trading show no edge after
> costs, that is still a valid and impressive result."

> "A strong negative result is still valuable if it shows: the market
> reacts too quickly; transaction costs eliminate gross edge; LLM
> extraction does not improve over deterministic parsing; only certain
> event categories are promising."

Three of those four (efficient pricing, costs eliminating edge, no
useful event category from the seven tested) are exactly what the data
showed. The fourth (deterministic vs LLM) was sidestepped because the
deterministic parser hit 96.6% extraction success — the LLM was never
needed to clear the bar.
