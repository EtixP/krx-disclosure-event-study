# KRX PaperLab

A Python paper-trading project for Korean equities, starting with disclosure-driven
buyback strategies. The immediate goal is a reproducible simulated account;
ML improvements and an agent operating a frozen paper strategy come later.

This branch is a focused foundation. Orders, fills, cash/positions and an agent
runner are not implemented yet. No profitable strategy or executable fill has
been established.

## What is included

- Buyback demand and pre-publication stock/market context calculations.
- Matched-policy accounting with explicit cancellation, no-fill and unknown states.
- Dated Korean-equity transaction-cost calculations.
- Shared disclosure/event models, lineage normalization and pure document helpers.
- Focused offline tests using synthetic inputs and one existing viewer fixture.

The package remains `kdtb` to preserve useful import paths. The installable
project is named `krx-paperlab`.

## Setup and checks

From this checkout, with Python 3.11 or newer:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q
```

The complete retained suite currently has 319 passing tests. These checks
require no credentials, database, market feed or original research checkout. Do not reuse the original project's editable virtual environment.

## Layout

| Path | Purpose |
|---|---|
| `src/kdtb/learning/` | Buyback feature and economic-accounting calculations |
| `src/kdtb/backtest/` | Dated cost model |
| `src/kdtb/schemas/` | Shared event models and declared session values |
| `src/kdtb/events/` | Disclosure normalization and declared chronology checks |
| `src/kdtb/data/` | Pure helpers for already captured documents; no HTTP client |
| `tests/` | Self-contained retained regression tests and fixture |
| [docs/strategy.md](docs/strategy.md) | Hypothesis, timing, accounting and evidence limits |
| [docs/provenance.json](docs/provenance.json) | Source working-file hashes and exact extractions/adaptations |

## Preserved research and limits

The original `ML_Design` checkout is left intact at
`/Users/jsp2022310/krx-disclosure-event-study`. Its historical datasets, private
evidence, research artifacts, old learner, collectors and full research test
suite are not copied into this branch. They remain available for a later,
explicitly scoped port. The source was a dirty working tree; its base commit
alone does not contain all the source versions used here.

The original 1.2M-disclosure collection and 1,000+ test suite are not part of
this checkout. This branch contains a smaller test suite for its retained code.
The original DB-dependent artifact failure remains unresolved in the original
project; omitting that unrelated reproduction does not repair or bless its DB.

The retained session value validates a declared date and close, not an official
exchange calendar. The cost model supports 2021–2026 and rejects other dates.
Real market runs need permitted, appropriately timed data. Historical ML and
empirical trading claims remain gated by source evidence and independent review.
Synthetic simulator tests can proceed without claiming a trading edge.

## Next development steps

First implement a deterministic paper order/fill/cash/position engine over
explicit fixtures, with conservative fill assumptions and visible rejections.
Then add portfolio constraints and one frozen buyback baseline. Permitted
forward data, independently evaluated ML comparisons and an agent operating
that paper policy follow in separate, reviewed steps.

Local agent instructions, current-state checkpoints and implementation journals
stay outside version control under this repository's pre-commit policy.
