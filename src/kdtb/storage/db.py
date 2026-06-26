from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS disclosures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_no TEXT NOT NULL UNIQUE,
    corp_code TEXT NOT NULL,
    corp_name TEXT NOT NULL,
    stock_code TEXT,
    report_name TEXT NOT NULL,
    receipt_datetime TEXT NOT NULL,
    market TEXT NOT NULL DEFAULT 'OTHER',
    source TEXT NOT NULL DEFAULT 'DART',
    raw_url TEXT,
    raw_payload_json TEXT,
    raw_text TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_disclosures_corp_code ON disclosures(corp_code);
CREATE INDEX IF NOT EXISTS idx_disclosures_receipt_datetime ON disclosures(receipt_datetime);

CREATE TABLE IF NOT EXISTS extractions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    disclosure_id INTEGER NOT NULL REFERENCES disclosures(id),
    model_name TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    event_type TEXT NOT NULL,
    direction TEXT NOT NULL,
    confidence REAL NOT NULL,
    contract_value_krw INTEGER,
    prior_year_revenue_krw INTEGER,
    contract_to_revenue_ratio REAL,
    is_new_contract INTEGER,
    is_revision INTEGER,
    is_cancellation INTEGER,
    counterparty_name TEXT,
    counterparty_type TEXT,
    red_flags_json TEXT,
    summary TEXT,
    raw_llm_output TEXT,
    validation_status TEXT NOT NULL DEFAULT 'ok',
    validation_errors_json TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id TEXT NOT NULL UNIQUE,
    disclosure_id INTEGER NOT NULL REFERENCES disclosures(id),
    extraction_id INTEGER NOT NULL REFERENCES extractions(id),
    stock_code TEXT NOT NULL,
    strategy_name TEXT NOT NULL,
    direction TEXT NOT NULL,
    strength REAL NOT NULL,
    reason_codes_json TEXT,
    entry_type TEXT NOT NULL DEFAULT 'marketable_limit',
    max_entry_price REAL,
    stop_loss_pct REAL NOT NULL,
    take_profit_pct REAL NOT NULL,
    time_exit TEXT NOT NULL DEFAULT 'market_close',
    notional_krw INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS risk_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id TEXT NOT NULL REFERENCES signals(signal_id),
    approved INTEGER NOT NULL,
    rejection_reasons_json TEXT,
    snapshot_json TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id TEXT NOT NULL REFERENCES signals(signal_id),
    broker TEXT NOT NULL,
    account_mode TEXT NOT NULL,
    stock_code TEXT NOT NULL,
    side TEXT NOT NULL,
    order_type TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    limit_price REAL,
    submitted_at TEXT,
    broker_order_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    raw_response_json TEXT
);

CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    filled_quantity INTEGER NOT NULL,
    fill_price REAL NOT NULL,
    fill_time TEXT NOT NULL,
    fees REAL DEFAULT 0,
    taxes REAL DEFAULT 0,
    raw_response_json TEXT
);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    average_price REAL NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    realized_pnl REAL DEFAULT 0,
    unrealized_pnl REAL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'open'
);
"""


MIGRATIONS = [
    # idempotent ALTER TABLEs — wrap in try/except since SQLite has no IF NOT EXISTS
    # for ADD COLUMN. Added Loop 7 for the KOSPI-only / counterparty-blacklist filters.
    "ALTER TABLE extractions ADD COLUMN counterparty_name TEXT",
    "ALTER TABLE extractions ADD COLUMN counterparty_type TEXT",
]


def init_db(path: str | Path) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    for sql in MIGRATIONS:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError as e:
            # "duplicate column name" means the migration already ran — ignore.
            if "duplicate column" not in str(e):
                raise
    conn.commit()
    return conn
