# Research baselines

This directory preserves diagnostic snapshots of research outputs before
methodology changes. A baseline is evidence of what a particular implementation
reported; it is not a target that later code must reproduce.

## Pre-revision snapshot

`pre_revision/` contains four generated JSON outputs:

- `cross_category_summary.json` — all seven event categories, including
  aggregate, realistic-entry, market-split, and half-year results;
- `buyback_intraday_summary.json` — filing-time coverage, time-aware entry,
  fold results, tail diagnostics, and learned-selector comparison;
- `learner_buyback_summary.json` — deterministic real-buyback learner results
  plus the planted-edge control;
- `shareholder_change_summary.json` — the currently blacklisted negative event
  class, copied structurally from the cross-category result.

`manifest.json` records hashes for those artifacts, the runtime versions, the
tracked event-study CSV inputs, and the exact generator source files.

The ignored local SQLite database is not needed to regenerate the baseline.
`pre_revision/inputs/buyback_filing_times.csv` is the minimal pinned input slice:
the public DART receipt number and scraped filing time for buyback rows with an
available time. It was extracted from `data/kdtb.db` by the generator; missing
filing times remain missing through absence from the slice.

## Commands

From the repository root with the project environment active:

```bash
# Verify committed artifact hashes and internal arithmetic invariants.
python -m scripts.verify_research_state

# Historical diagnostic: after later milestones this is expected to report drift.
python -m scripts.verify_research_state --check-inputs

# Replay with current code/data into a separate directory.
python -m scripts.verify_research_state \
  --generate \
  --output-dir /tmp/m0_1_replay \
  --filing-times-input artifacts/baselines/pre_revision/inputs/buyback_filing_times.csv
```

The verified `pre_revision/` directory is immutable. Generation and input
refresh commands refuse to write there; current-tree replays belong in a new
directory and are not expected to reproduce its historical provenance hashes.

The input slice should be refreshed only when intentionally defining a new
snapshot from a local source database:

```bash
python -m scripts.verify_research_state \
  --refresh-buyback-times-from-db \
  --db data/kdtb.db \
  --generate \
  --output-dir /tmp/m0_1_db_replay
```

Generation is byte-deterministic for the same data, source, and dependency
versions. Learner paths use `random_state=0`.

## Comparing a later methodology

Generate later results into a separate directory and reuse the pinned intraday
input so data changes are not confused with methodology changes:

```bash
python -m scripts.verify_research_state \
  --generate \
  --snapshot-name after_m0_2 \
  --output-dir artifacts/baselines/after_m0_2 \
  --filing-times-input artifacts/baselines/pre_revision/inputs/buyback_filing_times.csv

diff -ru \
  artifacts/baselines/pre_revision \
  artifacts/baselines/after_m0_2
```

The default verifier intentionally does not recompute current research and
assert equality with the old numbers. It verifies artifact hashes, pinned-input
integrity, cross-artifact agreement, category counts across aggregate,
execution-scenario, market, and fold views, coverage arithmetic, and learner
selection-lift arithmetic. `--check-inputs` also hashes every direct
result-producing project source used by the harness, including
`src/kdtb/backtest/metrics.py`. This keeps old baselines diagnostic when later
milestones correctly change methodology.
