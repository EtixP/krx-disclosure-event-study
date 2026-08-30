# Design Specification

This is the original design specification I wrote before building anything: the
project's intent, the strategy hypotheses, the layering rules, and the milestone
definitions. It is kept as written so the design can be read against what the
research actually found.

It is a plan, not a description of the finished repo. The system deliberately
stopped short of broker integration and live execution, because the research
found no tradable edge to execute on. For what was built and what the data
showed, see [README.md](README.md) and
[RESEARCH_FINDINGS.md](RESEARCH_FINDINGS.md).

## Project Title

**Event-Driven Korean Equity Trading System Using LLM-Assisted Disclosure Interpretation**

## Project Purpose

Build a real-money-capable, event-driven trading system for Korean equities that monitors official corporate disclosures, extracts structured financial signals using an LLM, evaluates those signals with deterministic trading and risk rules, and eventually places small trades through a Korean brokerage API.

This is **not** a high-frequency trading system. The goal is not to beat professional HFT firms on microsecond latency. The goal is to build a disciplined personal trading assistant that can:

1. Detect relevant corporate events faster than manual monitoring.
2. Interpret Korean financial disclosures into structured features.
3. Apply strict rule-based trading and risk filters.
4. Submit orders faster and more consistently than a human clicking through a broker app.
5. Log every decision for later evaluation.

The first serious goal is to build a reliable research and paper-trading system. Real-money execution should come only after the system has passed historical testing, live monitoring, and paper-trading validation.

---

## Core Project Thesis

Korean corporate disclosures may contain short-term tradable information that is not always fully incorporated immediately, especially for smaller and mid-cap stocks.

The system will begin with official disclosure events, not random news headlines. Official filings are cleaner, more structured, easier to audit, and less likely to be duplicated rumor/noise.

Initial hypothesis:

> Certain Korean disclosure categories, such as large supply contracts, buybacks, share cancellations, contract cancellations, or dilutive financing events, may produce measurable short-term price reactions. An automated system can identify and act on these events faster and more consistently than manual retail trading.

The project must remain intellectually honest: the edge may not exist. If backtesting and live paper trading show no edge after costs, that is still a valid and impressive result.

---

## Important Design Principle

ML/LLM components are not allowed to directly place orders. **Order submission is always the output of a short, auditable deterministic function** whose inputs include any ML/LLM-derived features. This is how serious quant funds actually operate — black-box decisions are unfixable when they go wrong; you can only turn them off.

ML/LLM IS allowed to do the heavy lifting upstream:

- **Extraction**: LLM pulls structured fields and qualitative attributes from disclosure text.
- **Feature enrichment**: LLM scores attributes that regex can't capture (counterparty type, contract language strength, hedging caveats).
- **Filtering / probability**: ML model (boosted trees on engineered features) predicts P(signal works) given current market state.
- **Position sizing**: ML output scales position size within hard caps.

Use this separation:

```text
Disclosure text
    ↓
LLM extracts structured fields + qualitative features
    ↓
Deterministic parser cross-checks numeric fields (block on mismatch)
    ↓
Feature store combines extracted, market, and regime features
    ↓
ML filter outputs P(signal works) — optional, falls back to rule-only
    ↓
Deterministic strategy layer applies hard rules → candidate signal
    ↓
Risk engine approves or rejects with reason codes
    ↓
Broker execution layer submits/cancels orders
    ↓
Logger records every input, intermediate value, and decision
```

Two patterns we never build:

```text
LLM reads headline → LLM says buy → system buys     # unsafe, unauditable
ML model outputs trade → system trades              # same problem, ML-shaped
```

The decision function is always deterministic, short, and inspectable. ML lives in the layers feeding it.

---

## Target Market

Primary market:

- Korean listed equities
- KOSPI and KOSDAQ stocks

Initial data source:

- OPEN DART API for official corporate disclosures

Future possible data sources:

- KRX KIND disclosures
- Broker-provided live quotes
- Broker-provided historical prices
- News APIs or RSS feeds, only after the official-disclosure pipeline is stable

Initial broker target:

- Korea Investment Securities Open API

Alternative broker target:

- Kiwoom REST API

The project should be designed so broker-specific logic is isolated in a `broker/` module.

---

## Non-Goals

Do **not** build these in the first version:

- Full HFT system
- FPGA/ASIC acceleration
- Reinforcement learning trading agent
- Generic ChatGPT stock picker
- Social-media sentiment bot
- Random technical-indicator bot
- Unbounded autonomous trading system
- Large-capital trading system
- Options/futures trading system
- Multi-broker smart-order router
- Crypto trading bot

These may be interesting later, but they will destroy scope control.

---

## Initial Strategy Focus

Start with **one disclosure type**:

### Major Supply Contract Strategy

Reason:

- Supply contracts often contain measurable values.
- Contract value can be compared with prior-year revenue.
- Direction is usually easier to interpret than merger/biotech/legal disclosures.
- The hypothesis can be tested historically.

Example event:

```text
Company announces a new supply contract.
Contract value: ₩85 billion
Prior-year revenue: ₩600 billion
Contract/revenue ratio: 14.2%
Disclosure time: 10:12 AM
```

Candidate rule:

```text
Generate positive signal only if:
- disclosure is a genuinely new supply contract
- contract value / prior-year revenue > threshold, e.g. 8%
- company is tradable and not halted
- live liquidity is sufficient
- bid-ask spread is below maximum threshold
- price has not already moved too far after disclosure
- no conflicting negative disclosure exists recently
```

Later strategies:

1. Share buybacks / share cancellations
2. Dilutive financing: rights offering, convertible bonds, warrants
3. Contract cancellation / major loss
4. Earnings surprise proxy
5. Trading halt / resumption events
6. Major shareholder changes

Do not add these until the first strategy has a working data, backtest, paper-trade, and logging pipeline.

---

## Project Phases

### Phase 0 — Repository Setup

Goal:

Create a clean Python project with strong separation between data ingestion, interpretation, strategy, risk, execution, and logging.

Tasks:

- Create project structure.
- Set up Python environment.
- Add `.env.example`.
- Add config system.
- Add logging.
- Add basic unit-test setup.
- Add README with warning that this is experimental and not financial advice.

Recommended stack:

- Python 3.11+
- `pydantic` for schemas
- `pandas` for analysis
- `httpx` or `requests` for APIs
- `sqlite` for early storage
- `sqlalchemy` optional
- `pytest` for tests
- `python-dotenv` for local secrets
- `streamlit` later for dashboard
- `openai`, `anthropic`, or local LLM client abstraction later

---

### Phase 1 — Disclosure Data Ingestion

Goal:

Pull official Korean disclosures from OPEN DART and store them locally.

Tasks:

- Implement `DartClient`.
- Fetch disclosure list by date.
- Filter by market, corporation, and disclosure type.
- Fetch original disclosure document when available.
- Store raw response and parsed metadata.
- Avoid duplicate ingestion.
- Add retry and rate-limit handling.

Important:

- Store raw API payloads exactly as received.
- Never rely only on processed text.
- Every parsed feature should be traceable back to the raw source.

Minimum data fields:

```text
disclosure_id
corp_code
corp_name
stock_code
report_name
receipt_no
receipt_datetime
market
raw_url
raw_payload
created_at
```

---

### Phase 2 — Historical Event Dataset

Goal:

Build a historical dataset of supply-contract disclosures and related stock returns.

Tasks:

- Identify supply-contract-related disclosure titles.
- Collect events across a meaningful historical period.
- Pull historical price data around each event.
- Compute event-time returns:
  - 1 minute
  - 5 minutes
  - 15 minutes
  - 30 minutes
  - close-to-disclosure
  - next open
  - next close
  - 3-day return
- Include realistic transaction cost assumptions.

If minute-level data is unavailable at first, begin with daily data. But the long-term goal should support intraday data because this is an event-driven strategy.

Important:

- Use out-of-sample testing.
- Do not tune thresholds repeatedly on the same period and pretend the result is real.
- Track rejected events too.

---

### Phase 3 — LLM-Assisted Disclosure Extraction

Goal:

Use an LLM to turn Korean disclosure text into structured JSON.

The LLM should extract:

```json
{
  "event_type": "major_supply_contract",
  "direction": "positive",
  "confidence": 0.0,
  "company_name": "",
  "stock_code": "",
  "contract_value_krw": null,
  "contract_counterparty": "",
  "contract_start_date": "",
  "contract_end_date": "",
  "prior_year_revenue_krw": null,
  "contract_to_revenue_ratio": null,
  "is_new_contract": null,
  "is_revision": null,
  "is_cancellation": null,
  "red_flags": [],
  "summary": ""
}
```

Rules:

- LLM output must be strict JSON.
- Validate with Pydantic.
- If required fields are missing, mark event as `needs_manual_review`.
- If numbers conflict with deterministic parser results, block trading.
- If LLM confidence is below threshold, block trading.
- If disclosure is a revision/correction and the original event is old, block trading unless explicitly handled.

Do not allow the LLM to invent missing values.

Prompting principle:

- Ask the LLM to extract and to score qualitative features — never to recommend a trade.
- The model may classify direction, but final trading logic must ignore direct "buy/sell" recommendations.

Beyond raw extraction, the LLM is also responsible for **qualitative feature enrichment** that downstream ML and rules can consume:

- counterparty_type ("government" | "large_corp" | "sme" | "unknown")
- contract_language_strength (0.0–1.0; "확정" is strong, "조건부" or hedging caveats are weak)
- counterparty_recurrence (one-shot vs repeat customer language)
- strategic_vs_routine (qualitative score)
- red_flags_qualitative (list of strings)

These features feed into the M5b ML filter, not into the order decision directly.

---

### Phase 4 — Strategy Engine

Goal:

Convert validated event features into candidate trading signals.

Signal schema:

```json
{
  "signal_id": "",
  "event_id": "",
  "stock_code": "",
  "strategy_name": "major_supply_contract_v1",
  "direction": "long",
  "strength": 0.0,
  "reason_codes": [],
  "entry_type": "marketable_limit",
  "max_entry_price": null,
  "stop_loss_pct": 0.02,
  "take_profit_pct": 0.04,
  "time_exit": "market_close",
  "created_at": ""
}
```

Initial rule example:

```text
Generate LONG candidate if:
- event_type == major_supply_contract
- is_new_contract == true
- is_cancellation == false
- contract_to_revenue_ratio >= 0.08
- confidence >= 0.80
- stock is not on blacklist
```

The strategy engine should only generate candidates. It should not execute.

---

### Phase 5 — Risk Engine

Goal:

Prevent catastrophic behavior.

The risk engine is mandatory before any live order.

Hard constraints for early real-money testing:

```text
max_order_value_krw = 30000
max_daily_loss_krw = 10000
max_open_positions = 1
max_trades_per_day = 3
no_short_selling = true
no_margin = true
no_derivatives = true
force_exit_before_close = true
require_liquidity_check = true
require_spread_check = true
require_price_move_limit = true
```

Risk checks:

- Is market open?
- Is stock tradable?
- Is account balance sufficient?
- Is there already a position?
- Is the order size below cap?
- Has daily loss limit been hit?
- Is current spread too wide?
- Has price already jumped too much?
- Is recent volatility too extreme?
- Is disclosure too old?
- Is the event duplicate or stale?
- Did the LLM output pass validation?
- Did deterministic parser confirm critical numbers?

If any check fails, block order and log the reason.

---

### Phase 6 — Broker Integration

Goal:

Connect to Korea Investment Securities Open API or another broker API.

Implementation principles:

- Broker code must be isolated.
- All broker calls must be wrapped.
- All order requests and responses must be stored.
- Never place live orders from test code.
- Require an explicit config flag for live trading.
- Default mode must be `paper`.

Modes:

```text
BACKTEST
PAPER
LIVE_TINY
LIVE
```

Only `LIVE_TINY` should be implemented initially. Do not implement unrestricted `LIVE` until the system has long-term evidence.

Order behavior:

- Prefer marketable limit orders, not blind market orders.
- Set maximum acceptable price for buys.
- Confirm order status after submission.
- Handle partial fills.
- Cancel stale unfilled orders.
- Log fills separately from submitted orders.

---

### Phase 7 — Paper Trading

Goal:

Run the system live without placing real orders.

Paper trading must use real-time observed bid/ask or conservative simulated fills.

Avoid unrealistic paper assumptions:

Bad:

```text
Signal at 10:00:00 buys at last traded price.
```

Better:

```text
Signal at 10:00:00 assumes buy at ask price plus slippage buffer.
```

Paper trading should track:

- Signal time
- Simulated entry
- Simulated exit
- Slippage estimate
- Taxes and commissions
- Maximum adverse excursion
- Maximum favorable excursion
- Reason for exit
- Whether live order would have been allowed

Minimum paper-trading period before live tiny trades:

- At least several weeks
- Preferably 50+ candidate events
- Enough to expose operational problems

---

### Phase 8 — Live Tiny Trading

Goal:

Trade tiny real-money positions to validate execution.

Rules:

- Maximum order size should be intentionally tiny at first.
- The goal is not profit.
- The goal is to verify operational correctness.
- Every trade must be explainable from stored logs.
- Stop immediately if unexpected behavior occurs.

Live tiny trading should begin with only one strategy and one event type.

No automatic scale-up. Position size increases require explicit manual decision after reviewing logs.

---

### Phase 9 — Dashboard and Analysis

Goal:

Build a dashboard for monitoring and post-trade analysis.

Dashboard should show:

- Recent disclosures
- LLM classifications
- Candidate signals
- Blocked signals and rejection reasons
- Paper trades
- Live tiny trades
- PnL
- Win rate
- Average win/loss
- Slippage
- Costs
- Performance by event type
- Performance by contract/revenue ratio bucket
- Performance by market cap/liquidity bucket

Use Streamlit for a quick dashboard.

---

## Proposed Repository Structure

```text
korean-disclosure-trading-bot/
│
├── DESIGN.md
├── README.md
├── pyproject.toml
├── requirements.txt
├── .env.example
├── .gitignore
│
├── config/
│   ├── default.yaml
│   ├── paper.yaml
│   └── live_tiny.yaml
│
├── src/
│   └── kdtb/
│       ├── __init__.py
│       │
│       ├── data/
│       │   ├── dart_client.py
│       │   ├── disclosure_store.py
│       │   ├── market_data_client.py
│       │   └── historical_prices.py
│       │
│       ├── interpretation/
│       │   ├── disclosure_cleaner.py
│       │   ├── deterministic_parser.py
│       │   ├── llm_client.py
│       │   ├── llm_prompts.py
│       │   └── extraction_validator.py
│       │
│       ├── schemas/
│       │   ├── disclosure.py
│       │   ├── extraction.py
│       │   ├── signal.py
│       │   ├── order.py
│       │   └── position.py
│       │
│       ├── strategy/
│       │   ├── base.py
│       │   └── major_supply_contract.py
│       │
│       ├── risk/
│       │   ├── limits.py
│       │   ├── checks.py
│       │   └── risk_engine.py
│       │
│       ├── broker/
│       │   ├── base.py
│       │   ├── kis_client.py
│       │   ├── paper_broker.py
│       │   └── order_manager.py
│       │
│       ├── backtest/
│       │   ├── event_study.py
│       │   ├── cost_model.py
│       │   └── metrics.py
│       │
│       ├── storage/
│       │   ├── db.py
│       │   ├── models.py
│       │   └── migrations/
│       │
│       ├── dashboard/
│       │   └── app.py
│       │
│       └── main.py
│
├── notebooks/
│   ├── 01_disclosure_exploration.ipynb
│   ├── 02_supply_contract_event_study.ipynb
│   └── 03_paper_trading_analysis.ipynb
│
├── tests/
│   ├── test_dart_client.py
│   ├── test_extraction_validator.py
│   ├── test_supply_contract_strategy.py
│   ├── test_risk_engine.py
│   └── test_cost_model.py
│
└── scripts/
    ├── ingest_disclosures.py
    ├── run_event_study.py
    ├── run_paper_trader.py
    └── run_live_tiny.py
```

---

## Data Model

### Disclosure Table

```text
id
receipt_no
corp_code
corp_name
stock_code
report_name
receipt_datetime
market
source
raw_url
raw_payload_json
raw_text
created_at
updated_at
```

### Extraction Table

```text
id
disclosure_id
model_name
prompt_version
event_type
direction
confidence
contract_value_krw
prior_year_revenue_krw
contract_to_revenue_ratio
is_new_contract
is_revision
is_cancellation
red_flags_json
summary
raw_llm_output
validation_status
validation_errors_json
created_at
```

### Signal Table

```text
id
disclosure_id
extraction_id
strategy_name
direction
strength
reason_codes_json
status
created_at
```

### Risk Decision Table

```text
id
signal_id
approved
rejection_reasons_json
snapshot_json
created_at
```

### Order Table

```text
id
signal_id
broker
account_mode
stock_code
side
order_type
quantity
limit_price
submitted_at
broker_order_id
status
raw_response_json
```

### Fill Table

```text
id
order_id
filled_quantity
fill_price
fill_time
fees
taxes
raw_response_json
```

### Position Table

```text
id
stock_code
quantity
average_price
opened_at
closed_at
realized_pnl
unrealized_pnl
status
```

---

## Backtesting Standards

The backtest must be conservative.

Always include:

- Transaction tax
- Brokerage commission
- Bid-ask spread estimate
- Slippage estimate
- Delayed entry assumption
- Failed trade assumptions when price gaps too much

Avoid:

- Look-ahead bias
- Survivorship bias
- Tuning on all available data
- Using close price when signal happened intraday
- Assuming perfect fills
- Ignoring rejected signals

Minimum metrics:

```text
number_of_events
number_of_trades
win_rate
average_return
median_return
average_win
average_loss
max_drawdown
profit_factor
sharpe_like_metric
average_slippage
total_costs
return_after_costs
benchmark_return
```

Compare against:

- Same stock buy-and-hold after disclosure
- Random event baseline
- Market index ETF baseline
- Delayed entry baseline, e.g. 5/15/30 minutes after disclosure

---

## Risk and Safety Rules

This project can eventually place real orders, so safety matters.

Hard safety rules:

1. Default mode must always be paper trading.
2. Live trading requires explicit config and command-line flag.
3. Live order size must be capped.
4. No margin.
5. No short selling.
6. No derivatives.
7. No trading if validation fails.
8. No trading if broker balance cannot be confirmed.
9. No trading if market data is stale.
10. No trading if order status cannot be confirmed.
11. No increasing position size automatically.
12. No retry loop that can accidentally submit duplicate orders.
13. Every order must have an idempotency key or internal unique request id.
14. Every action must be logged.

Emergency controls:

- Global kill switch file or config value.
- Daily loss limit.
- Max number of orders per day.
- Max number of open positions.
- Cancel stale orders.
- Force close before market close in early versions.

---

## Configuration

Use environment variables for secrets.

`.env.example`:

```text
DART_API_KEY=
KIS_APP_KEY=
KIS_APP_SECRET=
KIS_ACCOUNT_NO=
KIS_ACCOUNT_PRODUCT_CODE=
LLM_PROVIDER=
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
TRADING_MODE=PAPER
ENABLE_LIVE_TRADING=false
```

Example config:

```yaml
trading:
  mode: PAPER
  allow_live_orders: false
  max_order_value_krw: 30000
  max_daily_loss_krw: 10000
  max_open_positions: 1
  max_trades_per_day: 3
  force_exit_before_close: true

strategy:
  major_supply_contract:
    enabled: true
    min_contract_to_revenue_ratio: 0.08
    min_llm_confidence: 0.80
    max_price_move_after_disclosure_pct: 0.08
    stop_loss_pct: 0.02
    take_profit_pct: 0.04

market:
  max_spread_pct: 0.004
  min_avg_daily_trading_value_krw: 3000000000
  max_disclosure_age_minutes: 30

llm:
  provider: anthropic
  model: claude-3-5-sonnet-latest
  temperature: 0
  max_tokens: 1500
```

---

## Coding Standards

- Use type hints.
- Use Pydantic schemas for external and internal structured data.
- Keep modules small.
- Write unit tests for strategy and risk logic.
- Never put API keys in code.
- Never put live account numbers in committed files.
- Make live-trading code difficult to trigger accidentally.
- Prefer deterministic behavior.
- Log raw inputs and decisions.
- Treat broker responses as unreliable until verified.
- Do not silently swallow exceptions in live or paper trading loops.

---

## LLM Prompt Requirements

LLM prompt must tell the model:

- Extract structured data only.
- Do not give financial advice.
- Do not recommend buy/sell.
- Do not invent missing values.
- Use `null` for missing numeric fields.
- Identify uncertainty explicitly.
- Return strict JSON only.
- Flag whether the disclosure is new, revised, canceled, or administrative.

Prompt versioning is required.

Store:

```text
prompt_version
model_name
raw_prompt
raw_response
parsed_json
validation_status
```

---

## Example LLM Prompt Skeleton

```text
You are an information extraction system for Korean corporate disclosures.

Your task is to extract structured information from the disclosure text.
Do not provide investment advice.
Do not recommend buying or selling.
Do not invent missing numbers.
If a value is not explicitly present, return null.
Return strict JSON only.

Classify whether this disclosure is:
- major_supply_contract
- contract_revision
- contract_cancellation
- share_buyback
- share_cancellation
- dilutive_financing
- earnings
- other

Also determine whether the event appears positive, negative, mixed, or unclear.
This direction is only an information label, not a trading recommendation.

Disclosure text:
{{ disclosure_text }}

Return JSON matching this schema:
{{ schema }}
```

---

## Development Milestones

### Milestone 1 — Local skeleton

Acceptance criteria:

- Repository structure exists.
- Config loads.
- Tests run.
- SQLite database initializes.
- No broker or LLM integration yet.

### Milestone 2 — DART ingestion

Acceptance criteria:

- Can fetch disclosures for a date range.
- Can store raw disclosure metadata.
- Can avoid duplicate ingestion.
- Can filter supply-contract-like reports.

### Milestone 3 — LLM extraction prototype

Acceptance criteria:

- Can pass one disclosure to LLM.
- Receives strict JSON.
- Validates with Pydantic.
- Stores extraction result.
- Blocks invalid extractions.

### Milestone 4 — Historical event study

Acceptance criteria:

- Builds dataset of supply-contract events.
- Computes post-event returns.
- Includes cost model.
- Produces summary statistics.
- Saves results to CSV or database.

### Milestone 5 — Strategy and risk engine

Acceptance criteria:

- Converts validated extraction into candidate signal.
- Risk engine approves/rejects with reason codes.
- Unit tests cover common approval and rejection cases.

### Milestone 5b — ML-enriched signal filter

Goal:

Add an ML layer between candidate-signal generation (M5) and the risk engine. The ML layer DOES NOT decide whether to trade. It outputs a probability or score that becomes one input to the deterministic strategy rule.

Sub-steps:

1. **Data**: backfill 24+ months of disclosures and parsed extractions. Below ~5000 events, anything ML finds is statistically indistinguishable from noise — there is no point training before then.
2. **LLM feature enrichment**: structured-output prompts that score qualitative attributes regex cannot capture — counterparty type (government / large corp / SME / unknown), contract language strength ("확정" vs "조건부"), hedging caveats, repeat-customer signal.
3. **Engineered features**: market regime features (recent KOSPI volatility, sector momentum, disclosure flow rate) computed from existing price/disclosure data — no new dependencies.
4. **Filter model**: train a gradient-boosted tree (XGBoost / LightGBM / sklearn GBT) to predict P(T+5 return > cost) given the features. Inputs: ratio bucket, counterparty type, regime features, etc. Output: a probability.
5. **Walk-forward validation**: split data into N rolling windows. Tune thresholds on window K only from windows 1..K-1. Discard any "edge" that doesn't replicate across at least 5 windows.
6. **Deterministic decision stays unchanged**: `if (rule passes) AND (P_filter > P_threshold) AND (risk checks pass) → emit order`. The filter only narrows what passes, never expands.

Acceptance criteria:

- 5000+ events in the training dataset.
- Walk-forward validation report (CSV of OOS performance per window).
- Filter trained, persisted, loaded by the strategy layer at evaluation time.
- Strategy layer falls back to rule-only mode if the filter file is missing.
- Tests cover: feature engineering correctness, filter output shape, fallback behavior, rule-overrides-filter behavior.
- No change to risk engine or broker layer — the filter sits upstream.

Non-goals for this milestone:

- Reinforcement learning, neural nets, transformers — small data; gradient-boosted trees are the right tool.
- ML in the decision layer itself.
- Online learning.

### Milestone 6 — Paper broker

Acceptance criteria:

- Simulates order fills conservatively.
- Tracks paper positions and PnL.
- Logs all paper trades.

### Milestone 7 — Live monitoring

Acceptance criteria:

- Watches for new disclosures during market hours.
- Classifies events.
- Generates alerts.
- Does not place live orders.

### Milestone 8 — Broker API integration

Acceptance criteria:

- Authenticates with broker.
- Fetches account balance.
- Fetches live quote.
- Places no live orders by default.
- Can submit test or mock order safely if broker supports simulation.

### Milestone 9 — Live tiny mode

Acceptance criteria:

- Requires explicit manual config.
- Places tiny real order only when all checks pass.
- Logs order and fill.
- Cancels stale orders.
- Stops after max daily loss or error.

---

## Success Criteria

The project is successful if it produces:

1. A working disclosure ingestion pipeline.
2. Reliable LLM extraction with validation.
3. A historical event-study dataset.
4. A realistic backtest with costs.
5. A paper-trading system.
6. A tiny real-money execution pipeline.
7. Honest analysis of whether the strategy has edge.

Profit is not the only success criterion.

A strong negative result is still valuable if it shows:

- The market reacts too quickly.
- Transaction costs eliminate gross edge.
- LLM extraction does not improve over deterministic parsing.
- Only certain event categories are promising.

---

## Warnings

- This is experimental software.
- This is not financial advice.
- The system may lose money.
- Backtests can be misleading.
- LLM outputs can be wrong.
- Broker APIs can fail.
- Live orders can execute at bad prices.
- Real-money mode must remain tiny until the system has substantial evidence.
- The goal is rigorous research and engineering first, profit second.
