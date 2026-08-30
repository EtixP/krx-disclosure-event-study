"""Run the strategy engine across all parsed extractions; store candidate signals.

For each extraction (default model: deterministic_supply_contract_v1) with
validation_status='ok', the strategy evaluates and either emits a Signal or
returns None. Emitted signals get stored. Rejections get tallied by reason for
visibility.

Usage:
    python scripts/run_strategy.py
    python scripts/run_strategy.py --min-ratio 0.30      # override DESIGN.md default
    python scripts/run_strategy.py --dry-run             # don't persist, just summarize
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from kdtb.config import load_settings
from kdtb.logging_setup import setup_logging
from kdtb.schemas.disclosure import Disclosure
from kdtb.schemas.extraction import Extraction
from kdtb.storage.db import init_db
from kdtb.storage.signal_store import SignalStore
from kdtb.strategy.major_supply_contract import MajorSupplyContractStrategy

PARSER_MODEL = "deterministic_supply_contract_v1"

QUERY = """
SELECT e.id AS extraction_id, e.disclosure_id, e.model_name, e.prompt_version,
       e.event_type, e.direction, e.confidence,
       e.contract_value_krw, e.prior_year_revenue_krw, e.contract_to_revenue_ratio,
       e.is_new_contract, e.is_revision, e.is_cancellation,
       e.counterparty_name, e.counterparty_type,
       e.red_flags_json, e.summary, e.raw_llm_output,
       e.validation_status, e.validation_errors_json,
       d.id AS d_id, d.receipt_no, d.corp_code, d.corp_name, d.stock_code,
       d.report_name, d.receipt_datetime, d.market
FROM extractions e
JOIN disclosures d ON e.disclosure_id = d.id
WHERE e.model_name = ? AND e.validation_status = 'ok'
ORDER BY d.receipt_datetime DESC
"""


def _build_extraction(row: sqlite3.Row) -> Extraction:
    return Extraction(
        id=row["extraction_id"],
        disclosure_id=row["disclosure_id"],
        model_name=row["model_name"],
        prompt_version=row["prompt_version"],
        event_type=row["event_type"],
        direction=row["direction"],
        confidence=row["confidence"],
        contract_value_krw=row["contract_value_krw"],
        prior_year_revenue_krw=row["prior_year_revenue_krw"],
        contract_to_revenue_ratio=row["contract_to_revenue_ratio"],
        is_new_contract=bool(row["is_new_contract"]) if row["is_new_contract"] is not None else None,
        is_revision=bool(row["is_revision"]) if row["is_revision"] is not None else None,
        is_cancellation=bool(row["is_cancellation"]) if row["is_cancellation"] is not None else None,
        counterparty_name=row["counterparty_name"],
        counterparty_type=row["counterparty_type"],
        red_flags=json.loads(row["red_flags_json"] or "[]"),
        summary=row["summary"] or "",
        raw_llm_output=row["raw_llm_output"],
        validation_status=row["validation_status"],
        validation_errors=json.loads(row["validation_errors_json"] or "[]"),
    )


def _build_disclosure(row: sqlite3.Row) -> Disclosure:
    return Disclosure(
        id=row["d_id"],
        receipt_no=row["receipt_no"],
        corp_code=row["corp_code"],
        corp_name=row["corp_name"],
        stock_code=row["stock_code"],
        report_name=row["report_name"],
        receipt_datetime=datetime.fromisoformat(row["receipt_datetime"]),
        market=row["market"],
    )


def _why_no_signal(ext: Extraction, disc, params) -> str:
    """Categorize why an extraction didn't produce a signal."""
    if not params.enabled:
        return "strategy_disabled"
    if ext.event_type != "major_supply_contract":
        return f"wrong_event_type:{ext.event_type}"
    if ext.is_cancellation:
        return "is_cancellation"
    if ext.is_revision and not ext.is_new_contract:
        return "is_pure_revision"
    if ext.is_new_contract is not True:
        return "not_marked_as_new_contract"
    if ext.confidence < params.min_llm_confidence:
        return f"confidence_below_threshold"
    r = ext.contract_to_revenue_ratio
    if r is None:
        return "ratio_unknown"
    if r < params.min_contract_to_revenue_ratio:
        return "ratio_below_threshold"
    if params.kospi_only and disc.market != "KOSPI":
        return f"market_filter:{disc.market}"
    if ext.counterparty_type in params.excluded_counterparty_types:
        return f"counterparty_filter:{ext.counterparty_type}"
    return "other"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--min-ratio", type=float, default=None)
    p.add_argument("--min-confidence", type=float, default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--model", default=PARSER_MODEL)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    load_dotenv(".env")
    settings = load_settings([Path("config/default.yaml")])
    setup_logging(settings.logging.level, settings.logging.json_format)
    log = logging.getLogger("run_strategy")

    params = settings.strategy.major_supply_contract
    if args.min_ratio is not None:
        params.min_contract_to_revenue_ratio = args.min_ratio
    if args.min_confidence is not None:
        params.min_llm_confidence = args.min_confidence
    log.info(
        "Strategy params: min_ratio=%.3f min_confidence=%.2f",
        params.min_contract_to_revenue_ratio, params.min_llm_confidence,
    )

    strategy = MajorSupplyContractStrategy(params)

    conn = init_db(settings.storage.sqlite_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(QUERY, (args.model,)).fetchall()
    log.info("Evaluating %d extractions (model=%s, status=ok)", len(rows), args.model)

    store = SignalStore(conn)
    if not args.dry_run:
        cleared = store.clear_strategy(strategy.name)
        if cleared:
            log.info("Cleared %d prior signals for strategy=%s", cleared, strategy.name)

    signals: list = []
    rejection_reasons: Counter = Counter()
    for row in rows:
        ext = _build_extraction(row)
        disc = _build_disclosure(row)
        sig = strategy.evaluate(ext, disc)
        if sig is None:
            rejection_reasons[_why_no_signal(ext, disc, params)] += 1
            continue
        signals.append(sig)
        if not args.dry_run:
            store.upsert(sig)

    if not args.dry_run:
        conn.commit()

    log.info("=" * 60)
    log.info("Generated %d signals from %d extractions", len(signals), len(rows))
    log.info("Rejections by reason:")
    for reason, n in rejection_reasons.most_common():
        log.info("  %-30s %5d", reason, n)
    if signals:
        avg_strength = sum(s.strength for s in signals) / len(signals)
        log.info("Signal strength: min=%.2f mean=%.2f max=%.2f",
                 min(s.strength for s in signals),
                 avg_strength,
                 max(s.strength for s in signals))
        # Top 5 by strength
        top = sorted(signals, key=lambda s: -s.strength)[:5]
        log.info("Top 5 by strength:")
        for s in top:
            log.info("  %s strength=%.2f reasons=%s", s.signal_id, s.strength, s.reason_codes[:2])

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
