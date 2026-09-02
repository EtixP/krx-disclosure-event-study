# Intraday / Execution-Speed Feasibility — what I could determine autonomously

The pre-M0.3 raw-return research found a large event-day reaction that died by
the **T+1 close** (buyback: +1.5% → +0.14%). Broad-market adjustment later
showed the realistic buyback mean is −0.03% abnormal.
The proposed escape hatch was *execution speed* — entering faster than next-day
close, which the user can do via a Korean broker API. This document records
what I could verify without any of the user's credentials or paid data.

## 1. Can we get exact disclosure filing TIMES? — YES, free, including history

| Source | Filing time available? |
|---|---|
| OPEN DART OpenAPI (`list.json`) | **No** — `rcept_dt` is date only (YYYYMMDD); receipt number is a sequence, not a timestamp |
| DART disclosure document (`document.xml`) | **No** — only the date appears in the body |
| DART website daily list (`dsac001/mainAll.do`) | **YES** — renders an `HH:MM` column per row, paired with each `rcpNo`, **for historical dates too** (verified back to 2024) |

→ Built [`src/kdtb/data/disclosure_time_scraper.py`](src/kdtb/data/disclosure_time_scraper.py)
(tested): `scrape_date(d) -> {receipt_no: "HH:MM"}`. We can backfill precise
filing times for the entire 1.2M-row history by scraping DART's daily list and
matching on `receipt_no`. **No paid data, no credentials.**

## 2. When do these disclosures actually publish? — ~half intraday (door is OPEN)

Scraped 5 recent days (2,347 real filing times) and bucketed by KST market hours
(regular session 09:00–15:30):

| Event type | Pre-open | **Market hours (09:00–15:30)** | After close |
|---|---|---|---|
| Supply contracts | 0.0% | **45.5%** | 54.5% |
| **Buybacks** | 0.0% | **71.1%** | 28.9% |
| Bonus issues | 0.0% | 60.0% | 40.0% |
| All disclosures | 0.6% | 48.5% | 50.8% |

This is the pivotal result. For the ~half of supply contracts and **71% of
buybacks that publish during market hours, the same-day close is tradable** —
so the "+1.6% same-day-close" entry we treated as unrealistically idealized is
actually *achievable* for those events. Our uniform "T+1-close = realistic"
assumption was too pessimistic for the intraday-published subset.

## 3. Can we get historical intraday PRICE data? — NO (free); this is the wall

| Source | Intraday price history? |
|---|---|
| pykrx (current free source) | **No** — daily bars only; zero minute/tick functions |
| KIS Open API `inquire-time-itemchartprice` | **No** — same-day (당일) only, 1-min, 30 rows/call; **yesterday and earlier are unavailable** |
| KIS daily-chart endpoints | daily only |
| KRX data products / commercial vendors (Koscom, FnGuide…) | **Yes, but paid** |

→ A multi-year intraday *backtest* cannot be built from free/broker APIs. Two
honest paths exist:
- **Forward collection** (free): each market day, scrape DART filing times +
  capture KIS same-day minute bars; accumulate prospectively. Months to
  build a sample, but $0 and exactly the "online learning" mode.
- **Buy history** (paid): KRX/vendor minute data → immediate backtest.

## 4. The reanalysis that needs NO new data

We already have **daily OHLC** (open/high/low/close) per event, and we can get
**filing times** for free. That's enough to fix the entry assumption per event
*without any minute data*:

- **Intraday-published** event → earliest realistic entry is the **same-day
  close** (you saw it at 11:00, you buy before 15:30). For these, the "+1.6%
  idealized" buyback number is the realistic one.
- **After-close** event → earliest entry is **T+1** (our prior "realistic").

The prior conclusion applied the after-close assumption to *all* events
uniformly. Re-blending by actual publication time is the immediate, free next
experiment.

### Proof-of-concept (recent buyback window, 2025-07 → 2026-06)

Scraped filing times for buyback events in a 12-month window and matched 520 of
864 (the rest lost to transient network failures mid-scrape, not a data
limitation). Re-priced with time-aware entry:

| Entry assumption | n | Mean net / trade | Win % |
|---|---|---|---|
| Old: uniform T+1-close entry | 520 | +0.444% | 46.9% |
| **Time-aware blended** | 520 | **+0.982%** | 49.8% |
| &nbsp;&nbsp;— intraday-published → same-day-close entry | 241 | **+1.698%** | 53.5% |
| &nbsp;&nbsp;— after-close → T+1-close entry | 279 | +0.363% | 46.6% |

Correctly crediting the achievable same-day-close entry for the
intraday-published half **roughly doubles** the blended realistic edge
(+0.44% → +0.98%/trade), and the intraday subset shows +1.70%/trade with a
>50% win rate.

**This is a promising LEAD, not a validated edge.** It carries the exact
caveats that sank earlier buyback claims: it is a single recent 12-month
window, with **no walk-forward and no adversarial / selection-lift gate**, on a
60%-matched sample. Two prior adversarial passes refuted buyback at this same
stage. Before it means anything it must clear: (1) full-history time backfill,
(2) time-aware walk-forward across all folds, (3) the selection-lift +
adversarial-verification gate already built into the project. The honest status
is "the most promising lead the project has produced, pending the same
validation that killed the others."

Raw POC log: [`data/intraday_poc_buyback.log`](data/intraday_poc_buyback.log).

## Full-history validation + adversarial verdict

Backfilled filing times for the full buyback history (534,285 disclosures
timestamped via the resumable scraper) and ran the time-aware walk-forward
across **all 10 half-year folds (2021H2–2026H1, 3,562 events, 74% coverage)**.
Then put the result through a 4-skeptic adversarial workflow
([`scripts/run_intraday_walkforward.py`](scripts/run_intraday_walkforward.py),
snapshot [`data/intraday_buyback_skeptics_2026-06-30.json`](data/intraday_buyback_skeptics_2026-06-30.json)).

M0.2 re-priced every row using its exact exit date and market. This lowers both
absolute entry variants by about 1.56bps but leaves the entry-timing delta
unchanged, because uniform and time-aware entries share the same exit cost.

M0.3 then subtracts KOSPI/KOSDAQ over the exact dates of each entry variant and
the common T+5 exit. Unlike transaction costs, the market adjustment differs
between t0 and T+1 entries, so it can change the timing delta itself.

| | Raw mean | Raw WF+ | Abnormal mean | Abnormal WF+ |
|---|---:|---:|---:|---:|
| Uniform T+1 entry (old assumption) | +0.141% | 7/10 | −0.093% | 5/10 |
| **Time-aware entry** | **+0.458%** | 9/10 | **+0.164%** | 7/10 |
| **Entry-timing delta** | **+0.317%** | **10/10** | **+0.257%** | **9/10** |
| Learned selector lift | +0.068% | — | −0.087% | — |

The pre-M0.3 verdict was `real_delta_untradable_level`. The current status is a
**provisional positive timing effect, not abnormal-return alpha and not a
deployable strategy**. Coverage remains clean, but market attribution refutes
the learned-selection claim and weakens fold stability.

**What remains (keep it cautiously):** The raw +0.317% timing delta is positive
in 10/10 folds. After removing broad-market movement, it is +0.257% and positive
in 9/10; 2026H1 is slightly negative. The mechanism remains the event-day-close
→ next-close gap captured by buying one session earlier, but the earlier claim
of regime independence no longer holds. The learned selector has −0.087%
abnormal lift, so there is no ML selection edge.

**Why it is not tradable today:**
1. **Positive-skew lottery, not a robust mean.** Median trade *loses*
   (−0.062%), win rate 49.4%; removing the top 5% of trades flips the mean to
   **−0.43%**. The PnL lives in extreme up-moves — the hardest names to fill.
2. **Fill-fragile at exactly the required fill.** The median capturable
   overnight gap is **~20bps** — smaller than one KOSDAQ small-cap tick — and
   concentrates in low-price/KOSDAQ names where transacting at the closing
   auction (단일가) without moving it is least realistic. +30bps extra entry
   slippage halves the delta and flips a fold.
3. **Capacity is decisive.** Under the project's own `max_open_positions=1`
   with a 5-day hold, only **~6% of signals (~41/year)** are reachable;
   The pre-M0.3 raw-return Monte Carlo over tie-breaks gave **~+0.25%/trade**
   (55% of paths below the +0.30% bar), and at the ₩30,000 order cap the
   economics were only **~$2–9/year** even before benchmark adjustment.

## Bottom line

- **Filing times: solved, free, historical** — scraper built, tested, and the
  full buyback history is backfilled.
- **The entry-timing effect remains positive but provisional** — market
  adjustment reduces it to +0.257% and 9/10 positive fold deltas. It is a
  deterministic chronology effect, not validated stock-selection alpha.
- **It is not deployable at retail scale** — capacity (1-position cap), fill
  fragility at the closing auction, and fat-tail dependence reduce even the
  +0.164% abnormal headline further. The historical daily-close data **cannot** answer the
  one decisive question: whether the ~20bps median gap is actually fillable at
  `t0_close` in the low-price/KOSDAQ names that carry it.
- **The only way to resolve it** is forward, live: collect real closing-auction
  fill prices for upcoming buyback events (KIS API, paper mode, `max_open_positions=1`,
  with a KOSPI-only / min-price filter to test the fill-fragility hypothesis).
  That is Milestone 6/7 territory and needs the user's KIS credentials — and it
  is now a *targeted* test of one specific assumption, not an open-ended search.
