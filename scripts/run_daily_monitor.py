"""Daily disclosure monitor: end-to-end pipeline for one date.

What it does for the given date (default today):
  1. Ingest DART disclosures for the date (skip if --no-ingest).
  2. Parse all matching supply-contract disclosures with the deterministic parser.
  3. Evaluate each ok extraction through the v2 strategy
     (default config: ratio >= 0.08, KOSPI only, skip government counterparty).
  4. Apply the risk engine including the shareholder_change blacklist
     (default 60-day lookback).
  5. Print a candidate-signal table + a "blacklist-impact" table showing
     stocks that just had a negative event (and so will be blocked for 60d).

Read-only with respect to broker state — no orders are submitted. The DB
is updated with the new disclosures and parser extractions.

Usage:
    python scripts/run_daily_monitor.py
    python scripts/run_daily_monitor.py --date 2026-05-27
    python scripts/run_daily_monitor.py --date 2026-05-27 --no-ingest   # use existing DB rows
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

from dotenv import load_dotenv

from kdtb.config import load_settings
from kdtb.data.dart_client import DartClient
from kdtb.data.disclosure_store import DisclosureStore
from kdtb.data.event_categories import CATEGORIES
from kdtb.interpretation.deterministic_parser import (
    PARSER_VERSION,
    parse_supply_contract,
)
from kdtb.logging_setup import setup_logging
from kdtb.risk import EventBlacklist, RiskContext, RiskDecision, RiskEngine
from kdtb.risk.limits import RiskLimits
from kdtb.schemas.disclosure import Disclosure
from kdtb.schemas.extraction import Extraction
from kdtb.storage.db import init_db
from kdtb.storage.extraction_store import ExtractionStore
from kdtb.strategy.major_supply_contract import MajorSupplyContractStrategy


@dataclass
class CandidateOutcome:
    signal_id: str
    stock_code: str
    corp_name: str
    market: str
    ratio: float | None
    strength: float
    contract_value_krw: int | None
    counterparty_type: str | None
    risk_decision: RiskDecision


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    p.add_argument("--no-ingest", action="store_true", help="Skip DART ingest, use existing DB rows")
    p.add_argument("--no-parse", action="store_true", help="Skip parser, use existing extractions")
    p.add_argument("--config", default="config/default.yaml")
    return p.parse_args()


def _target_date(arg: str | None) -> date:
    if arg:
        try:
            return datetime.strptime(arg, "%Y-%m-%d").date()
        except ValueError as e:
            raise SystemExit(f"--date must be YYYY-MM-DD, got: {arg!r}") from e
    return date.today()


def _ingest(client: DartClient, store: DisclosureStore, target: date) -> tuple[int, int]:
    records = list(client.list_disclosures(target))
    return store.ingest_records(records)


def _supply_contract_disclosures_needing_parse(
    conn: sqlite3.Connection, target: date
) -> list[dict]:
    where = CATEGORIES["supply_contract"]
    rows = conn.execute(
        f"""
        SELECT d.id, d.receipt_no, d.corp_name, d.stock_code, d.report_name, d.market
        FROM disclosures d
        LEFT JOIN extractions e
          ON e.disclosure_id = d.id AND e.model_name = ?
        WHERE DATE(d.receipt_datetime) = ?
          AND ({where})
          AND d.stock_code IS NOT NULL
          AND d.stock_code != ''
          AND d.market IN ('KOSPI', 'KOSDAQ')
          AND e.id IS NULL
        """,
        (PARSER_VERSION, target.isoformat()),
    ).fetchall()
    return [
        {"id": r[0], "receipt_no": r[1], "corp_name": r[2],
         "stock_code": r[3], "report_name": r[4], "market": r[5]}
        for r in rows
    ]


def _parse_pending(
    client: DartClient, conn: sqlite3.Connection, target: date, log: logging.Logger
) -> tuple[int, int]:
    """Parse all unparsed supply-contract disclosures for the date.

    Returns (n_ok, n_attempted) so the caller can distinguish "everything failed"
    from "everything succeeded but yielded a non-ok validation_status".
    """
    pending = _supply_contract_disclosures_needing_parse(conn, target)
    if not pending:
        return 0, 0
    store = ExtractionStore(conn)
    n_ok = 0
    for row in pending:
        try:
            text = client.fetch_document_text(row["receipt_no"])
        except Exception as e:
            log.warning("fetch failed for %s: %s", row["receipt_no"], e)
            continue
        ext = parse_supply_contract(text, disclosure_id=row["id"], report_name=row["report_name"])
        store.upsert(ext)
        if ext.validation_status == "ok":
            n_ok += 1
    conn.commit()
    return n_ok, len(pending)


def _build_extraction(row: sqlite3.Row) -> Extraction:
    import json
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


def _evaluate_signals(
    conn: sqlite3.Connection,
    target: date,
    strategy: MajorSupplyContractStrategy,
    engine: RiskEngine,
) -> list[CandidateOutcome]:
    # row_factory may already be set by caller; setting again is harmless.
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"""
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
        WHERE e.model_name = ?
          AND e.validation_status = 'ok'
          AND DATE(d.receipt_datetime) = ?
        """,
        (PARSER_VERSION, target.isoformat()),
    ).fetchall()

    outcomes: list[CandidateOutcome] = []
    # `now` for the risk engine should be a representative trading-hour time
    # so the disclosure-age check doesn't reject everything from earlier in the day.
    now_dt = datetime.combine(target, time(15, 0))
    for row in rows:
        ext = _build_extraction(row)
        disc = _build_disclosure(row)
        sig = strategy.evaluate(ext, disc)
        if sig is None:
            continue
        sig.notional_krw = engine.limits.max_order_value_krw  # for risk eval
        ctx = RiskContext(
            signal=sig, extraction=ext,
            disclosure_time=disc.receipt_datetime,
            now=now_dt,
        )
        decision = engine.evaluate(ctx)
        outcomes.append(CandidateOutcome(
            signal_id=sig.signal_id,
            stock_code=disc.stock_code or "",
            corp_name=disc.corp_name,
            market=disc.market,
            ratio=ext.contract_to_revenue_ratio,
            strength=sig.strength,
            contract_value_krw=ext.contract_value_krw,
            counterparty_type=ext.counterparty_type,
            risk_decision=decision,
        ))
    return outcomes


def _print_signals(outcomes: list[CandidateOutcome]) -> None:
    if not outcomes:
        print("\n  (no candidate signals on this date)\n")
        return
    print("\n=== Candidate signals ===")
    print(f"{'stock':>7} {'market':>7} {'corp':<22} {'ratio':>7} {'value(원)':>15} {'cp_type':>15} {'strength':>9}  status")
    print("-" * 130)
    for o in outcomes:
        status = "APPROVED" if o.risk_decision.approved else f"BLOCKED: {','.join(o.risk_decision.rejection_reasons)[:60]}"
        ratio_s = f"{o.ratio:.3f}" if o.ratio is not None else "n/a"
        val_s = f"{o.contract_value_krw:,}" if o.contract_value_krw is not None else "n/a"
        cp_s = o.counterparty_type or "unknown"
        print(f"{o.stock_code:>7} {o.market:>7} {o.corp_name[:22]:<22} {ratio_s:>7} {val_s:>15} {cp_s:>15} {o.strength:>9.2f}  {status}")


def _print_blacklist_events(conn: sqlite3.Connection, target: date) -> None:
    """Show negative-event disclosures hit today — these will block trades on those stocks for the lookback period."""
    blacklist_cats = ["shareholder_change"]  # match DEFAULT_NEGATIVE_CATEGORIES
    fragments = " OR ".join(f"({CATEGORIES[c]})" for c in blacklist_cats)
    rows = conn.execute(
        f"""
        SELECT corp_name, stock_code, report_name
        FROM disclosures
        WHERE DATE(receipt_datetime) = ?
          AND stock_code IS NOT NULL
          AND ({fragments})
        """,
        (target.isoformat(),),
    ).fetchall()
    if not rows:
        return
    print(f"\n=== Negative events today (will block long signals on these stocks for 60d) ===")
    for r in rows:
        print(f"  {r[1]:>7} {r[0]:<25} {r[2]}")


def main() -> int:
    args = parse_args()
    load_dotenv(".env")
    settings = load_settings([Path(args.config)])
    setup_logging(settings.logging.level, settings.logging.json_format)
    log = logging.getLogger("daily_monitor")

    target = _target_date(args.date)
    log.info("monitoring %s", target)

    if not args.no_ingest:
        api_key = os.getenv("DART_API_KEY")
        if not api_key:
            log.error("DART_API_KEY not set in .env — cannot ingest. Use --no-ingest to skip.")
            return 1
    else:
        api_key = None

    conn = init_db(settings.storage.sqlite_path)
    conn.row_factory = sqlite3.Row

    if not args.no_ingest and api_key is not None:
        with DartClient(api_key) as client:
            store = DisclosureStore(conn)
            new, total = _ingest(client, store, target)
            log.info("ingested: %d total / %d new", total, new)
            if not args.no_parse:
                n_ok, n_attempted = _parse_pending(client, conn, target, log)
                log.info(
                    "parsed: %d ok / %d attempted (%d had non-ok validation or failed to fetch)",
                    n_ok, n_attempted, n_attempted - n_ok,
                )
    elif not args.no_parse:
        log.info("--no-ingest implies skipping parse (cannot fetch docs without DartClient)")

    strategy = MajorSupplyContractStrategy(settings.strategy.major_supply_contract)
    limits = RiskLimits(
        max_order_value_krw=settings.trading.max_order_value_krw,
        max_daily_loss_krw=settings.trading.max_daily_loss_krw,
        max_open_positions=settings.trading.max_open_positions,
        max_trades_per_day=settings.trading.max_trades_per_day,
        min_llm_confidence=settings.strategy.major_supply_contract.min_llm_confidence,
        # Daily monitor: we're reviewing the full day's events, not enforcing
        # real-time staleness. The DART list endpoint only gives date (no time),
        # so a tight in-config max_disclosure_age_minutes (e.g. 30) would block
        # every signal on a historical date. Allow up to 24h here.
        max_disclosure_age_minutes=24 * 60,
    )
    blacklist = EventBlacklist(conn)
    engine = RiskEngine(limits, blacklist=blacklist)

    outcomes = _evaluate_signals(conn, target, strategy, engine)
    _print_signals(outcomes)
    _print_blacklist_events(conn, target)

    print()
    approved = sum(1 for o in outcomes if o.risk_decision.approved)
    blocked = len(outcomes) - approved
    print(f"Summary: {len(outcomes)} candidate signal(s), {approved} approved, {blocked} blocked.")
    print("(no orders placed — this is a research monitor, not an execution path)")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
