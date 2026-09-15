"""Run the durable M1.2 OPEN DART disclosure watcher.

Examples:
    python -m scripts.watch_dart --once
    python -m scripts.watch_dart --poll-interval 60
    python -m scripts.watch_dart --once --date 2026-09-07 --corp-cls Y

The watcher persists raw disclosure observations and relationship evidence,
normalizes through the shared M1.1 core, journals a canonical-event logging
delivery, and records M2.2 eligibility decisions for any active experiment. It
contains no outcome, broker, order, or trade execution path.
"""

from __future__ import annotations

import argparse
import logging
import os
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from kdtb.alerts import ResearchAlertBuilder
from kdtb.config import load_settings
from kdtb.data.dart_client import DartClient
from kdtb.experiments import (
    ExperimentRegistry,
    ForwardDecisionConsumer,
    ForwardDecisionLedger,
)
from kdtb.live import EventEnvelope, LiveDartWatcher, WatcherPolicy
from kdtb.live.store import LiveEventStore
from kdtb.logging_setup import setup_logging
from kdtb.storage.db import init_db


class CanonicalEventLogConsumer:
    consumer_name = "canonical-event-log-v1"

    def __init__(self, log: logging.Logger) -> None:
        self.log = log

    def consume(self, envelope: EventEnvelope) -> None:
        self.log.info(
            "canonical_event delivery_id=%s trigger=%s event_id=%s type=%s status=%s lineage=%s",
            envelope.delivery_id,
            envelope.trigger_receipt_no,
            envelope.event.economic_event_id,
            envelope.event.event_type,
            envelope.event.status,
            envelope.event.lineage_status,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    parser.add_argument(
        "--date",
        help="Fixed YYYY-MM-DD poll date; default is the current Asia/Seoul date",
    )
    parser.add_argument(
        "--corp-cls",
        choices=["Y", "K", "N", "E"],
        default=None,
        help="Y=KOSPI, K=KOSDAQ, N=KONEX, E=other; default is all",
    )
    parser.add_argument("--poll-interval", type=float, default=60.0)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--config", default="config/default.yaml")
    return parser.parse_args()


def _target_date(value: str | None) -> date:
    if value is not None:
        return date.fromisoformat(value)
    return datetime.now(ZoneInfo("Asia/Seoul")).date()


def main() -> int:
    args = parse_args()
    load_dotenv(".env")
    settings = load_settings([Path(args.config)])
    setup_logging(settings.logging.level, settings.logging.json_format)
    log = logging.getLogger("watch_dart")

    api_key = os.getenv("DART_API_KEY")
    if not api_key:
        log.error("DART_API_KEY not set. Add it to .env (see .env.example).")
        return 1

    policy = WatcherPolicy(
        poll_interval_seconds=args.poll_interval,
        processing_batch_size=args.batch_size,
    )
    conn = init_db(settings.storage.sqlite_path)
    try:
        with DartClient(api_key) as client:
            registry = ExperimentRegistry(conn)
            decision_consumer = ForwardDecisionConsumer(
                registry=registry,
                ledger=ForwardDecisionLedger(conn, registry=registry),
                alert_builder=ResearchAlertBuilder(),
            )
            watcher = LiveDartWatcher(
                source=client,
                store=LiveEventStore(conn),
                consumers=(decision_consumer, CanonicalEventLogConsumer(log)),
                policy=policy,
            )
            if not args.once:
                watcher.run_forever(
                    corp_cls=args.corp_cls,
                    target_date=(
                        _target_date(args.date) if args.date is not None else None
                    ),
                )
                return 0

            result = watcher.run_cycle(
                _target_date(args.date),
                corp_cls=args.corp_cls,
            )
            log.info(
                "cycle complete pages=%d records=%d new=%d processed=%d failed=%d pending=%d",
                result.poll.pages_fetched,
                result.poll.records_seen,
                result.poll.new_receipts,
                len(result.processing.processed_receipt_nos),
                len(result.processing.failed_receipt_nos),
                result.processing.remaining_receipts,
            )
            return int(bool(result.processing.failed_receipt_nos))
    except Exception:
        log.exception("DART watcher stopped with an error")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
