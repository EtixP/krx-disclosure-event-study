from __future__ import annotations

import argparse
import os
from contextlib import nullcontext
from datetime import date, datetime
from numbers import Integral
from pathlib import Path
from typing import Sequence
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from kdtb.alerts import (
    ResearchAlertBuilder,
    ResearchAlertConsumer,
    historical_replay_assessment_time,
)
from kdtb.config import load_settings
from kdtb.context import HistoricalContextService
from kdtb.data.dart_client import DartClient
from kdtb.experiments import (
    ExperimentRegistry,
    ForwardDecisionConsumer,
    ForwardDecisionLedger,
)
from kdtb.live import LiveDartWatcher, WatcherPolicy
from kdtb.live.store import LiveEventStore
from kdtb.logging_setup import setup_logging
from kdtb.schemas.alert import ResearchAlert
from kdtb.storage.db import init_db

SEOUL = ZoneInfo("Asia/Seoul")


def _date_arg(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from error


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("value must be a positive integer") from error
    if isinstance(parsed, bool) or not isinstance(parsed, Integral) or parsed < 1:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return parsed


class _OfflineReplaySource:
    """Allow local original/snapshotted receipts to replay without an API key."""

    def iter_disclosure_pages(self, target_date, corp_cls=None, page_count=100):
        raise RuntimeError("offline replay cannot poll DART")

    def fetch_report_relations_capture(self, rcept_no):
        raise RuntimeError(
            "DART_API_KEY is required to retrieve missing relationship evidence"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kdtb",
        description="Deterministic Korean disclosure event intelligence",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    watch = subparsers.add_parser("watch", help="Poll DART and print event alerts")
    watch.add_argument("--once", action="store_true", help="Run one cycle and exit")
    watch.add_argument(
        "--date",
        type=_date_arg,
        help="Fixed poll date; defaults to the current Asia/Seoul date",
    )
    watch.add_argument("--corp-cls", choices=["Y", "K", "N", "E"])
    watch.add_argument("--poll-interval", type=float, default=60.0)
    watch.add_argument("--batch-size", type=_positive_int, default=100)
    watch.add_argument("--config", default="config/default.yaml")
    watch.add_argument("--context-data-dir", default="data")

    replay = subparsers.add_parser(
        "replay",
        help="Replay one disclosure date through the live event-alert consumer",
    )
    replay.add_argument("date", type=_date_arg)
    replay.add_argument("--limit", type=_positive_int)
    replay.add_argument("--config", default="config/default.yaml")
    replay.add_argument("--context-data-dir", default="data")
    replay.add_argument(
        "--consumer-name",
        default="research-alert-replay-v1",
        help="Delivery-journal identity; change deliberately to repeat output",
    )
    return parser


def _console_sink(_: ResearchAlert, rendered: str) -> None:
    print(rendered, flush=True)
    print(flush=True)


def _api_key() -> str | None:
    load_dotenv(".env")
    return os.getenv("DART_API_KEY")


def _builder(context_data_dir: str) -> ResearchAlertBuilder:
    return ResearchAlertBuilder(
        historical_context_service=HistoricalContextService(
            data_dir=Path(context_data_dir)
        )
    )


def _run_watch(args: argparse.Namespace) -> int:
    api_key = _api_key()
    if not api_key:
        print("DART_API_KEY not set. Add it to .env (see .env.example).")
        return 1
    settings = load_settings([Path(args.config)])
    setup_logging(settings.logging.level, settings.logging.json_format)
    policy = WatcherPolicy(
        poll_interval_seconds=args.poll_interval,
        processing_batch_size=args.batch_size,
    )
    conn = init_db(settings.storage.sqlite_path)
    try:
        with DartClient(api_key) as client:
            alert_builder = _builder(args.context_data_dir)
            registry = ExperimentRegistry(conn)
            decision_consumer = ForwardDecisionConsumer(
                registry=registry,
                ledger=ForwardDecisionLedger(conn, registry=registry),
                alert_builder=alert_builder,
            )
            alert_consumer = ResearchAlertConsumer(
                builder=alert_builder,
                sink=_console_sink,
                consumer_name="research-alert-live-v1",
            )
            watcher = LiveDartWatcher(
                source=client,
                store=LiveEventStore(conn),
                consumers=(decision_consumer, alert_consumer),
                policy=policy,
            )
            if not args.once:
                watcher.run_forever(corp_cls=args.corp_cls, target_date=args.date)
                return 0
            target = args.date or datetime.now(SEOUL).date()
            result = watcher.run_cycle(target, corp_cls=args.corp_cls)
            print(
                "Watch complete: "
                f"records={result.poll.records_seen} "
                f"new={result.poll.new_receipts} "
                f"processed={len(result.processing.processed_receipt_nos)} "
                f"failed={len(result.processing.failed_receipt_nos)} "
                f"pending={result.processing.remaining_receipts}"
            )
            return int(bool(result.processing.failed_receipt_nos))
    finally:
        conn.close()


def _run_replay(args: argparse.Namespace) -> int:
    settings = load_settings([Path(args.config)])
    setup_logging(settings.logging.level, settings.logging.json_format)
    conn = init_db(settings.storage.sqlite_path)
    try:
        store = LiveEventStore(conn)
        receipt_nos = store.receipt_nos_for_date(args.date, limit=args.limit)
        if not receipt_nos:
            print(f"No disclosures found for {args.date.isoformat()}.")
            return 0
        api_key = _api_key()
        source_context = (
            DartClient(api_key) if api_key else nullcontext(_OfflineReplaySource())
        )
        if not api_key:
            print(
                "DART_API_KEY not set; replaying local snapshots/original receipts "
                "only. Updates lacking stored relationship evidence will fail."
            )
        with source_context as source:
            consumer = ResearchAlertConsumer(
                builder=_builder(args.context_data_dir),
                sink=_console_sink,
                consumer_name=args.consumer_name,
                assessment_time=historical_replay_assessment_time,
            )
            result = LiveDartWatcher(source=source, store=store).replay(
                consumer,
                trigger_receipt_nos=receipt_nos,
            )
        print(
            "Replay complete: "
            f"date={args.date.isoformat()} "
            f"selected={len(receipt_nos)} "
            f"delivered={result.delivered} "
            f"skipped={result.skipped} "
            f"failed={len(result.failed_trigger_receipt_nos)}"
        )
        return int(bool(result.failed_trigger_receipt_nos))
    finally:
        conn.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "watch":
        return _run_watch(args)
    return _run_replay(args)


if __name__ == "__main__":
    raise SystemExit(main())
