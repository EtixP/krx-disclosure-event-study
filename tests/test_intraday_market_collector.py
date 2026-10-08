from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import parse_qs

import httpx
import pytest

from kdtb.data.kis_intraday import (
    KIS_MINUTE_ENDPOINT,
    KIS_PRODUCTION_BASE_URL,
    KisMinuteCapture,
    KisMinuteRequest,
    KisOpenApiClient,
)
from kdtb.event_identity import delivery_id_for_snapshot
from kdtb.events.normalizer import normalize_disclosures
from kdtb.live.dart_watcher import EventEnvelope
from kdtb.live.market_collector import (
    IntradayCollectorPolicy,
    IntradayMarketCollector,
)
from kdtb.live.market_store import MarketDataStore, MarketDataStoreError
from kdtb.live.store import LiveEventStore
from kdtb.schemas.intraday_market_data import IntradayBarObservation, MarketDataGap
from kdtb.storage.db import init_db


class TickingClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        result = self.value
        self.value += timedelta(milliseconds=1)
        return result


class FakeKisSource:
    def __init__(self, pages, *, clock: TickingClock) -> None:
        self.pages = list(pages)
        self.clock = clock
        self.calls: list[tuple[str, date, time, int]] = []

    def fetch_minute_page(self, stock_code, target_date, through_time, page_number):
        self.calls.append((stock_code, target_date, through_time, page_number))
        value = self.pages.pop(0)
        if isinstance(value, Exception):
            raise value
        request = KisMinuteRequest(
            stock_code=stock_code,
            target_date=target_date,
            through_time=through_time,
            page_number=page_number,
            requested_at=self.clock(),
        )
        raw = json.dumps(
            {"rt_cd": "0", "msg_cd": "MCA00000", "output1": {}, "output2": value},
            separators=(",", ":"),
        ).encode()
        return KisMinuteCapture(
            request=request,
            received_at=self.clock(),
            status_code=200,
            raw_content=raw,
        )


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    connection = init_db(tmp_path / "intraday.db")
    yield connection
    connection.close()


def _record(receipt_no: str, *, stock_code: str = "005930") -> dict[str, str]:
    return {
        "corp_code": "00126380",
        "corp_name": "테스트회사",
        "stock_code": stock_code,
        "corp_cls": "Y",
        "report_nm": "주요사항보고서(회사합병결정)",
        "rcept_no": receipt_no,
        "rcept_dt": receipt_no[:8],
        "rm": "",
    }


def _event_envelope(
    conn: sqlite3.Connection,
    *,
    receipt_no: str = "20260917000001",
    stock_code: str = "005930",
    observed_at: datetime = datetime(2026, 9, 17, 0, 0, tzinfo=timezone.utc),
    normalized_at: datetime = datetime(2026, 9, 17, 0, 1, tzinfo=timezone.utc),
) -> EventEnvelope:
    store = LiveEventStore(conn)
    run_id = store.start_poll_run(
        target_date=receipt_no[:8], corp_cls="Y", started_at=observed_at
    )
    store.persist_poll_page(
        poll_run_id=run_id,
        records=(_record(receipt_no, stock_code=stock_code),),
        observed_at=observed_at,
    )
    store.finish_poll_run(run_id, completed_at=observed_at)
    disclosure = store.get_disclosure(receipt_no)
    assert disclosure is not None
    event = normalize_disclosures((disclosure,)).events[0]
    snapshot = store.save_snapshot(
        trigger_receipt_no=receipt_no,
        event=event,
        normalized_at=normalized_at,
    )
    return EventEnvelope(
        delivery_id=delivery_id_for_snapshot(
            trigger_receipt_no=receipt_no,
            event_sha256=snapshot.event_sha256,
        ),
        trigger_receipt_no=receipt_no,
        normalized_at=normalized_at,
        event=event,
    )


def _bar(hour: str, *, ymd: str = "20260917", price: int = 70000) -> dict[str, str]:
    return {
        "stck_bsop_date": ymd,
        "stck_cntg_hour": hour,
        "stck_oprc": str(price - 100),
        "stck_hgpr": str(price + 100),
        "stck_lwpr": str(price - 200),
        "stck_prpr": str(price),
        "cntg_vol": "25",
        "acml_vol": "1000",
    }


def _register(conn, envelope, *, registered_at=None, target_date=None):
    return MarketDataStore(conn).register_event(
        envelope,
        registered_at=registered_at or datetime(2026, 9, 17, 0, 2, tzinfo=timezone.utc),
        target_date=target_date,
    )


def test_collects_exact_event_and_market_timestamps_raw_first_and_idempotently(conn):
    envelope = _event_envelope(conn)
    target = _register(conn, envelope)
    clock = TickingClock(datetime(2026, 9, 17, 7, 0, tzinfo=timezone.utc))
    source = FakeKisSource([[_bar("093100"), _bar("093000")]], clock=clock)
    collector = IntradayMarketCollector(
        source=source,
        store=MarketDataStore(conn),
        policy=IntradayCollectorPolicy(request_interval_seconds=0),
        clock=clock,
    )

    result = collector.run_pending()
    duplicate = collector.run_pending()

    assert result.completed_target_ids == (target.target_id,)
    assert result.observations_seen == 2
    assert duplicate.completed_target_ids == ()
    assert len(source.calls) == 1
    same_target = _register(
        conn,
        envelope,
        registered_at=datetime(2026, 9, 17, 7, 1, tzinfo=timezone.utc),
    )
    assert same_target == target
    stored_target = MarketDataStore(conn).get_target(target.target_id)
    assert stored_target is not None
    assert stored_target.source_event_date == date(2026, 9, 17)
    assert stored_target.event_observed_at == datetime(
        2026, 9, 17, 0, 0, tzinfo=timezone.utc
    )
    assert stored_target.event_normalized_at == datetime(
        2026, 9, 17, 0, 1, tzinfo=timezone.utc
    )
    capture = conn.execute(
        "SELECT raw_content, raw_sha256 FROM intraday_provider_captures"
    ).fetchone()
    assert hashlib.sha256(capture[0]).hexdigest() == capture[1]
    rows = conn.execute(
        """
        SELECT observation_json
        FROM intraday_bar_observations
        ORDER BY market_timestamp
        """
    ).fetchall()
    observations = [IntradayBarObservation.model_validate_json(row[0]) for row in rows]
    assert [item.market_timestamp.isoformat() for item in observations] == [
        "2026-09-17T09:30:00+09:00",
        "2026-09-17T09:31:00+09:00",
    ]
    assert all(item.target_id == target.target_id for item in observations)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_empty_provider_result_and_missing_symbol_are_explicit_gaps(conn):
    tradable = _event_envelope(conn, receipt_no="20260917000001")
    no_symbol = _event_envelope(conn, receipt_no="20260917000002", stock_code="")
    tradable_target = _register(conn, tradable)
    missing_target = _register(conn, no_symbol)
    clock = TickingClock(datetime(2026, 9, 17, 7, 0, tzinfo=timezone.utc))
    source = FakeKisSource([[]], clock=clock)

    result = IntradayMarketCollector(
        source=source,
        store=MarketDataStore(conn),
        policy=IntradayCollectorPolicy(request_interval_seconds=0),
        clock=clock,
    ).run_pending()

    assert result.completed_target_ids == (tradable_target.target_id,)
    assert result.observations_seen == 0
    assert len(source.calls) == 1
    rows = conn.execute(
        "SELECT target_id, reason, gap_json FROM market_data_gaps ORDER BY reason"
    ).fetchall()
    gaps = [MarketDataGap.model_validate_json(row[2]) for row in rows]
    assert {(row[0], row[1]) for row in rows} == {
        (missing_target.target_id, "missing_stock_code"),
        (tradable_target.target_id, "provider_no_rows"),
    }
    missing = next(gap for gap in gaps if gap.reason == "missing_stock_code")
    provider = next(gap for gap in gaps if gap.reason == "provider_no_rows")
    assert missing.run_id is None and missing.capture_id is None
    assert provider.run_id is not None and provider.capture_id is not None
    assert MarketDataStore(conn).requeue_complete((missing_target.target_id,)) == 0


def test_wrong_date_fails_after_preserving_raw_and_retry_succeeds(conn):
    target = _register(conn, _event_envelope(conn))
    clock = TickingClock(datetime(2026, 9, 17, 7, 0, tzinfo=timezone.utc))
    source = FakeKisSource(
        [[_bar("093000", ymd="20260916")], [_bar("093000")]],
        clock=clock,
    )
    collector = IntradayMarketCollector(
        source=source,
        store=MarketDataStore(conn),
        policy=IntradayCollectorPolicy(request_interval_seconds=0),
        clock=clock,
    )

    failed = collector.run_pending()
    retried = collector.run_pending(retry_failed=True)

    assert failed.failed_target_ids == (target.target_id,)
    assert retried.completed_target_ids == (target.target_id,)
    assert conn.execute(
        "SELECT COUNT(*) FROM intraday_provider_captures"
    ).fetchone() == (2,)
    assert conn.execute(
        "SELECT COUNT(*) FROM intraday_bar_observations"
    ).fetchone() == (1,)
    statuses = conn.execute(
        "SELECT status FROM intraday_collection_runs ORDER BY id"
    ).fetchall()
    assert statuses == [("failed",), ("succeeded",)]


def test_later_than_request_market_row_fails_after_raw_commit(conn):
    target = _register(conn, _event_envelope(conn))
    clock = TickingClock(datetime(2026, 9, 17, 7, 0, tzinfo=timezone.utc))
    result = IntradayMarketCollector(
        source=FakeKisSource([[_bar("160001")]], clock=clock),
        store=MarketDataStore(conn),
        policy=IntradayCollectorPolicy(request_interval_seconds=0),
        clock=clock,
    ).run_pending()

    assert result.failed_target_ids == (target.target_id,)
    assert conn.execute(
        "SELECT COUNT(*) FROM intraday_provider_captures"
    ).fetchone() == (1,)
    assert conn.execute(
        "SELECT COUNT(*) FROM intraday_bar_observations"
    ).fetchone() == (0,)


def test_completed_target_can_explicitly_collect_a_later_raw_vintage(conn):
    target = _register(conn, _event_envelope(conn))
    clock = TickingClock(datetime(2026, 9, 17, 7, 0, tzinfo=timezone.utc))
    source = FakeKisSource(
        [[_bar("093000")], [_bar("093100", price=70100)]], clock=clock
    )
    store = MarketDataStore(conn)
    collector = IntradayMarketCollector(
        source=source,
        store=store,
        policy=IntradayCollectorPolicy(request_interval_seconds=0),
        clock=clock,
    )

    collector.run_pending()
    assert store.requeue_complete((target.target_id,)) == 1
    refreshed = collector.run_pending()

    assert refreshed.completed_target_ids == (target.target_id,)
    assert conn.execute("SELECT COUNT(*) FROM intraday_collection_runs").fetchone() == (
        2,
    )
    assert conn.execute(
        "SELECT COUNT(*) FROM intraday_provider_captures"
    ).fetchone() == (2,)
    assert conn.execute(
        "SELECT COUNT(*) FROM intraday_bar_observations"
    ).fetchone() == (2,)


def test_interrupted_run_is_recovered_before_new_claim(conn):
    target = _register(conn, _event_envelope(conn))
    store = MarketDataStore(conn)
    work = store.claim_next(started_at=datetime(2026, 9, 17, 7, 0, tzinfo=timezone.utc))
    assert work is not None
    clock = TickingClock(datetime(2026, 9, 17, 7, 0, tzinfo=timezone.utc))
    result = IntradayMarketCollector(
        source=FakeKisSource([[_bar("093000")]], clock=clock),
        store=store,
        policy=IntradayCollectorPolicy(request_interval_seconds=0),
        clock=clock,
    ).run_pending()

    assert result.recovered_runs == 1
    assert result.completed_target_ids == (target.target_id,)
    assert conn.execute(
        "SELECT status, error_type FROM intraday_collection_runs ORDER BY id"
    ).fetchall() == [
        ("failed", "InterruptedCollection"),
        ("succeeded", None),
    ]


def test_claim_and_terminal_state_cannot_predate_target_registration(conn):
    target = _register(conn, _event_envelope(conn))
    store = MarketDataStore(conn)
    before_registration = datetime(2026, 9, 17, 0, 0, 30, tzinfo=timezone.utc)

    with pytest.raises(MarketDataStoreError, match="before target registration"):
        store.claim_next(started_at=before_registration)

    assert conn.execute("SELECT COUNT(*) FROM intraday_collection_runs").fetchone() == (
        0,
    )
    assert conn.execute("SELECT COUNT(*) FROM market_data_gaps").fetchone() == (0,)
    assert (
        conn.execute(
            """
        SELECT status, attempts, last_started_at
        FROM intraday_collection_state
        WHERE target_id = ?
        """,
            (target.target_id,),
        ).fetchone()
        == ("pending", 0, None)
    )

    registered_at = datetime(2026, 9, 17, 0, 2, tzinfo=timezone.utc)
    work = store.claim_next(started_at=registered_at)
    assert work is not None
    assert work.target == target


def test_legacy_pre_target_run_cannot_be_recovered_to_a_terminal_state(conn):
    target = _register(conn, _event_envelope(conn))
    before_registration = datetime(2026, 9, 17, 0, 0, 30, tzinfo=timezone.utc)
    with conn:
        conn.execute(
            """
            UPDATE intraday_collection_state
            SET status = 'collecting', attempts = 1, last_started_at = ?
            WHERE target_id = ?
            """,
            (before_registration.isoformat(), target.target_id),
        )
        conn.execute(
            """
            INSERT INTO intraday_collection_runs (
                target_id, started_at, initial_cursor, status
            ) VALUES (?, ?, '09:00:30', 'running')
            """,
            (target.target_id, before_registration.isoformat()),
        )

    with pytest.raises(MarketDataStoreError, match="before target registration"):
        MarketDataStore(conn).recover_interrupted(
            recovered_at=datetime(2026, 9, 17, 0, 3, tzinfo=timezone.utc)
        )

    assert conn.execute(
        "SELECT status, finished_at FROM intraday_collection_runs"
    ).fetchone() == ("running", None)
    assert conn.execute("SELECT COUNT(*) FROM market_data_gaps").fetchone() == (0,)


def test_full_page_paginates_backward_without_assuming_a_1530_close(conn):
    _register(conn, _event_envelope(conn))
    base = datetime(2026, 9, 17, 11, 0)
    first_page = [
        _bar((base - timedelta(minutes=index)).strftime("%H%M%S"), price=70000 + index)
        for index in range(120)
    ]
    second_page = [_bar("090000", price=70150)]
    clock = TickingClock(datetime(2026, 9, 17, 7, 0, tzinfo=timezone.utc))
    source = FakeKisSource([first_page, second_page], clock=clock)

    result = IntradayMarketCollector(
        source=source,
        store=MarketDataStore(conn),
        policy=IntradayCollectorPolicy(request_interval_seconds=0),
        clock=clock,
    ).run_pending()

    assert result.observations_seen == 121
    assert [call[3] for call in source.calls] == [1, 2]
    assert source.calls[1][2] == time(9, 0, 59)


def test_completion_reconciles_retained_capture_and_bars_before_terminalizing(conn):
    target = _register(conn, _event_envelope(conn))
    store = MarketDataStore(conn)
    work = store.claim_next(started_at=datetime(2026, 9, 17, 7, 0, tzinfo=timezone.utc))
    assert work is not None
    finished_at = datetime(2026, 9, 17, 7, 1, tzinfo=timezone.utc)

    with pytest.raises(MarketDataStoreError, match="at least one raw capture"):
        store.finish_run(work.run_id, finished_at=finished_at)

    clock = TickingClock(datetime(2026, 9, 17, 7, 0, tzinfo=timezone.utc))
    capture = FakeKisSource([[_bar("093000")]], clock=clock).fetch_minute_page(
        "005930", target.target_date, time(16, 0), 1
    )
    capture_id = store.persist_capture(work.run_id, capture)
    with pytest.raises(MarketDataStoreError, match="do not match"):
        store.finish_run(work.run_id, finished_at=finished_at)
    assert conn.execute("SELECT COUNT(*) FROM market_data_gaps").fetchone() == (0,)

    store.normalize_capture(capture_id)
    assert store.finish_run(work.run_id, finished_at=finished_at) == 1
    assert conn.execute(
        "SELECT observations_seen FROM intraday_collection_runs WHERE id = ?",
        (work.run_id,),
    ).fetchone() == (1,)
    with pytest.raises(MarketDataStoreError, match="running collection run"):
        store.normalize_capture(capture_id)


def test_completion_is_bound_to_the_durable_initial_cursor(conn):
    target = _register(conn, _event_envelope(conn))
    store = MarketDataStore(conn)
    work = store.claim_next(started_at=datetime(2026, 9, 17, 7, 0, tzinfo=timezone.utc))
    assert work is not None
    assert work.initial_cursor == time(16, 0)
    assert conn.execute(
        "SELECT initial_cursor FROM intraday_collection_runs WHERE id = ?",
        (work.run_id,),
    ).fetchone() == ("16:00:00",)
    with pytest.raises(sqlite3.IntegrityError, match="run identity is immutable"):
        with conn:
            conn.execute(
                "UPDATE intraday_collection_runs SET initial_cursor = '09:00:00' WHERE id = ?",
                (work.run_id,),
            )

    clock = TickingClock(datetime(2026, 9, 17, 7, 0, 1, tzinfo=timezone.utc))
    capture = FakeKisSource([[_bar("090000")]], clock=clock).fetch_minute_page(
        "005930", target.target_date, time(9, 0), 1
    )
    capture_id = store.persist_capture(work.run_id, capture)
    store.normalize_capture(capture_id)

    with pytest.raises(MarketDataStoreError, match="run initial cursor"):
        store.finish_run(
            work.run_id,
            finished_at=datetime(2026, 9, 17, 7, 0, 2, tzinfo=timezone.utc),
        )
    assert conn.execute(
        "SELECT status FROM intraday_collection_runs WHERE id = ?", (work.run_id,)
    ).fetchone() == ("running",)
    assert conn.execute("SELECT COUNT(*) FROM market_data_gaps").fetchone() == (0,)


def test_completion_and_gap_cannot_predate_retained_response(conn):
    target = _register(conn, _event_envelope(conn))
    store = MarketDataStore(conn)
    work = store.claim_next(started_at=datetime(2026, 9, 17, 7, 0, tzinfo=timezone.utc))
    assert work is not None
    request = KisMinuteRequest(
        stock_code="005930",
        target_date=target.target_date,
        through_time=work.initial_cursor,
        page_number=1,
        requested_at=datetime(2026, 9, 17, 7, 2, tzinfo=timezone.utc),
    )
    raw = json.dumps(
        {"rt_cd": "0", "msg_cd": "MCA00000", "output1": {}, "output2": []},
        separators=(",", ":"),
    ).encode()
    capture = KisMinuteCapture(
        request=request,
        received_at=datetime(2026, 9, 17, 7, 3, tzinfo=timezone.utc),
        status_code=200,
        raw_content=raw,
    )
    capture_id = store.persist_capture(work.run_id, capture)
    assert store.normalize_capture(capture_id).source_row_count == 0

    with pytest.raises(MarketDataStoreError, match="terminal timestamp predates"):
        store.finish_run(
            work.run_id,
            finished_at=datetime(2026, 9, 17, 7, 1, tzinfo=timezone.utc),
        )
    assert conn.execute("SELECT COUNT(*) FROM market_data_gaps").fetchone() == (0,)

    finished_at = datetime(2026, 9, 17, 7, 3, tzinfo=timezone.utc)
    assert store.finish_run(work.run_id, finished_at=finished_at) == 0
    assert conn.execute(
        "SELECT finished_at FROM intraday_collection_runs WHERE id = ?",
        (work.run_id,),
    ).fetchone() == (finished_at.isoformat(),)
    gap_json = conn.execute("SELECT gap_json FROM market_data_gaps").fetchone()[0]
    assert MarketDataGap.model_validate_json(gap_json).recorded_at == finished_at


def test_page_above_documented_kis_limit_fails_after_raw_commit(conn):
    target = _register(conn, _event_envelope(conn))
    base = datetime(2026, 9, 17, 11, 0)
    oversized_page = [
        _bar(
            (base - timedelta(seconds=index)).strftime("%H%M%S"),
            price=70000 + index,
        )
        for index in range(121)
    ]
    clock = TickingClock(datetime(2026, 9, 17, 7, 0, tzinfo=timezone.utc))

    result = IntradayMarketCollector(
        source=FakeKisSource([oversized_page], clock=clock),
        store=MarketDataStore(conn),
        policy=IntradayCollectorPolicy(request_interval_seconds=0),
        clock=clock,
    ).run_pending()

    assert result.failed_target_ids == (target.target_id,)
    assert conn.execute(
        "SELECT COUNT(*) FROM intraday_provider_captures"
    ).fetchone() == (1,)
    assert conn.execute(
        "SELECT COUNT(*) FROM intraday_bar_observations"
    ).fetchone() == (0,)
    error = conn.execute(
        "SELECT error_message FROM intraday_collection_runs"
    ).fetchone()[0]
    assert "documented 120-row limit" in error


def test_future_target_and_storage_mutation_fail_closed(conn):
    envelope = _event_envelope(conn)
    with pytest.raises(ValueError, match="future date"):
        _register(conn, envelope, target_date=date(2026, 9, 18))

    target = _register(conn, envelope)
    with pytest.raises(sqlite3.IntegrityError, match="targets are immutable"):
        with conn:
            conn.execute(
                "UPDATE intraday_collection_targets SET target_date = '2026-09-16'"
            )
    with pytest.raises(sqlite3.IntegrityError, match="targets are immutable"):
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO intraday_collection_targets SELECT * FROM intraday_collection_targets WHERE target_id = ?",
                (target.target_id,),
            )


def test_target_rejects_first_seen_time_inconsistent_with_raw_observation(conn):
    envelope = _event_envelope(conn)
    with conn:
        conn.execute(
            """
            UPDATE live_receipt_processing
            SET first_seen_at = '2026-09-17T00:00:01+00:00'
            WHERE receipt_no = ?
            """,
            (envelope.trigger_receipt_no,),
        )
    with pytest.raises(RuntimeError, match="retained raw observations"):
        _register(conn, envelope)


def test_kis_client_uses_verified_endpoint_and_keeps_secrets_out_of_capture():
    with pytest.raises(ValueError, match="finite and positive"):
        KisOpenApiClient("APPKEY", "APPSECRET", timeout_seconds=float("nan"))
    with pytest.raises(ValueError, match="positive integer"):
        KisMinuteRequest(
            stock_code="005930",
            target_date=date(2026, 9, 17),
            through_time=time(16, 0),
            page_number=1.5,  # type: ignore[arg-type]
            requested_at=datetime(2026, 9, 17, 7, 0, tzinfo=timezone.utc),
        )
    requests: list[httpx.Request] = []
    response_bytes = json.dumps(
        {"rt_cd": "0", "msg_cd": "MCA00000", "output1": {}, "output2": []},
        separators=(",", ":"),
    ).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/oauth2/tokenP":
            return httpx.Response(200, json={"access_token": "TOKEN"})
        return httpx.Response(200, content=response_bytes)

    clock = TickingClock(datetime(2026, 9, 17, 7, 0, tzinfo=timezone.utc))
    http_client = httpx.Client(
        base_url=KIS_PRODUCTION_BASE_URL,
        transport=httpx.MockTransport(handler),
    )
    client = KisOpenApiClient(
        "APPKEY",
        "APPSECRET",
        client=http_client,
        clock=clock,
    )
    capture = client.fetch_minute_page("005930", date(2026, 9, 17), time(16, 0), 1)
    http_client.close()

    assert [request.url.path for request in requests] == [
        "/oauth2/tokenP",
        KIS_MINUTE_ENDPOINT,
    ]
    query = parse_qs(requests[1].url.query.decode())
    assert query["FID_INPUT_DATE_1"] == ["20260917"]
    assert query["FID_INPUT_HOUR_1"] == ["160000"]
    assert requests[1].headers["tr_id"] == "FHKST03010230"
    assert capture.raw_content == response_bytes
    assert "APPKEY" not in capture.request.canonical_json()
    assert "APPSECRET" not in capture.request.canonical_json()


def test_cli_exposes_selected_event_collector():
    from kdtb.cli import build_parser

    args = build_parser().parse_args(
        [
            "collect-intraday",
            "--receipt-no",
            "20260917000001",
            "--target-date",
            "2026-09-17",
            "--retry-failed",
            "--refresh",
            "--max-targets",
            "5",
        ]
    )
    assert args.command == "collect-intraday"
    assert args.receipt_no == ["20260917000001"]
    assert args.target_date == date(2026, 9, 17)
    assert args.retry_failed is True
    assert args.refresh is True
    assert args.max_targets == 5
