# krx-disclosure-event-study

An event study of Korean corporate disclosures: does the market leave anything
on the table after a company files a material disclosure, and can a retail
trader capture it after costs?

> **Research deliverable, not a profitable trading system.**
> Per the 5-year empirical analysis ([RESEARCH_FINDINGS.md](RESEARCH_FINDINGS.md)),
> **no Korean disclosure event category** tested here is tradable long after
> realistic T+1-close execution costs and broad-market adjustment. Buyback's
> realistic mean is +0.14% raw but −0.03% after its matching KOSPI/KOSDAQ move
> is removed. **Shareholder change** is the one well-supported exploratory
> negative class (used conservatively as a long-side blacklist, not promoted
> as a strategy). The methodology — deterministic pipeline + walk-forward +
> adversarial verification — is the project's primary value.

## The question

Korean disclosure filings (DART) are public, timestamped, and structured. If
the market absorbs them imperfectly, a disclosure should be followed by a
short-horizon drift that a disciplined rule could trade. I wanted to know
whether that drift exists in any event category, and whether it survives the
historically applicable roundtrip cost of actually trading Korean equities.

I set out to answer this honestly, including the possibility that the answer is
no. It is no. What follows is how I established that, and how much of the
apparent edge I found along the way turned out to be artifact.

## Data

- **1,209,249 DART disclosures** ingested, covering 2021-06-28 to 2026-08-26.
  The event studies below cover events through 2026-06-24.
- **28,728 analyzed event rows** across seven disclosure categories, each with
  event-relative daily returns out to T+5.
- **3,544 supply-contract filings** parsed for contract value and prior-year
  revenue; **96.6% (3,425) extracted cleanly**, the remainder flagged
  `needs_manual_review` rather than guessed at.
- Stock prices from pinned `pykrx==1.2.8` daily OHLCV with
  `adjusted=True`, which routes to Naver Finance; broad KOSPI/KOSDAQ index
  closes from Naver Finance's public domestic-index history endpoint. The KRX
  unadjusted route requires credentials in the audited environment and is not
  the research return basis. Disclosure text and metadata come from OPEN DART.

Raw API record fields remain preserved, so derived features trace back to their
source. The live watcher additionally journals every distinct list-record
observation and stores exact relationship-viewer response bytes before
normalization. Exact filing *times* are not in the DART API but are on the DART
website; a resumable scraper backfilled them, which is what made the intraday
analysis below possible.

The canonical event layer now groups corrections, amendments, cancellations,
and completions only when DART's own report-viewer relationships support the
link. The parser handles revision `family`, attachment `att`, and related `ref`
selectors, including the provider's attachment-to-parent-family shape. It
retains every receipt and hashes both raw filings and relationship responses as
provenance; missing lineage remains explicit instead of being inferred from
similar titles. The verified historical event studies remain receipt-level
artifacts, so this new layer does not silently rewrite their samples or
headline results. M1.1 is independently verified. The independently verified
M1.2 watcher polls, journals, normalizes, and delivers canonical event
snapshots. It enforces a finite one-minute polling floor, binds parsed lineage
to the exact captured viewer bytes, and requests DART's supported ascending
receipt-date order without assuming receipt-number monotonicity.

## Method

The pipeline is deterministic end to end. No model output reaches a trading
decision:

```
DART list.json → Disclosure ↘
                              SQLite (data/kdtb.db)
DART document.xml → Parser → Extraction ↗

Disclosure + DART lineage → Canonical economic event

DART poll → raw observation/evidence journal → canonical snapshot → consumer

Extraction + Disclosure → Strategy → Signal → Risk engine (+ blacklist) → Decision
```

Three things do the real work of keeping me honest:

**Cost realism.** Every return is net of its dated, market-aware roundtrip:
0.015% commission per side, 10% VAT on commission, 5bps slippage per side, and
the statutory sell tax applicable on the exact exit date. All-in modeled costs
are 0.363% in 2021–2022, 0.333% in 2023 and 2026, 0.313% in 2024, and 0.283% in
2025. KOSPI and KOSDAQ have the same totals in this sample but different tax
components. See [src/kdtb/backtest/cost_model.py](src/kdtb/backtest/cost_model.py).

**Execution realism.** An event study that buys at the event-day close assumes a
fill I could not actually get — many disclosures land after hours. So I report
two numbers: idealized (event-day close entry) and realistic (T+1 close entry).
The gap between them is where most apparent edge dies.

**Market attribution.** Every KOSPI event is paired with KOSPI and every KOSDAQ
event with KOSDAQ on the stock row's exact recorded entry and exit dates.
Abnormal return is `stock simple return − benchmark simple return`; costs are
then subtracted once. Missing exact-date index data fails the headline analysis
rather than becoming zero or being filled from another day. Benchmark outcomes
are labels/attribution only and never decision-time learner features.

**Price basis.** Historical announcement returns use the explicitly selected
`vendor_adjusted` series. This avoids treating a later split or consolidation
as shareholder loss or gain inside an event window. The vendor can revise
historical absolute prices after a later corporate action, so committed event
observations are the reproducible vintage; adjusted closes are not presented
as executable historical quotes. The M0.4 audit and limitations are in
[`docs/history/M0.4.md`](docs/history/M0.4.md).

**Inference and hypothesis status.** Headline uncertainty uses a deterministic
issuer-clustered bootstrap, so repeated filings by one `stock_code` remain one
resampling cluster. The intervals are pointwise and descriptive. Every current
historical category, timing, blacklist, and subgroup claim is `exploratory`;
there are no pre-specified or confirmatory findings on this repeatedly examined
sample, and no multiple-testing adjustment is implied. Fixing the M0.5
reanalysis does not retroactively preregister earlier hypotheses.

**Walk-forward.** Aggregate statistics over a five-year sample hide regime
dependence. Every category is re-scored across non-overlapping half-year
windows, and I count how many come out positive. A result that only works in
2024–2025 is a regime bet, not an edge.

On top of that, each promising result went through adversarial verification —
independent passes trying to refute it on sample size, execution realism, and
regime stability. That process refuted several of my own intermediate findings,
including one I had already written into the risk engine.

## Results

All seven categories, 5-year sample. Point estimates and fold counts come from
`python -m scripts.summarize_all_categories`; the interval column comes from
`python -m scripts.analyze_research_inference`:

| Category | n | Raw T+5 net | Abnormal T+5 net | Raw realistic | Abnormal realistic | Issuer-clustered 95% CI | Abnormal WF+ | Verdict |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| supply_contract | 8,585 | −0.13% | −0.44% | −0.31% | −0.54% | [−0.74%, −0.34%] | 3/11 | neutral |
| buyback | 4,801 | +1.18% | +0.92% | +0.14% | −0.03% | [−0.21%, +0.16%] | 9/10 | neutral |
| bonus_issue | 828 | +1.22% | +1.12% | +0.21% | +0.09% | [−0.71%, +0.95%] | 6/10 | neutral |
| rights_offering | 5,911 | +0.38% | +0.27% | +0.24% | +0.11% | [−0.32%, +0.58%] | 7/11 | positive_noisy |
| convertible_bond | 4,175 | +0.62% | +0.56% | −0.03% | −0.04% | [−0.53%, +0.53%] | 5/11 | neutral |
| halt_resumption | 2,570 | −1.19% | −1.34% | +0.04% | −0.02% | [−1.41%, +1.95%] | 4/10 | neutral |
| shareholder_change | 1,858 | −1.23% | −1.64% | −1.10% | −1.40% | [−2.04%, −0.75%] | 1/10 | negative |

"Raw" is the stock return after dated costs. "Abnormal" additionally subtracts
the paired broad-index return over identical dates. "Realistic" moves entry
from the event-day close to the T+1 close. The interval is for the abnormal
realistic mean, resampling whole issuer histories; it is not adjusted for the
seven-category screen.
The tradability bar is realistic mean > +0.30% with profit factor > 1.15 and at
least 60% of walk-forward windows positive. Nothing clears it. The best
abnormal realistic mean in the table is +0.11%.

**Where the +0.30% bar comes from.** It is a chosen hurdle, and worth stating
plainly rather than leaving as a magic number. It was originally rounded from
the old 2024-style 0.313% estimate and remains fixed in M0.2 to avoid tuning a
hurdle after observing corrected results. The dated modeled costs now span
0.283%–0.363%. The margin remains useful because the 5bps-per-side slippage
assumption is the weakest input and is optimistic for the illiquid
KOSDAQ names where most of these events happen, and because the event study
fills at a daily close a real order may not get. The fixed bar has companion
gates of profit factor > 1.15 and
at least 60% of walk-forward windows positive so one lucky regime cannot carry
the mean. It lives in one place with that derivation written down
([`TRADABILITY_BAR_PCT`](src/kdtb/backtest/cost_model.py)). The conclusion does
not hinge on the historical rounding: the best abnormal realistic mean across
all seven categories is +0.11%.

**Reading the walk-forward column.** It is windows positive over windows
*scored*. A half-year window with fewer than 5 events is not scored, and is
excluded from the denominator as well as the numerator — hence 9/10 abnormal
windows for buyback (11 generated, the first has 3 events) and 3/11 for
supply_contract
(every window has enough). An earlier hand-written version of this table applied
that rule inconsistently in three rows; the numbers here are generated by the
script, which now prints the scored/skipped split. See the correction note in
[RESEARCH_FINDINGS.md](RESEARCH_FINDINGS.md).

One category remains useful as a conservative risk control, in the negative
direction. The exploratory **shareholder_change** class (disclosures involving
a change of largest shareholder) runs −1.40% abnormal realistic with a
pointwise issuer-clustered 95% interval of [−2.04%, −0.75%] and 1 of 10
walk-forward windows positive. Korean retail accounts cannot easily short it,
so it remains a long-side blacklist rather than a promoted strategy: the risk
engine rejects a long if the stock had one of these filings in the last 60 days
([src/kdtb/risk/event_blacklist.py](src/kdtb/risk/event_blacklist.py)).

### The buyback false positive

Buyback is the result I most wanted to be real, and it is the one worth
describing in detail, because the way it fell apart is the actual finding.

On first pass it looked like an edge: **+0.30% per trade**, 5 of 9 folds traded
profitably, walk-forward positive in every window with enough events. That
number cleared the bar exactly.

Adversarial verification took it apart on two counts:

**Baseline mismatch.** I was comparing the model's return, measured only over
the 2024–2026 folds it chose to trade, against always-trade diluted across all 9
folds including the early bad ones the model skipped. That is not a comparison.
On the pre-M0.2 flat-cost baseline, a **matched same-period basis** gave
trade-everything +0.315% versus the model's +0.299%.

**Regime artifact.** The model's apparent skill was fold timing, not stock
picking. Isolating the within-fold selection component gives **−0.017% per
trade** — negative, and statistically insignificant (per-fold paired t = 0.78).
The abstention in early folds that looked like prudence was a mechanical
walk-forward warmup artifact: the model had no training data yet.

Stripped of both, the pre-M0.2 whole-sample buyback return was +0.157% per
trade, consistent with the then-current +0.16% category result.

The fix outlived the finding. I added a **selection-lift** metric — model return
minus trade-everything *on the same folds* — which is the only number that
separates genuine selection skill from regime beta. It is now computed and
printed on every run, so the tool cannot make that baseline mistake again.

M0.2's historical-cost correction exposed how threshold-sensitive this learner
is: the deterministic rerun produces +0.320% for the model versus +0.298% for
matched always-trade, a **+0.022% selection lift**, and the code-level verdict
flips to `POSITIVE SELECTIVE EDGE`. That 2.2bps lift is too small and too newly
method-dependent to overturn the category result; buyback's whole-sample
realistic mean remained +0.14% raw, below the bar. M0.3 resolves the fragility:
on benchmark-adjusted rewards the model returns −0.082% versus matched
always-trade at −0.045%, a −0.037% selection lift and `NO SELECTION EDGE`
verdict. The raw threshold crossing was broad-market exposure, not recovered
stock selection.

### Entry timing: an exploratory effect that still isn't tradable

Everything above assumes T+1 close entry, which is an artifact of free daily-bar
data rather than a real constraint — a broker API can trade intraday. Backfilling
exact filing times showed 46–71% of these events publish *during* market hours,
making the same-day close a legitimate entry for them.

Assigning each event its earliest genuinely tradable entry:

| | Raw mean / trade | Raw WF+ | Abnormal mean / trade | Abnormal WF+ |
|---|---:|---:|---:|---:|
| Uniform T+1 (assumed) | +0.141% | 7/10 | −0.093% | 5/10 |
| Time-aware | +0.458% | 9/10 | +0.164% | 7/10 |
| **Entry-timing delta** | **+0.317%** | **10/10** | **+0.257%** | **9/10** |

The chronology effect remains positive after market adjustment, but it is no
longer positive in every fold. M0.5's pointwise issuer-clustered 95% interval
is [−0.059%, +0.395%] for the +0.164% adjusted absolute level and
[+0.184%, +0.331%] for the paired +0.257% timing delta. The first spans zero;
the second does not, but both remain exploratory on repeatedly inspected data.
The learned selector reverses from +0.068% raw lift to −0.087% abnormal lift.

It still is not tradable. The previously reported −0.062% median, 49.4% win
rate, and −0.43% mean after removing the top 5% are raw-return diagnostics. On
the abnormal basis they are −0.410%, 46.1%, and −0.693%; removing the top 5%
of paired abnormal timing deltas also flips +0.257% to −0.073%. The median
capturable gap is about 20bps, smaller than one KOSDAQ tick, concentrated in
exactly the low-priced names hardest to fill at the closing auction. And with
one open position at a time and a five-day hold, only ~6% of signals are even
reachable. Full detail in
[INTRADAY_FEASIBILITY.md](INTRADAY_FEASIBILITY.md).

## The learning paper-trader

After the event studies came back negative, I built the thing the project was
originally imagined to be: a paper-trader that learns from its own mock trades
([src/kdtb/learning/](src/kdtb/learning/)).

Each event is a mock trade — enter T+1 close, exit T+5 close, subtract the
matching broad-index move and its dated market-aware cost. A gradient-boosted
classifier predicts P(abnormal net > 0) from decision-time
features and trades when its PnL-optimal threshold is cleared. The loop is
champion/challenger walk-forward: each half-year fold, a challenger trains only
on strictly earlier folds, both are scored on a held-out validation fold, and
the challenger is promoted only if it wins. The champion starts as "never
trade." There is no look-ahead, enforced structurally and asserted in tests.

Two claims, which I keep separate because they have different answers:

**"The machine works" — true.** It is leak-free and seed-stable, and on a
synthetic dataset with a planted edge it finds it: **+2.021% per trade with
+2.161% selection lift** over trade-everything, trading in 6 of 6 folds. The
`--synthetic-edge` path exists precisely to prove the machine *can* learn when
there is something to learn.

**"Any Korean disclosure category is tradable" — still false.** The M0.2 raw
buyback learner returned +0.320% per trade against matched +0.298%. With M0.3
market adjustment it returns −0.082% against matched −0.045%: **selection lift
−0.037%** and `NO SELECTION EDGE`. The whole-sample realistic buyback mean is
−0.03% abnormal.
On supply contracts it traded only 2 of 9 folds — insufficient breadth, the same
recency artifact the event study found.

That gap is the point. A learning system can only optimize edge that already
exists in the data; it cannot manufacture edge from noise. Pointed at
near-efficient Korean disclosure data, small methodology changes can move a
learner across a hard threshold. The deliverable is the machine — reusable on
any dataset — plus matched-period and benchmark-adjusted reporting that exposes
the attribution error rather than overstating it.

## Limitations

- **Daily bars.** Most of the analysis uses daily closes. The entry-timing
  result above shows intraday data changes the picture materially; I have
  same-day-close resolution, not tick data.
- **The intraday effect is unconfirmed forward.** It is characterized on
  historical closes. Whether those closing-auction fills are actually
  obtainable is the one question no historical daily-close dataset can answer.
  It needs a live forward test.
- **Long-only.** The strongest signal I found (shareholder_change) is negative,
  and I cannot easily short it from a Korean retail account, so it is only
  usable as a filter.
- **Costs are modeled, not measured.** Statutory sell taxes are dated and
  market-aware, but commission remains assumed and 5bps slippage is applied per
  side. Real slippage on
  illiquid KOSDAQ names is likely worse.
- **Broad-index adjustment is deliberately simple.** It uses the KOSPI or
  KOSDAQ price index with an assumed beta of one, not a total-return, sector,
  size, or estimated-beta benchmark. Naver Finance is the historical index
  delivery source; the normalized cache is pinned and provenance-hashed.
- **Seven categories, not all of them.** Earnings surprise, large M&A, and
  delisting/relisting were never tested. The infrastructure handles a new
  category with a one-line SQL addition to
  [src/kdtb/data/event_categories.py](src/kdtb/data/event_categories.py).
- **No execution path.** There is no broker integration and no order-placing
  code in this repo, by choice — there is no edge here worth executing.
- **The LLM extraction layer is scaffolded but unused.** The deterministic
  parser hit 96.6%, so the LLM was never needed to clear the bar. Whether LLM
  extraction would beat regex on the hard 3.4% is untested.

## Reproduce

Setup:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
cp .env.example .env             # add DART_API_KEY to ingest anything new
pytest                            # run the full suite
```

The derived event-study CSVs are committed, so the headline results run
straight from a clone with no database and no API key:

```bash
# Verify the deterministic pre-revision research snapshot
python -m scripts.verify_research_state

# Verify the immutable M0.2 before/after comparison checksum
python -m scripts.compare_cost_revision

# Regenerate the M0.3 raw/abnormal comparison
python -m scripts.compare_benchmark_adjustment

# Regenerate the M0.4 corporate-action price-policy audit
python -m scripts.audit_price_adjustments

# Regenerate issuer-clustered intervals and buyback tail sensitivity
python -m scripts.analyze_research_inference

# The cross-category results table above
python -m scripts.summarize_all_categories

# One category in detail (aggregate, walk-forward, realistic execution)
python scripts/analyze_event_category.py --category buyback

# The learning paper-trader
python scripts/train_learner.py --synthetic-edge      # sanity: learns a planted edge
python scripts/train_learner.py --category buyback    # abnormal-return reward

```

The baseline harness uses the committed minimal filing-time input under
[`artifacts/baselines/pre_revision/`](artifacts/baselines/pre_revision/), so it
does not need the ignored 688 MB local database. Legacy reproduction paths keep
the M0.1/M0.2 raw-return artifacts stable while current commands use abnormal
returns. Running the live-database entry-timing command still requires that
database:

```bash
python scripts/run_intraday_walkforward.py --db data/kdtb.db
```

Categories accepted by `--category`: `supply_contract`, `buyback`,
`bonus_issue`, `rights_offering`, `convertible_bond`, `halt_resumption`,
`shareholder_change`.

Rebuilding from source data needs `DART_API_KEY` and a local SQLite DB:

```bash
# Run one raw-first live watcher cycle, or continue polling once per minute
python -m scripts.watch_dart --once
python -m scripts.watch_dart --poll-interval 60

# Print structured research alerts, or replay one local disclosure date
python -m kdtb.cli watch --once
python -m kdtb.cli replay 2026-08-25 --limit 20

# Ingest a date range
for i in $(seq 1 90); do
  d=$(date -v-${i}d -j +%Y-%m-%d)
  python scripts/ingest_disclosures.py --date $d
done

# Extract contract value / prior-year revenue (deterministic parser)
python scripts/parse_supply_contracts.py [--limit N] [--skip-existing]

# Recompute event-relative returns for a category
python scripts/run_event_study.py --category buyback

# Refresh the pinned broad-index cache and exact-date abnormal returns
python -m scripts.backfill_event_study_benchmarks --refresh

# Strategy walk-forward and the realistic-execution backtest
python scripts/walk_forward.py
python scripts/run_paper_backtest.py

# Back up the SQLite DB
./scripts/backup_db.sh
```

The watcher uses the current Asia/Seoul date unless `--date YYYY-MM-DD` is
provided. Each distinct DART record and exact relationship response is durable
before normalization. Canonical snapshots and delivery attempts are journaled,
so a restart retries interrupted work without repeating acknowledged
deliveries. Downstream consumers must honor the stable delivery ID because a
hard crash after a side effect but before acknowledgement is inherently
at-least-once. The built-in consumer only logs canonical event metadata; it
does not run a strategy or place orders.

## Deterministic event significance

M1.3 adds a pure significance engine over the canonical `EconomicEvent` model.
The initial supported measurement is deliberately narrow: supply-contract
value divided by prior-year revenue, using validated normalized fields already
produced by the deterministic extraction path. Other event types return an
explicit `unsupported_event_type` result until reliable normalized buyback,
dilution, liquidity, or timing inputs exist.

`SignificanceEngine.assess(...)` returns a structured, immutable
`EventSignificance` object. It records the measured ratio, its source fields and
derivation, missing inputs, limitations, and optional historical and
same-issuer percentile ranks. It always sets `is_trade_recommendation=False`
and `trading_recommendation=None`; magnitude is not expected return or a signal.

Percentiles require a caller-supplied, versioned
`ComparableSignificanceDataset`. The population is one latest-known
measurement per prior canonical event with the same event type and market; its
source receipt must precede the assessed event and its `known_at` timestamp
must be no later than `assessed_at`. Same-issuer unusualness further restricts
that population. Ranks use the recorded midrank formula and are withheld below
20 broad comparables or five issuer comparables. Each comparison records an
order-independent SHA-256 of the complete supplied dataset, so changed content
cannot hide behind a reused label. M1.4 may supply a durable historical-context
dataset/query layer; M1.3 does not invent one.

Missing fundamentals remain `None` and are named in `missing_inputs`. Invalid,
non-finite, non-positive, or materially inconsistent normalized measurements
fail rather than being silently converted to zero. The same mode-free engine
accepts canonical events from both live delivery and historical replay.

The 5% consistency rule uses the positive component-derived ratio as its exact
relative-error denominator, including for extremely small ratios. Assessment
and observation also reject times before the latest canonical update or
provenance source timestamp; naive DART timestamps are interpreted as Korean
local wall-clock time. M1.3 is independently `VERIFIED`.

## Historical context queries

M1.4 provides a read-only query over the seven committed Phase 0 event-study
categories:

```python
from kdtb.context import HistoricalContextService

context = HistoricalContextService().query(event, assessed_at=decision_time)
```

The service selects same-event-type, same-market receipts strictly before the
current cutoff, excludes the current canonical lineage, and uses only T+5
outcomes strictly before the Seoul assessment date. It fails on incomplete
current lineage or invalid selected benchmark data rather than risking
self-comparison, future leakage, or zero imputation.

The returned immutable object records source and selection hashes, exact
receipt/date cutoffs, selected receipt IDs, realistic T+1-to-T+5 stock,
benchmark and dated-cost components, sample size, positive fraction, and a
deterministic issuer-clustered 95% interval. It is exploratory historical
context only and structurally contains no recommendation. Deserialization
rechecks every row equation and recomputes all derived aggregates and interval
bounds, so a self-consistent replacement hash cannot legitimize contradictory
values. Independent verification replayed the row, aggregate, and inference
attacks and accepted M1.4 as `VERIFIED`; alert formatting and replay commands
are provided by M1.5.

## Structured event alerts and replay

M1.5 composes each immutable canonical event with the verified M1.3 magnitude
assessment and M1.4 historical outcome context, then renders a deterministic
terminal alert. It shows issuer/event/lineage, verified contract/revenue fields
when present, significance status, T+1-to-T+5 abnormal outcome statistics and
uncertainty, dataset cutoff, and an explicit strategy boundary. Missing fields
or unavailable context stay visible rather than becoming zero or invented
prose. No LLM is used by the alert path.

Date replay selects local disclosure receipts and sends them through
`LiveDartWatcher.replay`, so it uses the same canonical snapshot construction,
alert consumer, and durable delivery journal as live mode. Its research cutoff
is the event's latest source timestamp rather than the later replay time; this
prevents future outcomes from entering a historical alert. A repeat under the
same consumer name is skipped by the delivery journal. Existing snapshots and
original receipts can replay without an API key; update receipts need stored
relationship evidence or `DART_API_KEY`.

There is no active frozen experiment registry before M2.1. Alerts therefore
say `NO REGISTERED STRATEGY` and `NO TRADE`; the repository's legacy historical
strategy module is not misrepresented as a registered forward experiment.
This is a no-execution status, not a forecast or trading recommendation.

M1.5 is `VERIFIED`. Canonical event serialization, snapshot
hashing, watcher delivery-ID derivation, and alert validation now use one M1.2
identity boundary. Alert deserialization recomputes the delivery ID from the
exact trigger and embedded event, so issuer, event, arbitrary-ID, and
related-trigger substitutions fail before rendering. Replay chronology and the
no-strategy boundary remain unchanged.

## Frozen forward-experiment definitions

M2.1 provides a deeply immutable, versioned experiment specification and an
append-only SQLite registry. Each definition records its historical cutoff and
prospective start, event scope, decision-time features, benchmark, eligibility,
entry/exit rules, cost assumptions, exclusions, and prespecified evaluation
criteria. Evaluation fixes all-success, any-failure, failure-over-success, and
inconclusive semantics in the hashed definition and rejects an identical
condition on both sides. Canonical sorted JSON and SHA-256 make the complete
definition portable and tamper-evident.

Activation is a separate immutable fact and must be recorded no later than the
forward-test start. Registration and activation timestamps come from the
registry's UTC clock, so its public lifecycle API cannot backdate them. Any later
change creates the next version with a SHA-256 link to its activated predecessor;
database triggers reject in-place updates or deletes, and initialized SQLite
connections enforce activation-to-specification foreign keys. Identical
registration retries remain idempotent after the forward start, but new or
changed definitions remain subject to prospective chronology. Given a UTC
timestamp, the registry deterministically reconstructs the version that governed
that point in the prospective timeline, including after a process restart.

M2.1 is `VERIFIED`. Independent re-verification confirmed foreign-key
enforcement across database reopen, lifecycle-wide identical-registration
idempotency, and canonical non-ambiguous evaluation policies.

No production experiment has been registered or activated. The alert path
continues to report `NO REGISTERED STRATEGY` / `NO TRADE`.

## Forward event ledger

M2.2 connects an append-only eligibility ledger to live watch mode. For each
canonical event snapshot, it selects every frozen experiment active at the
snapshot's UTC normalization time and records one decision per experiment.
Eligible and rejected observations are both retained; rejection reasons cover
event scope, source chronology, unavailable/version-mismatched/invalid features,
eligibility rules, and exclusions. Incompatible predicates fail closed.

Each `m2.2-v1` decision embeds the exact experiment and activation plus the
canonical event, significance, historical context or structured unavailability,
and versioned feature values with source hashes. Content hashes and composite
foreign keys bind the record to the exact M2.1 activation and M1.2 event
snapshot. Identical finite-input retries return the original record without
changing its first-recorded time. Direct updates and deletes are rejected, and
every managed SQLite connection enables recursive triggers so the same delete
guard rejects the implicit conflict deletion attempted by `INSERT OR REPLACE`.
The first-recorded timestamp is database-level append-only under `init_db()`.

Historical replay does not write forward decisions. No production experiment
was activated, and M2.2 adds no outcomes, returns, PnL, fills, orders, broker
calls, or execution authority. Prospective outcomes remain a separate M2.3
layer. The independent exact-integer failure is corrected: numeric features
retain arbitrary-precision integers without float conversion, and a full-path
regression records and reloads `10**1000` exactly. The subsequent non-finite
coverage failures are also corrected. A strict-JSON extended canonical snapshot
used only when necessary makes genuine `None`, `NaN`, and both infinities
distinct in the M1.2 event hash and delivery ID; reload reconstructs the source
value, so M2.2 retries the identical decision without a separate sidecar.
Configured non-finite features and raw numeric equality/membership predicates
all fail closed with durable reasons. Ordinary event snapshot bytes remain
unchanged. `EconomicEvent` now recursively enforces string keys throughout
`normalized_fields`; canonicalization also rejects bypassed/unvalidated values
that cannot survive an exact type-sensitive encode/decode round trip, and
snapshot storage proves decoding before its transaction. Boolean-key
normalization and integer/string key collisions therefore fail before
persistence instead of creating an unreadable event. Those corrections are
confirmed, and the conflict-replacement regression now preserves the original
row and timestamp. Independent verification also covered replacement conflicts
through every current unique boundary and database reopen. M2.2 is `VERIFIED`;
M2.3 is `NOT STARTED`.

### Pipeline liveness check

```bash
python scripts/check_pipeline_health.py
python scripts/check_pipeline_health.py --date 2026-05-15 --no-ingest
```

This verifies the pipeline still works end to end against live DART data: that
the API responds, that the filing format has not drifted out from under the
deterministic parser, and that the strategy and risk engine still evaluate
without error. It ingests the day's disclosures, parses them, and prints any
candidate signals along with stocks newly hit by the blacklist.

It is a health check, not a signal generator. The candidates it prints are
pipeline output, not recommendations — the research above found no tradable edge
in this category, so a row appearing here is evidence the plumbing runs, not
evidence the trade is worth making. Nothing is submitted anywhere; the only side
effect is new rows in the local database. Zero candidates is a normal day.

## Repository

| Path | What it is |
|---|---|
| [src/kdtb/data/](src/kdtb/data/) | DART ingestion, disclosure store, price client, filing-time scraper |
| [src/kdtb/live/](src/kdtb/live/) | Durable polling, canonical snapshots, retry/recovery, consumer delivery, replay |
| [src/kdtb/alerts/](src/kdtb/alerts/) | Structured event-intelligence composition and deterministic terminal rendering |
| [src/kdtb/interpretation/](src/kdtb/interpretation/) | Deterministic parser; LLM client and validator (scaffolded, unused) |
| [src/kdtb/strategy/](src/kdtb/strategy/) | Deterministic candidate-signal rules |
| [src/kdtb/risk/](src/kdtb/risk/) | Risk engine, hard limits, event blacklist |
| [src/kdtb/backtest/](src/kdtb/backtest/) | Cost model and performance metrics |
| [src/kdtb/learning/](src/kdtb/learning/) | Champion/challenger walk-forward learner |
| [scripts/](scripts/) | Ingest, event study, analysis, walk-forward, training |
| [artifacts/baselines/](artifacts/baselines/) | Generated, provenance-hashed research-state snapshots |
| [artifacts/m0_2/](artifacts/m0_2/) | Reproducible historical-cost before/after comparison |
| [artifacts/m0_3/](artifacts/m0_3/) | Reproducible raw/abnormal benchmark comparison |
| [RESEARCH_FINDINGS.md](RESEARCH_FINDINGS.md) | Full findings and how the methodology evolved |
| [INTRADAY_FEASIBILITY.md](INTRADAY_FEASIBILITY.md) | The entry-timing effect and why it isn't deployable |
| [DESIGN.md](DESIGN.md) | Original design spec, written before any code |

## Warnings

- Experimental research code. Not financial advice.
- Backtests mislead. Most of the apparent edges in this repo's history did not
  survive contact with a larger sample or a stricter baseline, and that is the
  main thing it documents.
- The cost model uses published rates that change; verify before relying on them.
