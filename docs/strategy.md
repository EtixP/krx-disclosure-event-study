# Buyback strategy foundation

## Hypothesis and existing calculations

Test whether a new direct on-exchange ordinary-share buyback's announced demand,
relative to its issuer's listed shares and usual liquidity, helps select useful
trades after costs and market movement. Prior run-up and stock/market volatility
provide context. A positive announcement alone does not predict a positive return.
This is an unproven hypothesis, not an active or validated trading strategy.

The retained feature kernel calculates:

- Announced ordinary shares / listed ordinary shares at reference close R.
- Planned ordinary-share KRW / median traded KRW over V1..V20.
- Stock and matching-index 20-session returns using all 21 closes P0..P20.
- Unannualized sample daily-return volatility (ddof=1) for stock and index.

R is the last declared close strictly before ORIGINAL public availability.
Later corrections can change cutoff-known terms but cannot move R forward.
The supplied original interval must satisfy R < L <= U <= next_close, and
availability must strictly precede the exact 08:55 KST decision. These are
consistency checks; declarations and hashes do not prove real source vintages,
calendar completeness or identity. Missing required values remain explicit.

## Accounting is not execution

The retained evaluator accepts an externally supplied eligible cohort, policy
masks and potential outcomes. It adds matched AlwaysTrade/NeverTrade comparisons,
raw net and market-adjusted rewards, and explicit states for completion,
cancellation, no fill and unknown entry/cancellation/exit. Unknowns are not zero.
A conservative unresolved-position bound is not a point return or training label.

Its rewards are return fractions on unit equal-notional positions; it does not
track portfolio cash, positions, risk limits or executable fills. P1 adds that
separate paper-account behavior. The cost model supports dated 2021–2026 inputs
and rejects other dates; fees/slippage are configurable assumptions. A later
engine must avoid double-counting modeled slippage in both prices and costs.

## Preserved experiment requirements

The original daily research proposal uses an 08:55 KST decision on the next
regular session, with planned T+1-close entry and T+5-close exit. Issuer
execution-period dates are different from this research holding window.
Inclusive greedy issuer windows reserve slots; an overlapping suppressed
candidate does not extend an owner's reservation or replace a failed owner.
Unresolved ownership prerequisites cannot grant a later free slot.

The reviewed cancellation refinement requires a linked update observed strictly
before the earlier of 15:20 KST and ten minutes before the scheduled close,
also before actual close with auction entry pending. Point equality does not
cancel; an uncertain interval crossing a boundary can remain unresolved. Public
time is not strategy-observation time, and missing update coverage is not proof
of no cancellation. None of these cohort/execution rules is implemented by the
feature or accounting kernel alone.

Historical ML remains source-gated. The earlier proposal requires at least
350/100/150 eligible decisions with resolved labels across its fixed development,
calibration and evaluation periods, 100 issuers overall and 50 in evaluation,
without dropping unresolved evaluation decisions. Every learning stage needs
its planned exit horizon and state-dependent outcome/source versions available
strictly before its cutoff, plus frozen sample/economic/precision criteria.
These conditions do not block synthetic simulator engineering or admit existing
historical data. A new prospective experiment needs its own reviewed contract.

## Source and future integration

The full original cohort plan, protocol addendum, historical learner, source
probes, calendars and evidence remain in the original research checkout at
`/Users/jsp2022310/krx-disclosure-event-study`. `docs/provenance.json` records
which exact working source versions were selected; not all were committed at
the recorded base commit. Reuse a deferred component only when a named milestone
requires it and its actual dependency/evidence conditions are reviewed.

There is no collection or broker client in this seed. Document helpers only
process supplied bytes/HTML; they do not establish applicable filing versions,
ordinary-share terms, public timing, complete correction history or data rights.
A parsed title/family is not cohort admission. No transformer, new model fit,
agent decision loop or production data-distribution claim is included.
