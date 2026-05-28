# tradeforme

Event-driven Korean equity trading research system. Monitors official corporate
disclosures (OPEN DART), uses an LLM to extract structured event information,
evaluates candidate trades with deterministic strategy and risk rules, and
supports backtesting, paper trading, and tiny live execution through a
brokerage API.

The system tests whether selected disclosure events (initially major supply
contracts) produce delayed short-term price reactions after realistic Korean
market transaction costs. **The LLM is used only for information extraction;
all trading and risk decisions are handled by deterministic code.**

See [CLAUDE.md](CLAUDE.md) for the full design specification.

## Warning

- Experimental software. Not financial advice. May lose money.
- Backtests can be misleading; LLM outputs can be wrong; broker APIs can fail.
- Default mode is `PAPER`. Live trading requires explicit config and is capped
  to tiny size until the system has substantial evidence of working correctly.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
cp .env.example .env  # fill in keys as needed
pytest
```

## Status

Milestone 1: repo skeleton, config, schemas, SQLite helper, placeholders, tests.
No live broker calls. No real LLM calls. See `CLAUDE.md` for the full roadmap.
