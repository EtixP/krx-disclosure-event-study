from __future__ import annotations

import argparse
import os
import sys
from contextlib import nullcontext
from datetime import date, datetime, timedelta, timezone
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
from kdtb.data.kis_intraday import KisOpenApiClient
from kdtb.event_identity import delivery_id_for_snapshot
from kdtb.experiments import (
    ExperimentRegistry,
    ForwardDecisionConsumer,
    ForwardDecisionLedger,
    ForwardReportError,
    ForwardReportService,
    render_forward_report,
)
from kdtb.live import (
    EventEnvelope,
    IntradayCollectorPolicy,
    IntradayMarketCollector,
    LiveDartWatcher,
    MarketDataStore,
    WatcherPolicy,
)
from kdtb.live.store import LiveEventStore
from kdtb.logging_setup import setup_logging
from kdtb.schemas.alert import ResearchAlert
from kdtb.storage.db import init_db, open_readonly_db

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


def _utc_datetime_arg(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "timestamp must be an ISO-8601 UTC datetime"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must be timezone-aware UTC")
    if parsed.utcoffset() != timedelta(0):
        raise argparse.ArgumentTypeError("timestamp must use UTC")
    return parsed.astimezone(timezone.utc)


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

    report = subparsers.add_parser(
        "forward-report",
        help="Render one immutable forward experiment without modifying storage",
    )
    report.add_argument("experiment_id")
    report.add_argument("--version", type=_positive_int, required=True)
    report.add_argument(
        "--as-of",
        type=_utc_datetime_arg,
        required=True,
        help="UTC ledger cutoff; cannot be later than report generation time",
    )
    report.add_argument("--config", default="config/default.yaml")
    report.add_argument(
        "--json",
        action="store_true",
        help="Print canonical, source-embedded JSON instead of the text report",
    )

    intraday = subparsers.add_parser(
        "collect-intraday",
        help="Collect raw-first KIS minute bars for selected canonical events",
    )
    intraday.add_argument(
        "--receipt-no",
        action="append",
        default=[],
        help="Canonical trigger receipt to register; repeat for multiple events",
    )
    intraday.add_argument(
        "--target-date",
        type=_date_arg,
        help="Market date for newly selected receipts; defaults to first-seen KST date",
    )
    intraday.add_argument(
        "--retry-failed",
        action="store_true",
        help="Requeue prior failed collection targets before this run",
    )
    intraday.add_argument(
        "--refresh",
        action="store_true",
        help="Collect a later raw vintage for the selected completed targets",
    )
    intraday.add_argument("--max-targets", type=_positive_int, default=100)
    intraday.add_argument("--config", default="config/default.yaml")
    return parser


def _console_sink(_: ResearchAlert, rendered: str) -> None:
    print(rendered, flush=True)
    print(flush=True)


def _api_key() -> str | None:
    load_dotenv(".env")
    return os.getenv("DART_API_KEY")


def _kis_credentials() -> tuple[str | None, str | None]:
    load_dotenv(".env")
    return os.getenv("KIS_APP_KEY"), os.getenv("KIS_APP_SECRET")


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


def _run_forward_report(args: argparse.Namespace) -> int:
    settings = load_settings([Path(args.config)])
    conn = open_readonly_db(settings.storage.sqlite_path)
    try:
        report = ForwardReportService(conn).build(
            args.experiment_id,
            args.version,
            as_of=args.as_of,
        )
    except ForwardReportError as error:
        print(str(error), file=sys.stderr)
        return 2
    finally:
        conn.close()
    print(report.canonical_json() if args.json else render_forward_report(report))
    return 0


def _run_collect_intraday(args: argparse.Namespace) -> int:
    if args.target_date is not None and not args.receipt_no:
        print("--target-date requires at least one --receipt-no", file=sys.stderr)
        return 2
    if args.refresh and not args.receipt_no:
        print("--refresh requires at least one --receipt-no", file=sys.stderr)
        return 2
    settings = load_settings([Path(args.config)])
    conn = init_db(settings.storage.sqlite_path)
    try:
        live_store = LiveEventStore(conn)
        market_store = MarketDataStore(conn)
        selected_target_ids: list[str] = []
        for receipt_no in args.receipt_no:
            snapshot = live_store.get_snapshot(receipt_no)
            if snapshot is None:
                print(
                    f"No canonical event snapshot found for receipt {receipt_no}.",
                    file=sys.stderr,
                )
                return 2
            envelope = EventEnvelope(
                delivery_id=delivery_id_for_snapshot(
                    trigger_receipt_no=snapshot.trigger_receipt_no,
                    event_sha256=snapshot.event_sha256,
                ),
                trigger_receipt_no=snapshot.trigger_receipt_no,
                normalized_at=snapshot.normalized_at,
                event=snapshot.event,
            )
            target = market_store.register_event(
                envelope,
                registered_at=datetime.now(timezone.utc),
                target_date=args.target_date,
            )
            selected_target_ids.append(target.target_id)

        if args.refresh:
            market_store.requeue_complete(tuple(selected_target_ids))

        work_count = market_store.pending_count()
        if args.retry_failed:
            work_count += market_store.failed_count()
        if work_count == 0:
            print("Intraday collection complete: no pending targets.")
            return 0
        app_key, app_secret = _kis_credentials()
        if not app_key or not app_secret:
            print(
                "KIS_APP_KEY and KIS_APP_SECRET are required; registered targets "
                "remain pending.",
                file=sys.stderr,
            )
            return 1
        with KisOpenApiClient(app_key, app_secret) as source:
            result = IntradayMarketCollector(
                source=source,
                store=market_store,
                policy=IntradayCollectorPolicy(
                    max_targets_per_run=args.max_targets,
                ),
            ).run_pending(retry_failed=args.retry_failed)
        print(
            "Intraday collection complete: "
            f"completed={len(result.completed_target_ids)} "
            f"failed={len(result.failed_target_ids)} "
            f"observations={result.observations_seen} "
            f"remaining={result.remaining_targets} "
            f"recovered={result.recovered_runs}"
        )
        return int(bool(result.failed_target_ids))
    finally:
        conn.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "watch":
        return _run_watch(args)
    if args.command == "replay":
        return _run_replay(args)
    if args.command == "forward-report":
        return _run_forward_report(args)
    return _run_collect_intraday(args)


if __name__ == "__main__":
    raise SystemExit(main())
