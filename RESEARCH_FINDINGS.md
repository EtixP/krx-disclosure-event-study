# Research Findings

This document is the **primary research deliverable** of the project: a
rigorous, intellectually honest answer to the question DESIGN.md set out
to test, plus the methodology that produced the answer.

**Research question (from DESIGN.md):** do selected Korean disclosure
event categories — major supply contracts, share buybacks, dilutive
financing, halts, etc. — produce a tradable post-disclosure price
drift after realistic transaction costs?

**Answer:** No. Across 7 categories tested on 5 years of data
(28,728 scored event rows, 21 adversarial verifications), zero
categories are tradable long after realistic T+1-close execution.
One category — **shareholder_change** — is a well-supported negative
signal, useful as a long-side risk filter even though Korean retail
accounts can't easily short to monetize it directly.

## Final per-category verdict (5-year sample)

| Category | n | Idealized T+5 net | Realistic T+5 net | Walk-forward + | Lenses survive | Verdict |
|---|---|---|---|---|---|---|
| supply_contract | 8,585 | −0.11% | −0.29% | 6/11 | 3/3 | neutral (reversed from 2yr) |
| **buyback** | 4,801 | **+1.20%** | **+0.16%** | **10/10** | 2/3 | **idealized only — below tradability threshold** |
| bonus_issue | 828 | +1.23% | +0.23% | 7/10 | 1/3 | regime-dependent noise |
| rights_offering | 5,911 | +0.40% | +0.25% | 6/11 | 3/3 | neutral |
| convertible_bond | 4,175 | +0.63% | −0.02% | 7/11 | 3/3 | neutral |
| halt_resumption | 2,570 | −1.18% | +0.05% | 4/10 | 3/3 | neutral (negative-signal claim refuted) |
| **shareholder_change** | **1,858** | **−1.22%** | **−1.09%** | **2/10** | **3/3** | **negative_robust → blacklist** |

Roundtrip cost: **0.313%** (0.015% commission × 2 + 10% VAT + 0.18% sale
tax + 5bps slippage), per [`backtest/cost_model.py`](src/kdtb/backtest/cost_model.py).
The **+0.30% tradability bar** is derived in that same file
([`TRADABILITY_BAR_PCT`](src/kdtb/backtest/cost_model.py)): post-cost returns
must clear roughly one further roundtrip, so a category still profits if the
true all-in cost is about double what we model.

**Walk-forward column, corrected 2026-08-30.** "Walk-forward +" is *windows
positive / windows **scored***. A half-year window with fewer than 5 events is
not scored — its mean is noise — and is excluded from the denominator as well as
the numerator. Three rows in an earlier version of this table applied that rule
inconsistently: buyback read 10/11 and halt_resumption 4/11, both counting a
skipped 3–4 event window from Jan–Jun 2021 in the denominator only, and
supply_contract read 5/11 against an actual 6 positive windows. The underlying
data never changed (identical n in every row); only these counts were wrong. The
table now matches `python -m scripts.summarize_all_categories` exactly, and the
script prints the scored/skipped split so the denominator can no longer be
ambiguous. No verdict changes: buyback going 10/11 → 10/10 makes it *cleaner*,
and it still fails on realistic mean (+0.16%), which is what the verdict rests
on.

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

Loop 12 re-validated on 5 years (38 months added):

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
   scored walk-forward windows negative, both KOSPI and KOSDAQ below PF 0.80,
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
   delisting/relisting. DESIGN.md mentioned these but the work was
   never done. The methodology infrastructure (event-study runner,
   analyzer, walk-forward, adversarial workflow) handles any new
   category with a one-line SQL fragment addition to
   [`data/event_categories.py`](src/kdtb/data/event_categories.py).
3. **Faster-than-T+1 execution**. The data is unambiguous: any
   tradable edge in these events lives in the event-day reaction. A
   retail trader using next-day-close fills will not capture it.
   DESIGN.md explicitly excludes HFT-level infrastructure, but a
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
.venv/bin/python scripts/check_pipeline_health.py --date 2026-05-15 --no-ingest
```

The workflow outputs that drove the synthesis (JSON, agent-level detail)
are committed to [`data/`](data/):

- `multi_event_meta_2026-06-26.json` — Loop 10 (2yr meta-analysis)
- `blacklist_robustness_5yr_2026-06-26.json` — Loop 12 iter 2
- `multi_event_meta_5yr_2026-06-26.json` — Loop 12 iter 3 (5yr meta)
- `daily_monitor_review_2026-06-26.json` — Loop 12 iter 4 (adversarial code review)

## The learning paper-trader

After the research phase established that no category has a tradable long-side
edge, a second deliverable was built: a **self-improving paper-trader** that
learns from historical mock trades — the thing the project was originally
imagined to be ([src/kdtb/learning/](src/kdtb/learning/)).

**Design.** Each event is a mock trade (enter T+1 close, exit T+5 close, minus
the 0.313% cost). A gradient-boosted classifier predicts P(net > 0) from
decision-time features (ratio, market, counterparty, contract size, calendar)
and trades when its PnL-optimal threshold is cleared. The learning loop is a
**champion/challenger walk-forward**: each half-year fold, a challenger trains
only on strictly-earlier folds, both models are scored on a held-out
validation fold, and the challenger is promoted only if it wins. The champion
starts as "never trade." There is **no look-ahead** — enforced structurally
and asserted in tests.

**Two independent claims.**

1. *The machine works* — **TRUE**, a software/methodology result. It is
   leak-free and seed-stable, and on a planted-edge synthetic dataset it
   learns and earns **+2.02%/trade** with a **+2.16% selection lift** over
   trade-everything. The synthetic path (`--synthetic-edge`) is the proof the
   machine *can* learn when edge exists.

2. *Korean disclosure events are tradable* — **FALSE**, the edge claim. On
   real data the machine correctly finds no selective edge:
   - **supply_contract**: traded only 2 of 9 folds (the recent regime) →
     *insufficient breadth*, the same recency artifact found in the research
     phase.
   - **buyback**: looked positive (+0.299%/trade, 5/9 folds) and the first-cut
     verdict over-credited it. Three adversarial reviewers refuted it. The
     "+0.299% beats +0.174%" comparison was a **baseline mismatch**: the model
     was measured only over its favorable 2024–2026 folds, while always-trade
     was diluted across all 9 folds including the bad early ones it skipped. On
     a **matched same-period basis, trade-everything returns +0.315% and beats
     the model's +0.299%** — the within-fold stock-selection component is
     **negative (−0.017%/trade)**, statistically insignificant (per-fold paired
     t = 0.78), and the abstention in early folds was a mechanical walk-forward
     warmup artifact, not a prediction. The underlying whole-sample buyback
     return is +0.157%/trade, matching the earlier +0.16% finding and below the
     +0.30% tradability bar.

**The fix that came out of it.** The reviewers caught a flaw in the tool's own
verdict logic (the all-folds vs matched-folds baseline mismatch). The tool now
computes and reports a **selection-lift** metric — model per-trade return minus
trade-everything on the *same* folds — which is the only number that isolates
genuine stock-selection skill from fold-timing/regime beta. With that metric,
the buyback verdict correctly reads "NO SELECTION EDGE" and the synthetic case
correctly reads "POSITIVE SELECTIVE EDGE."

**The lesson.** A learning system can only optimize edge that already exists in
the data; it cannot manufacture edge from noise. Pointed at near-efficient
Korean disclosure data, the honest learner abstains or finds no selective edge.
The value delivered is the **machine** (reusable on any dataset) plus the
**discipline baked into its verdict** (breadth + selection-lift + adversarial
verification) that stops it from reporting regime beta as skill.

Adversarial-verification snapshot:
[`data/buyback_learner_skeptics_2026-06-29.json`](data/buyback_learner_skeptics_2026-06-29.json).

## Intraday execution speed — the one real positive effect

The research and learning phases all assumed next-day-close (T+1) entry, an
artifact of the free daily-bar data, not the user's actual capability (they can
trade intraday via a Korean broker API). Investigating that gap produced the
project's first robust positive finding — and, on honest examination, the
reason it still isn't tradable. Full detail in
[INTRADAY_FEASIBILITY.md](INTRADAY_FEASIBILITY.md).

**What was found.** DART exposes exact filing *times* on its website (not in the
API) for historical dates; a resumable scraper backfilled them for the full
buyback history (534k disclosures timestamped). ~46–71% of these events publish
*during* market hours, so for them the same-day close is a realistic entry — not
the T+1 close we had assumed. Assigning each event its earliest tradable entry
(same-day close if published intraday, else T+1) and walking forward across all
10 half-year folds:

| | Mean net / trade | Folds positive |
|---|---|---|
| Uniform T+1 (old) | +0.157% | 7/10 |
| Time-aware | +0.473% | 9/10 |
| **Entry-timing delta** | **+0.317%** | **10/10** |

The delta is a **real, deterministic, regime-independent effect**: positive in
every fold (including the negative-base 2021–2022 regime), stable across entry
cutoffs (fold t = 7.6), mechanism cleanly identified (the event-day-close →
next-close overnight gap), coverage unbiased (p = 0.99), and a learned selector
adds nothing (+0.06%) — it is an entry-timing rule, not stock selection. This is
the first thing in the project that survived walk-forward *and* adversarial
verification as a genuine effect.

**Why it still isn't tradable.** A 4-skeptic adversarial pass (3 of 4 refuted)
showed the absolute level collapses under real constraints: the median trade
loses (−0.044%, 49.6% win) and removing the top 5% of trades flips the mean to
−0.42% (positive-skew lottery); the median capturable gap is ~20bps, smaller
than one KOSDAQ tick and concentrated in the low-price names hardest to fill at
the closing auction; and under `max_open_positions=1` with a 5-day hold only ~6%
of signals are reachable, giving a capacity-realistic ~+0.25%/trade and ~$2–9/yr
at the ₩30k cap. Verdict: **`real_delta_untradable_level`** — keep the
entry-timing insight as a documented edge; do not deploy it without a
capacity-aware, closing-auction-fill-realistic *forward* test, the one thing no
historical daily-close dataset can answer.

**The lesson.** Even the project's single robust positive effect dissolved into
"not deployable at retail scale" once capacity and fill realism were modeled —
the same efficiency wall, now met one layer deeper. The honest output is a
precisely characterized effect plus the exact, narrow experiment that could
still validate it: live closing-auction fills via the broker API.

## Acknowledgments

This project intentionally produced a strong negative result. DESIGN.md
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
