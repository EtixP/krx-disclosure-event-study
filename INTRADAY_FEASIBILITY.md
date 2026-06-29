# Intraday / Execution-Speed Feasibility — what I could determine autonomously

The research phase established that the disclosure edge is real at the
**event-day close** but dies by the **T+1 close** (buyback: +1.6% → +0.16%).
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

## Bottom line

- **Filing times: solved, free, historical** — scraper built and tested.
- **The door is open**: a large fraction of the tradable events publish during
  market hours, so same-day-close entry (where the edge still exists) is real.
- **The wall is historical intraday *prices*** — free sources are daily-only;
  testing true minute-level entry requires either paid KRX/vendor data or
  forward collection via the broker API.
- **Immediate free win**: re-blend the existing daily-OHLC event study by actual
  filing time. If the intraday-published subset retains the same-day-close edge
  after walk-forward + adversarial verification, that's the first genuinely
  Korea-executable result — and it tees up Milestone 6/8 (KIS integration) for
  live forward validation.
