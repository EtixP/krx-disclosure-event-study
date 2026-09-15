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

-- Tracks which DART daily-list dates have had their filing times scraped, so
-- the backfill is resumable across network failures.
CREATE TABLE IF NOT EXISTS scraped_dates (
    ymd TEXT PRIMARY KEY,
    n_times INTEGER,
    scraped_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- M1.2 live ingestion journal. Poll runs remain visible even when a provider
-- or parser failure interrupts a cycle.
CREATE TABLE IF NOT EXISTS live_poll_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_date TEXT NOT NULL,
    corp_cls TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
    pages_fetched INTEGER NOT NULL DEFAULT 0,
    records_seen INTEGER NOT NULL DEFAULT 0,
    new_receipts INTEGER NOT NULL DEFAULT 0,
    error_type TEXT,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_live_poll_runs_started_at
ON live_poll_runs(started_at);

-- Every distinct list-API observation is retained. The first-seen payload in
-- disclosures remains immutable even if DART's later-state rm flags change.
CREATE TABLE IF NOT EXISTS dart_disclosure_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_no TEXT NOT NULL REFERENCES disclosures(receipt_no),
    observed_at TEXT NOT NULL,
    raw_payload_sha256 TEXT NOT NULL,
    raw_payload_json TEXT NOT NULL,
    UNIQUE(receipt_no, raw_payload_sha256)
);

CREATE INDEX IF NOT EXISTS idx_dart_observations_receipt
ON dart_disclosure_observations(receipt_no, observed_at);

-- Raw viewer bytes and the parsed M1.1 relation object are stored together.
-- Multiple response vintages may coexist; none is overwritten.
CREATE TABLE IF NOT EXISTS dart_relationship_captures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_no TEXT NOT NULL,
    source_url TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    raw_html_sha256 TEXT NOT NULL,
    raw_html BLOB NOT NULL,
    relations_json TEXT NOT NULL,
    UNIQUE(receipt_no, raw_html_sha256)
);

CREATE INDEX IF NOT EXISTS idx_dart_relationship_captures_receipt
ON dart_relationship_captures(receipt_no, fetched_at);

-- One durable work item per receipt. A restart turns an interrupted processing
-- row back into failed/retryable work rather than losing it.
CREATE TABLE IF NOT EXISTS live_receipt_processing (
    receipt_no TEXT PRIMARY KEY REFERENCES disclosures(receipt_no),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('queued', 'processing', 'processed', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_attempted_at TEXT,
    processed_at TEXT,
    last_error_type TEXT,
    last_error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_live_receipt_processing_state
ON live_receipt_processing(state, first_seen_at);

-- A trigger receipt's canonical snapshot is immutable once normalized. This
-- preserves what was handed downstream at that processing time.
CREATE TABLE IF NOT EXISTS canonical_event_snapshots (
    trigger_receipt_no TEXT PRIMARY KEY REFERENCES disclosures(receipt_no),
    economic_event_id TEXT NOT NULL,
    normalized_at TEXT NOT NULL,
    event_sha256 TEXT NOT NULL,
    event_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_canonical_event_snapshots_event
ON canonical_event_snapshots(economic_event_id, normalized_at);

-- When later authoritative lineage reveals an older primary, retain the old
-- event ID as an alias. Edges are append-only and may form a short chain.
CREATE TABLE IF NOT EXISTS economic_event_aliases (
    alias_event_id TEXT PRIMARY KEY,
    canonical_event_id TEXT NOT NULL,
    learned_from_receipt_no TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK(alias_event_id <> canonical_event_id)
);

-- Delivery intent is committed before calling a consumer. Successes are not
-- delivered again; interrupted/failed attempts retain a stable idempotency key.
CREATE TABLE IF NOT EXISTS event_deliveries (
    consumer_name TEXT NOT NULL,
    trigger_receipt_no TEXT NOT NULL REFERENCES canonical_event_snapshots(trigger_receipt_no),
    delivery_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'delivering', 'succeeded', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    first_attempted_at TEXT,
    last_attempted_at TEXT,
    delivered_at TEXT,
    last_error_type TEXT,
    last_error_message TEXT,
    PRIMARY KEY(consumer_name, trigger_receipt_no),
    UNIQUE(consumer_name, delivery_id)
);

-- M2.1 prospective experiment definitions are canonical, content-hashed, and
-- append-only. A new definition for an activated experiment is a new version.
CREATE TABLE IF NOT EXISTS experiment_specifications (
    experiment_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    specification_sha256 TEXT NOT NULL,
    specification_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    historical_cutoff TEXT NOT NULL,
    forward_test_start TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    PRIMARY KEY(experiment_id, version),
    UNIQUE(experiment_id, specification_sha256)
);

-- Activations are separate immutable facts. The active version at any past
-- timestamp can therefore be resolved without rewriting its predecessor.
CREATE TABLE IF NOT EXISTS experiment_activations (
    experiment_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    specification_sha256 TEXT NOT NULL,
    activated_at TEXT NOT NULL,
    PRIMARY KEY(experiment_id, version),
    FOREIGN KEY(experiment_id, version)
        REFERENCES experiment_specifications(experiment_id, version)
);

CREATE TRIGGER IF NOT EXISTS experiment_specifications_no_update
BEFORE UPDATE ON experiment_specifications
BEGIN
    SELECT RAISE(ABORT, 'experiment specifications are immutable');
END;

CREATE TRIGGER IF NOT EXISTS experiment_specifications_no_delete
BEFORE DELETE ON experiment_specifications
BEGIN
    SELECT RAISE(ABORT, 'experiment specifications are immutable');
END;

CREATE TRIGGER IF NOT EXISTS experiment_activations_no_update
BEFORE UPDATE ON experiment_activations
BEGIN
    SELECT RAISE(ABORT, 'experiment activations are immutable');
END;

CREATE TRIGGER IF NOT EXISTS experiment_activations_no_delete
BEFORE DELETE ON experiment_activations
BEGIN
    SELECT RAISE(ABORT, 'experiment activations are immutable');
END;

-- M2.2 decisions bind the exact M2.1 definition and M1.2 event snapshot with
-- composite foreign keys. The unique indexes also retrofit existing databases.
CREATE UNIQUE INDEX IF NOT EXISTS idx_experiment_specifications_identity_hash
ON experiment_specifications(experiment_id, version, specification_sha256);

CREATE UNIQUE INDEX IF NOT EXISTS idx_experiment_activations_identity_hash_time
ON experiment_activations(
    experiment_id, version, specification_sha256, activated_at
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_canonical_event_snapshots_trigger_hash
ON canonical_event_snapshots(trigger_receipt_no, event_sha256);

CREATE TABLE IF NOT EXISTS forward_event_decisions (
    decision_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL,
    experiment_version INTEGER NOT NULL CHECK(experiment_version >= 1),
    experiment_sha256 TEXT NOT NULL,
    experiment_activated_at TEXT NOT NULL,
    trigger_receipt_no TEXT NOT NULL,
    event_sha256 TEXT NOT NULL,
    decision_input_sha256 TEXT NOT NULL,
    disposition TEXT NOT NULL CHECK(disposition IN ('eligible', 'rejected')),
    rejection_reasons_json TEXT NOT NULL,
    decision_sha256 TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE(experiment_id, experiment_version, trigger_receipt_no),
    UNIQUE(decision_sha256),
    FOREIGN KEY(experiment_id, experiment_version, experiment_sha256)
        REFERENCES experiment_specifications(
            experiment_id, version, specification_sha256
        ),
    FOREIGN KEY(
        experiment_id, experiment_version,
        experiment_sha256, experiment_activated_at
    ) REFERENCES experiment_activations(
        experiment_id, version, specification_sha256, activated_at
    ),
    FOREIGN KEY(trigger_receipt_no, event_sha256)
        REFERENCES canonical_event_snapshots(trigger_receipt_no, event_sha256)
);

CREATE INDEX IF NOT EXISTS idx_forward_event_decisions_experiment
ON forward_event_decisions(experiment_id, decided_at, trigger_receipt_no);

CREATE INDEX IF NOT EXISTS idx_forward_event_decisions_trigger
ON forward_event_decisions(trigger_receipt_no, experiment_id, experiment_version);

CREATE TRIGGER IF NOT EXISTS forward_event_decisions_no_update
BEFORE UPDATE ON forward_event_decisions
BEGIN
    SELECT RAISE(ABORT, 'forward event decisions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS forward_event_decisions_no_delete
BEFORE DELETE ON forward_event_decisions
BEGIN
    SELECT RAISE(ABORT, 'forward event decisions are immutable');
END;

"""

MIGRATIONS = [
    # idempotent ALTER TABLEs — wrap in try/except since SQLite has no IF NOT EXISTS
    # for ADD COLUMN. Added Loop 7 for the KOSPI-only / counterparty-blacklist filters.
    "ALTER TABLE extractions ADD COLUMN counterparty_name TEXT",
    "ALTER TABLE extractions ADD COLUMN counterparty_type TEXT",
    # Exact disclosure filing time (HH:MM, KST) scraped from the DART website —
    # the OpenAPI only gives the date. Enables intraday / execution-speed analysis.
    "ALTER TABLE disclosures ADD COLUMN filing_time TEXT",
]


def init_db(path: str | Path) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # timeout=30 so a brief contention from a parallel ingestion or backfill
    # doesn't immediately crash the monitor. WAL mode lets readers and writers
    # operate without blocking each other.
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.execute("PRAGMA foreign_keys = ON")
    if conn.execute("PRAGMA foreign_keys").fetchone() != (1,):
        conn.close()
        raise RuntimeError("SQLite foreign-key enforcement could not be enabled")
    # SQLite fires DELETE triggers for rows removed by conflict replacement only
    # when recursive triggers are enabled. M2.2's append-only decision table
    # therefore depends on this connection-level invariant as well as its
    # explicit UPDATE/DELETE triggers.
    conn.execute("PRAGMA recursive_triggers = ON")
    if conn.execute("PRAGMA recursive_triggers").fetchone() != (1,):
        conn.close()
        raise RuntimeError("SQLite recursive-trigger enforcement could not be enabled")
    conn.execute("PRAGMA journal_mode = WAL")
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
