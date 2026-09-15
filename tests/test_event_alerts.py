from __future__ import annotations

import ast
import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from pydantic import ValidationError

from kdtb.alerts import (
    ResearchAlertBuilder,
    ResearchAlertConsumer,
    historical_replay_assessment_time,
    render_alert,
)
from kdtb.cli import build_parser, main
from kdtb.context import HistoricalContextPolicy, HistoricalContextService
from kdtb.data.benchmarks import BENCHMARK_SOURCE
from kdtb.data.disclosure_store import DisclosureStore
from kdtb.event_identity import delivery_id_for_event
from kdtb.live.dart_watcher import EventEnvelope, LiveDartWatcher
from kdtb.live.store import LiveEventStore
from kdtb.schemas.alert import ResearchAlert
from kdtb.schemas.economic_event import (
    EconomicEvent,
    EventIssuer,
    EventSourceProvenance,
)
from kdtb.storage.db import init_db


def _context_data(tmp_path):
    data_dir = tmp_path / "context"
    data_dir.mkdir()
    rows = [
        {
            "receipt_no": "20240101000001",
            "corp_code": "00000001",
            "corp_name": "과거회사1",
            "stock_code": "000001",
            "report_name": "단일판매ㆍ공급계약체결",
            "event_date": "2024-01-01",
            "market": "KOSPI",
            "t+1_date": "2024-01-02",
            "t+5_date": "2024-01-08",
            "t+1_close": 100.0,
            "t+5_close": 110.0,
            "benchmark_source": BENCHMARK_SOURCE,
            "benchmark_symbol": "KOSPI",
            "benchmark_t1_close": 1_000.0,
            "benchmark_t5_close": 1_020.0,
            "benchmark_alignment": "complete",
        },
        {
            "receipt_no": "20240102000001",
            "corp_code": "00000002",
            "corp_name": "과거회사2",
            "stock_code": "000002",
            "report_name": "단일판매ㆍ공급계약체결",
            "event_date": "2024-01-02",
            "market": "KOSPI",
            "t+1_date": "2024-01-03",
            "t+5_date": "2024-01-09",
            "t+1_close": 100.0,
            "t+5_close": 95.0,
            "benchmark_source": BENCHMARK_SOURCE,
            "benchmark_symbol": "KOSPI",
            "benchmark_t1_close": 1_000.0,
            "benchmark_t5_close": 1_010.0,
            "benchmark_alignment": "complete",
        },
        {
            "receipt_no": "20240103000001",
            "corp_code": "00000003",
            "corp_name": "미래결과회사",
            "stock_code": "000003",
            "report_name": "단일판매ㆍ공급계약체결",
            "event_date": "2024-01-03",
            "market": "KOSPI",
            "t+1_date": "2024-01-04",
            "t+5_date": "2024-01-20",
            "t+1_close": 100.0,
            "t+5_close": 150.0,
            "benchmark_source": BENCHMARK_SOURCE,
            "benchmark_symbol": "KOSPI",
            "benchmark_t1_close": 1_000.0,
            "benchmark_t5_close": 1_100.0,
            "benchmark_alignment": "complete",
        },
    ]
    pd.DataFrame(rows).to_csv(
        data_dir / "event_study_supply_contract.csv",
        index=False,
    )
    return data_dir


def _event(
    *,
    event_type: str = "major_supply_contract",
    lineage_status: str = "self_contained",
    normalized_fields: dict | None = None,
) -> EconomicEvent:
    receipt_no = "20240120000001"
    receipt_time = datetime(2024, 1, 20)
    return EconomicEvent(
        economic_event_id=f"dart:{receipt_no}",
        event_type=event_type,
        issuer=EventIssuer(
            corp_code="00126380",
            corp_name="테스트회사",
            stock_code="005930",
        ),
        market="KOSPI",
        primary_receipt_no=receipt_no,
        original_timestamp=receipt_time,
        latest_update_timestamp=receipt_time,
        status="active",
        normalized_fields=normalized_fields or {},
        source_provenance=(
            EventSourceProvenance(
                receipt_no=receipt_no,
                report_name="단일판매ㆍ공급계약체결",
                receipt_timestamp=receipt_time,
                source="DART",
                action="original",
                relationship="primary",
                raw_payload_sha256="a" * 64,
            ),
        ),
        lineage_status=lineage_status,
    )


def _envelope(event: EconomicEvent) -> EventEnvelope:
    trigger_receipt_no = event.primary_receipt_no
    return EventEnvelope(
        delivery_id=delivery_id_for_event(
            trigger_receipt_no=trigger_receipt_no,
            event=event,
        ),
        trigger_receipt_no=trigger_receipt_no,
        normalized_at=datetime(2024, 1, 20, 12, tzinfo=timezone.utc),
        event=event,
    )


def _builder(tmp_path) -> ResearchAlertBuilder:
    return ResearchAlertBuilder(
        historical_context_service=HistoricalContextService(
            data_dir=_context_data(tmp_path),
            policy=HistoricalContextPolicy(n_resamples=100, random_state=7),
        )
    )


def test_alert_is_deterministic_structured_and_useful_without_dashboard(tmp_path):
    event = _event(
        normalized_fields={
            "contract_value_krw": 12_000_000_000,
            "prior_year_revenue_krw": 100_000_000_000,
            "contract_to_revenue_ratio": 0.12,
        }
    )
    envelope = _envelope(event)
    builder = _builder(tmp_path)

    first = builder.build(envelope)
    second = builder.build(envelope)
    rendered = render_alert(first)

    assert first == second
    assert ResearchAlert.model_validate_json(first.model_dump_json()) == first
    assert first.generation_method == "deterministic_structured_formatter"
    assert [field.key for field in first.important_fields] == [
        "contract_value_krw",
        "prior_year_revenue_krw",
        "contract_to_revenue_ratio",
    ]
    assert first.significance.status == "measured"
    assert first.historical_context.status == "available"
    assert first.historical_context.result is not None
    assert first.historical_context.result.metrics is not None
    assert first.strategy.status == "NO_REGISTERED_STRATEGY"
    assert first.strategy.action == "NO_TRADE"
    assert first.is_trade_recommendation is False
    assert first.trading_recommendation is None
    with pytest.raises(ValidationError, match="frozen"):
        first.assessed_at = datetime(2024, 1, 21, tzinfo=timezone.utc)
    assert render_alert(second) == rendered
    assert "Issuer: 테스트회사 (005930, KOSPI; corp 00126380)" in rendered
    assert "Contract value: ₩12,000,000,000" in rendered
    assert "Contract / revenue: 12.00%" in rendered
    assert "Comparable sample: n=2, issuers=2" in rendered
    assert "issuer-clustered CI" in rendered
    assert "NO REGISTERED STRATEGY" in rendered
    assert "NO TRADE" in rendered


def test_alert_rejects_arbitrary_explanation_text(tmp_path):
    alert = _builder(tmp_path).build(
        _envelope(
            _event(
                normalized_fields={
                    "contract_value_krw": 12,
                    "prior_year_revenue_krw": 100,
                    "contract_to_revenue_ratio": 0.12,
                }
            )
        )
    )
    payload = alert.model_dump(mode="json")
    payload["significance"]["explanation"] = ["INJECTED ARBITRARY PROSE"]
    with pytest.raises(ValidationError, match="verified structured engine"):
        ResearchAlert.model_validate_json(json.dumps(payload))


def test_alert_rejects_structured_sections_from_another_event(tmp_path):
    alert = _builder(tmp_path).build(
        _envelope(
            _event(
                normalized_fields={
                    "contract_value_krw": 12,
                    "prior_year_revenue_krw": 100,
                    "contract_to_revenue_ratio": 0.12,
                }
            )
        )
    )
    payload = alert.model_dump(mode="json")
    payload["important_fields"][2]["value"] = 0.13

    with pytest.raises(ValidationError, match="important fields"):
        ResearchAlert.model_validate_json(json.dumps(payload))

    payload = alert.model_dump(mode="json")
    payload["significance"]["measurements"][0]["value"] = 0.13
    payload["important_fields"][2]["value"] = 0.13
    with pytest.raises(ValidationError, match="verified structured engine"):
        ResearchAlert.model_validate_json(json.dumps(payload))

    payload = alert.model_dump(mode="json")
    payload["historical_context"] = {
        "status": "unavailable",
        "unavailable_reason": "unsupported_event_type",
        "detail": "forged",
        "result": None,
    }
    with pytest.raises(ValidationError, match="unavailability reason"):
        ResearchAlert.model_validate_json(json.dumps(payload))


def test_alert_rejects_delivery_id_for_substituted_event(tmp_path):
    alert = _builder(tmp_path).build(_envelope(_event()))

    payload = alert.model_dump(mode="json")
    payload["event"]["issuer"] = {
        "corp_code": "99999999",
        "corp_name": "위조회사",
        "stock_code": "999999",
    }
    with pytest.raises(ValidationError, match="delivery ID must match"):
        ResearchAlert.model_validate_json(json.dumps(payload))


def test_alert_rejects_arbitrary_shape_valid_delivery_id(tmp_path):
    alert = _builder(tmp_path).build(_envelope(_event()))
    payload = alert.model_dump(mode="json")
    payload["delivery_id"] = "f" * 64
    with pytest.raises(ValidationError, match="delivery ID must match"):
        ResearchAlert.model_validate_json(json.dumps(payload))


def test_alert_rejects_delivery_id_for_another_related_trigger(tmp_path):
    related_receipt_no = "20240120000002"
    event = _event(lineage_status="partial").model_copy(
        update={"related_receipt_nos": (related_receipt_no,)}
    )
    alert = _builder(tmp_path).build(_envelope(event))
    payload = alert.model_dump(mode="json")
    payload["trigger_receipt_no"] = related_receipt_no

    with pytest.raises(ValidationError, match="delivery ID must match"):
        ResearchAlert.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize(
    ("event_type", "lineage_status", "reason"),
    [
        ("other", "self_contained", "unsupported_event_type"),
        ("major_supply_contract", "partial", "incomplete_lineage"),
    ],
)
def test_alert_keeps_expected_context_unavailability_explicit(
    tmp_path, event_type, lineage_status, reason
):
    alert = _builder(tmp_path).build(
        _envelope(_event(event_type=event_type, lineage_status=lineage_status))
    )

    assert alert.historical_context.status == "unavailable"
    assert alert.historical_context.unavailable_reason == reason
    assert alert.strategy.action == "NO_TRADE"
    rendered = render_alert(alert)
    assert f"Unavailable [{reason}]" in rendered
    assert "NO REGISTERED STRATEGY" in rendered


class _FakeSource:
    def __init__(self, records):
        self.records = records

    def iter_disclosure_pages(self, target_date, corp_cls=None, page_count=100):
        yield tuple(self.records)

    def fetch_report_relations_capture(self, rcept_no):
        raise AssertionError("original receipt must not fetch relationships")


def _record(receipt_no: str) -> dict[str, str]:
    return {
        "corp_code": "00126380",
        "corp_name": "테스트회사",
        "stock_code": "005930",
        "corp_cls": "Y",
        "report_nm": "단일판매ㆍ공급계약체결",
        "rcept_no": receipt_no,
        "rcept_dt": receipt_no[:8],
        "rm": "",
    }


def test_live_and_historical_replay_share_snapshot_consumer_and_cutoffs(tmp_path):
    conn = init_db(tmp_path / "live.db")
    try:
        receipt_no = "20240120000001"
        builder = _builder(tmp_path)
        live_alerts = []
        replay_alerts = []
        live_consumer = ResearchAlertConsumer(
            builder=builder,
            sink=lambda alert, rendered: live_alerts.append((alert, rendered)),
            consumer_name="alert-live-test",
        )
        source = _FakeSource([_record(receipt_no)])
        now = datetime(2024, 1, 20, 12, tzinfo=timezone.utc)
        watcher = LiveDartWatcher(
            source=source,
            store=LiveEventStore(conn),
            consumers=(live_consumer,),
            clock=lambda: now,
        )

        cycle = watcher.run_cycle(date(2024, 1, 20), corp_cls="Y")
        replay_consumer = ResearchAlertConsumer(
            builder=builder,
            sink=lambda alert, rendered: replay_alerts.append((alert, rendered)),
            consumer_name="alert-replay-test",
            assessment_time=historical_replay_assessment_time,
        )
        replay = watcher.replay(
            replay_consumer,
            trigger_receipt_nos=(receipt_no,),
        )

        assert cycle.processing.processed_receipt_nos == (receipt_no,)
        assert replay.delivered == 1
        assert len(live_alerts) == len(replay_alerts) == 1
        live_alert = live_alerts[0][0]
        replay_alert = replay_alerts[0][0]
        assert replay_alert.event == live_alert.event
        assert replay_alert.delivery_id == live_alert.delivery_id
        assert replay_alert.assessed_at.isoformat() == "2024-01-20T00:00:00+09:00"
        assert replay_alert.assessed_at < replay_alert.processed_at
        assert (
            replay_alert.historical_context.result.selection
            == live_alert.historical_context.result.selection
        )
        assert (
            "20240103000001"
            not in replay_alert.historical_context.result.selection.selected_receipt_nos
        )
        assert replay_alert.strategy == live_alert.strategy
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM event_deliveries WHERE status = 'succeeded'"
            ).fetchone()[0]
            == 2
        )
    finally:
        conn.close()


def test_date_replay_selection_is_sorted_bounded_and_read_only(tmp_path):
    conn = init_db(tmp_path / "replay.db")
    try:
        DisclosureStore(conn).ingest_records(
            [
                _record("20240120000002"),
                _record("20240119000001"),
                _record("20240120000001"),
            ]
        )
        store = LiveEventStore(conn)

        assert store.receipt_nos_for_date(date(2024, 1, 20)) == (
            "20240120000001",
            "20240120000002",
        )
        assert store.receipt_nos_for_date(date(2024, 1, 20), limit=1) == (
            "20240120000001",
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM live_receipt_processing").fetchone()[0]
            == 0
        )
        with pytest.raises(ValueError, match="positive integer"):
            store.receipt_nos_for_date(date(2024, 1, 20), limit=0)
    finally:
        conn.close()


def test_cli_exposes_watch_and_date_replay_commands():
    watch = build_parser().parse_args(["watch", "--once", "--date", "2024-01-20"])
    replay = build_parser().parse_args(["replay", "2024-01-20", "--limit", "5"])

    assert watch.command == "watch"
    assert watch.date == date(2024, 1, 20)
    assert replay.command == "replay"
    assert replay.date == date(2024, 1, 20)
    assert replay.limit == 5

    with pytest.raises(SystemExit):
        build_parser().parse_args(["replay", "2024-01-20", "--limit", "0"])


def test_cli_replays_local_original_receipt_without_api_key(
    tmp_path, monkeypatch, capsys
):
    database_path = tmp_path / "cli.db"
    conn = init_db(database_path)
    try:
        DisclosureStore(conn).ingest_records([_record("20240120000001")])
    finally:
        conn.close()
    data_dir = _context_data(tmp_path)
    settings = SimpleNamespace(
        storage=SimpleNamespace(sqlite_path=str(database_path)),
        logging=SimpleNamespace(level="INFO", json_format=False),
    )
    monkeypatch.setattr("kdtb.cli.load_settings", lambda _: settings)
    monkeypatch.setattr("kdtb.cli._api_key", lambda: None)

    exit_code = main(
        [
            "replay",
            "2024-01-20",
            "--context-data-dir",
            str(data_dir),
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "KDTB EVENT ALERT" in output
    assert "NO REGISTERED STRATEGY" in output
    assert "Replay complete: date=2024-01-20 selected=1 delivered=1" in output


def test_alert_path_has_no_llm_or_legacy_strategy_dependency():
    project_root = Path(__file__).resolve().parents[1]
    module_paths = (
        project_root / "src/kdtb/alerts/service.py",
        project_root / "src/kdtb/schemas/alert.py",
        project_root / "src/kdtb/cli.py",
    )
    imported: set[str] = set()
    for module_path in module_paths:
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)

    forbidden = (
        "kdtb.interpretation.llm",
        "kdtb.strategy",
        "kdtb.risk",
        "kdtb.schemas.signal",
        "broker",
    )
    assert not any(
        dependency.startswith(prefix) for dependency in imported for prefix in forbidden
    )
