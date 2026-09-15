from __future__ import annotations

import ast
import hashlib
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from kdtb.data.dart_client import (
    DartApiError,
    DartRelationshipCapture,
    parse_report_relations_html,
)
from kdtb.data.disclosure_store import DisclosureStore
from kdtb.events.normalizer import normalize_disclosures
from kdtb.live.dart_watcher import EventEnvelope, LiveDartWatcher, WatcherPolicy
from kdtb.live.store import LiveEventStore
from kdtb.schemas.economic_event import DartReportRelations
from kdtb.storage.db import init_db


class AdvancingClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        result = self.value
        self.value += timedelta(milliseconds=1)
        return result


class FakeDartSource:
    def __init__(
        self,
        pages: list[tuple[dict, ...]],
        *,
        relationships: dict[str, DartRelationshipCapture] | None = None,
        fail_after_pages: int | None = None,
    ) -> None:
        self.pages = pages
        self.relationships = relationships or {}
        self.fail_after_pages = fail_after_pages
        self.list_calls: list[tuple[date, str | None, int]] = []
        self.relationship_calls: list[str] = []

    def iter_disclosure_pages(
        self,
        target_date: date,
        corp_cls: str | None = None,
        page_count: int = 100,
    ):
        self.list_calls.append((target_date, corp_cls, page_count))
        for index, page in enumerate(self.pages, start=1):
            yield page
            if self.fail_after_pages == index:
                raise DartApiError("020", "request limit exceeded")

    def fetch_report_relations_capture(self, rcept_no: str) -> DartRelationshipCapture:
        self.relationship_calls.append(rcept_no)
        return self.relationships[rcept_no]


class RecordingConsumer:
    def __init__(
        self,
        consumer_name: str,
        *,
        fail_attempts: int = 0,
        before_consume=None,
    ) -> None:
        self.consumer_name = consumer_name
        self.fail_attempts = fail_attempts
        self.before_consume = before_consume
        self.envelopes: list[EventEnvelope] = []
        self.attempts = 0

    def consume(self, envelope: EventEnvelope) -> None:
        self.attempts += 1
        self.envelopes.append(envelope)
        if self.before_consume is not None:
            self.before_consume(envelope)
        if self.attempts <= self.fail_attempts:
            raise RuntimeError("synthetic downstream failure")


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    connection = init_db(tmp_path / "live.db")
    yield connection
    connection.close()


def _record(
    receipt_no: str,
    report_name: str = "주요사항보고서(회사합병결정)",
    *,
    rm: str = "",
) -> dict:
    return {
        "corp_code": "00126380",
        "corp_name": "테스트회사",
        "stock_code": "005930",
        "corp_cls": "Y",
        "report_nm": report_name,
        "rcept_no": receipt_no,
        "rcept_dt": receipt_no[:8],
        "rm": rm,
    }


def _relationship_capture(
    receipt_no: str,
    *,
    family: tuple[str, ...] = (),
    attachments: tuple[str, ...] = (),
    related: tuple[str, ...] = (),
) -> DartRelationshipCapture:
    selects = []
    for select_id, values in (
        ("family", family),
        ("att", attachments),
        ("ref", related),
    ):
        options = "".join(
            f'<option value="rcpNo={value}">{value}</option>' for value in values
        )
        selects.append(f'<select id="{select_id}">{options}</select>')
    html = "".join(selects)
    raw_content = html.encode("utf-8")
    relations = parse_report_relations_html(
        html,
        receipt_no,
        source_url=("https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + receipt_no),
        raw_html_sha256=hashlib.sha256(raw_content).hexdigest(),
    )
    return DartRelationshipCapture(relations=relations, raw_content=raw_content)


def _watcher(
    conn: sqlite3.Connection,
    source: FakeDartSource,
    *,
    consumers=(),
    clock=None,
) -> LiveDartWatcher:
    return LiveDartWatcher(
        source=source,
        store=LiveEventStore(conn),
        consumers=consumers,
        clock=clock or AdvancingClock(),
    )


def test_polling_is_idempotent_and_preserves_changed_raw_observations(conn):
    first = _record("20260907000001")
    second = _record("20260907000002")
    source = FakeDartSource([(first,), (second,)])
    watcher = _watcher(conn, source)

    initial = watcher.poll_once(date(2026, 9, 7), corp_cls="Y")
    duplicate = watcher.poll_once(date(2026, 9, 7), corp_cls="Y")

    assert initial.pages_fetched == 2
    assert initial.records_seen == 2
    assert initial.new_receipts == 2
    assert initial.new_observations == 2
    assert duplicate.new_receipts == 0
    assert duplicate.new_observations == 0
    assert source.list_calls == [
        (date(2026, 9, 7), "Y", 100),
        (date(2026, 9, 7), "Y", 100),
    ]

    changed = dict(first, rm="정")
    source.pages = [(changed,)]
    changed_result = watcher.poll_once(date(2026, 9, 7), corp_cls="Y")

    assert changed_result.new_receipts == 0
    assert changed_result.new_observations == 1
    assert conn.execute("SELECT COUNT(*) FROM disclosures").fetchone()[0] == 2
    assert (
        conn.execute("SELECT COUNT(*) FROM dart_disclosure_observations").fetchone()[0]
        == 3
    )
    stored = LiveEventStore(conn).get_disclosure(first["rcept_no"])
    assert stored is not None
    assert stored.raw_payload["rm"] == ""


def test_raw_and_snapshot_are_committed_before_delivery(conn):
    receipt_no = "20260907000001"
    source = FakeDartSource([(_record(receipt_no),)])

    def assert_committed(envelope: EventEnvelope) -> None:
        assert envelope.trigger_receipt_no == receipt_no
        observation = conn.execute(
            """
            SELECT observed_at, raw_payload_sha256
            FROM dart_disclosure_observations
            WHERE receipt_no = ?
            """,
            (receipt_no,),
        ).fetchone()
        assert observation is not None
        assert datetime.fromisoformat(observation[0]).tzinfo is not None
        assert observation[1] == envelope.event.source_provenance[0].raw_payload_sha256
        assert envelope.event.source_provenance[0].receipt_timestamp == datetime(
            2026, 9, 7
        )
        assert envelope.normalized_at.tzinfo is not None
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM canonical_event_snapshots WHERE trigger_receipt_no = ?",
                (receipt_no,),
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT status FROM event_deliveries WHERE consumer_name = ? AND trigger_receipt_no = ?",
                ("capture", receipt_no),
            ).fetchone()[0]
            == "delivering"
        )

    consumer = RecordingConsumer("capture", before_consume=assert_committed)
    watcher = _watcher(conn, source, consumers=(consumer,))

    first = watcher.run_cycle(date(2026, 9, 7))
    second = watcher.run_cycle(date(2026, 9, 7))

    assert first.processing.processed_receipt_nos == (receipt_no,)
    assert second.poll.new_receipts == 0
    assert consumer.attempts == 1
    assert consumer.envelopes[0].event.lineage_status == "self_contained"


def test_attachment_relationship_bytes_are_saved_before_normalization(conn):
    original_no = "20260901000001"
    correction_no = "20260907000002"
    capture = _relationship_capture(
        correction_no,
        family=(original_no,),
        attachments=(correction_no, original_no),
    )
    source = FakeDartSource(
        [
            (
                _record(original_no),
                _record(correction_no, "[첨부정정]주요사항보고서(회사합병결정)"),
            )
        ],
        relationships={correction_no: capture},
    )
    consumer = RecordingConsumer("capture")
    watcher = _watcher(conn, source, consumers=(consumer,))

    result = watcher.run_cycle(date(2026, 9, 7))

    assert result.processing.failed_receipt_nos == ()
    assert source.relationship_calls == [correction_no]
    raw_html, raw_hash = conn.execute(
        """
        SELECT raw_html, raw_html_sha256
        FROM dart_relationship_captures
        WHERE receipt_no = ?
        """,
        (correction_no,),
    ).fetchone()
    assert bytes(raw_html) == capture.raw_content
    assert hashlib.sha256(bytes(raw_html)).hexdigest() == raw_hash
    update = LiveEventStore(conn).get_snapshot(correction_no)
    assert update is not None
    assert update.event.primary_receipt_no == original_no
    assert update.event.lineage_status == "complete"
    update_source = next(
        item
        for item in update.event.source_provenance
        if item.receipt_no == correction_no
    )
    assert update_source.relationship == "dart_attachment"
    assert update_source.relationship_evidence_sha256 == raw_hash
    assert LiveEventStore(conn).resolve_event_id(f"dart:{correction_no}") == (
        f"dart:{original_no}"
    )


def test_corrupted_relationship_capture_cannot_reenter_normalization(conn):
    correction_no = "20260907000002"
    capture = _relationship_capture(
        correction_no,
        attachments=(correction_no, "20260901000001"),
    )
    store = LiveEventStore(conn)
    store.save_relationship_capture(
        capture,
        fetched_at=datetime(2026, 9, 7, 12, tzinfo=timezone.utc),
    )
    conn.execute(
        "UPDATE dart_relationship_captures SET raw_html = ? WHERE receipt_no = ?",
        (b"tampered", correction_no),
    )
    conn.commit()

    with pytest.raises(ValueError, match="evidence hash mismatch"):
        store.get_latest_relationship(correction_no)


def test_relationship_capture_rejects_lineage_not_parsed_from_its_bytes(conn):
    correction_no = "20260907000002"
    capture = _relationship_capture(
        correction_no,
        attachments=(correction_no, "20260901000001"),
    )
    inconsistent = DartReportRelations(
        receipt_no=correction_no,
        family_receipt_nos=(correction_no, "20260801000003"),
        source_url=capture.relations.source_url,
        raw_html_sha256=capture.relations.raw_html_sha256,
    )

    with pytest.raises(ValueError, match="does not match captured HTML"):
        LiveEventStore(conn).save_relationship_capture(
            DartRelationshipCapture(
                relations=inconsistent,
                raw_content=capture.raw_content,
            ),
            fetched_at=datetime(2026, 9, 7, 12, tzinfo=timezone.utc),
        )

    assert (
        conn.execute("SELECT COUNT(*) FROM dart_relationship_captures").fetchone()[0]
        == 0
    )


def test_altered_relationship_json_cannot_reenter_normalization(conn):
    correction_no = "20260907000002"
    capture = _relationship_capture(
        correction_no,
        attachments=(correction_no, "20260901000001"),
    )
    store = LiveEventStore(conn)
    store.save_relationship_capture(
        capture,
        fetched_at=datetime(2026, 9, 7, 12, tzinfo=timezone.utc),
    )
    altered = DartReportRelations(
        receipt_no=correction_no,
        family_receipt_nos=(correction_no, "20260801000003"),
        source_url=capture.relations.source_url,
        raw_html_sha256=capture.relations.raw_html_sha256,
    )
    conn.execute(
        "UPDATE dart_relationship_captures SET relations_json = ? WHERE receipt_no = ?",
        (altered.model_dump_json(), correction_no),
    )
    conn.commit()

    with pytest.raises(ValueError, match="does not match captured HTML"):
        store.get_latest_relationship(correction_no)


def test_corrupted_canonical_snapshot_cannot_be_delivered(conn):
    receipt_no = "20260907000001"
    source = FakeDartSource([(_record(receipt_no),)])
    watcher = _watcher(conn, source)
    watcher.run_cycle(date(2026, 9, 7))
    conn.execute(
        """
        UPDATE canonical_event_snapshots
        SET event_json = replace(event_json, 'self_contained', 'unresolved')
        WHERE trigger_receipt_no = ?
        """,
        (receipt_no,),
    )
    conn.commit()

    replay = watcher.replay(
        RecordingConsumer("tamper-check"),
        trigger_receipt_nos=(receipt_no,),
    )

    assert replay.failed_trigger_receipt_nos == (receipt_no,)


def test_failed_consumer_retries_but_successful_consumer_is_not_repeated(conn):
    receipt_no = "20260907000001"
    source = FakeDartSource([(_record(receipt_no),)])
    successful = RecordingConsumer("successful")
    flaky = RecordingConsumer("flaky", fail_attempts=1)
    watcher = _watcher(conn, source, consumers=(successful, flaky))

    watcher.poll_once(date(2026, 9, 7))
    first = watcher.process_pending()
    second = watcher.process_pending()

    assert first.failed_receipt_nos == (receipt_no,)
    assert second.processed_receipt_nos == (receipt_no,)
    assert successful.attempts == 1
    assert flaky.attempts == 2
    assert flaky.envelopes[0].delivery_id == flaky.envelopes[1].delivery_id
    statuses = conn.execute(
        "SELECT consumer_name, status, attempts FROM event_deliveries ORDER BY consumer_name"
    ).fetchall()
    assert statuses == [("flaky", "succeeded", 2), ("successful", "succeeded", 1)]


def test_relationship_failure_retries_from_persisted_raw_receipt(conn):
    original_no = "20260901000001"
    correction_no = "20260907000002"
    source = FakeDartSource(
        [
            (
                _record(original_no),
                _record(correction_no, "[기재정정]주요사항보고서(회사합병결정)"),
            )
        ]
    )
    consumer = RecordingConsumer("capture")
    watcher = _watcher(conn, source, consumers=(consumer,))
    watcher.poll_once(date(2026, 9, 7))

    first = watcher.process_pending()
    assert first.failed_receipt_nos == (correction_no,)
    assert (
        conn.execute(
            """
        SELECT COUNT(*)
        FROM dart_disclosure_observations
        WHERE receipt_no = ?
        """,
            (correction_no,),
        ).fetchone()[0]
        == 1
    )

    source.relationships[correction_no] = _relationship_capture(
        correction_no,
        family=(original_no, correction_no),
    )
    second = watcher.process_pending()

    assert second.processed_receipt_nos == (correction_no,)
    assert source.relationship_calls == [correction_no, correction_no]
    assert (
        conn.execute(
            """
        SELECT state, attempts
        FROM live_receipt_processing
        WHERE receipt_no = ?
        """,
            (correction_no,),
        ).fetchone()
        == ("processed", 2)
    )


def test_restart_recovers_interrupted_receipt_and_delivery(conn):
    receipt_no = "20260907000001"
    source = FakeDartSource([(_record(receipt_no),)])
    clock = AdvancingClock()
    store = LiveEventStore(conn)
    watcher = LiveDartWatcher(source=source, store=store, clock=clock)
    watcher.poll_once(date(2026, 9, 7))

    store.mark_receipt_processing(receipt_no, attempted_at=clock())
    disclosure = store.get_disclosure(receipt_no)
    assert disclosure is not None
    event = normalize_disclosures([disclosure]).events[0]
    snapshot = store.save_snapshot(
        trigger_receipt_no=receipt_no,
        event=event,
        normalized_at=clock(),
    )
    delivery_id = LiveDartWatcher._delivery_id(snapshot)
    assert store.begin_delivery(
        consumer_name="restart",
        trigger_receipt_no=receipt_no,
        delivery_id=delivery_id,
        attempted_at=clock(),
    )
    abandoned_poll_id = store.start_poll_run(
        target_date="2026-09-07",
        corp_cls=None,
        started_at=clock(),
    )

    consumer = RecordingConsumer("restart")
    restarted = LiveDartWatcher(
        source=source,
        store=LiveEventStore(conn),
        consumers=(consumer,),
        clock=clock,
    )
    result = restarted.process_pending()

    assert result.processed_receipt_nos == (receipt_no,)
    assert consumer.attempts == 1
    assert (
        conn.execute(
            "SELECT state FROM live_receipt_processing WHERE receipt_no = ?",
            (receipt_no,),
        ).fetchone()[0]
        == "processed"
    )
    assert conn.execute(
        "SELECT status, attempts FROM event_deliveries WHERE consumer_name = 'restart'"
    ).fetchone() == ("succeeded", 2)
    assert conn.execute(
        "SELECT status, error_type FROM live_poll_runs WHERE id = ?",
        (abandoned_poll_id,),
    ).fetchone() == ("failed", "InterruptedPoll")


def test_poll_failure_is_visible_and_prior_pages_remain_retryable(conn):
    receipt_no = "20260907000001"
    source = FakeDartSource(
        [(_record(receipt_no),), (_record("20260907000002"),)],
        fail_after_pages=1,
    )
    consumer = RecordingConsumer("capture")
    watcher = _watcher(conn, source, consumers=(consumer,))

    with pytest.raises(DartApiError) as exc_info:
        watcher.poll_once(date(2026, 9, 7))

    assert exc_info.value.status == "020"
    poll_row = conn.execute(
        """
        SELECT status, pages_fetched, records_seen, error_type, error_message
        FROM live_poll_runs
        """
    ).fetchone()
    assert poll_row == (
        "failed",
        1,
        1,
        "DartApiError",
        "DART API status=020: request limit exceeded",
    )
    assert (
        conn.execute(
            "SELECT state FROM live_receipt_processing WHERE receipt_no = ?",
            (receipt_no,),
        ).fetchone()[0]
        == "queued"
    )

    processing = watcher.process_pending()
    assert processing.processed_receipt_nos == (receipt_no,)
    assert consumer.attempts == 1


def test_historical_replay_uses_same_envelope_and_delivery_journal(conn):
    receipt_no = "20260907000001"
    source = FakeDartSource([(_record(receipt_no),)])
    live_consumer = RecordingConsumer("live")
    watcher = _watcher(conn, source, consumers=(live_consumer,))
    watcher.run_cycle(date(2026, 9, 7))

    replay_consumer = RecordingConsumer("historical-replay")
    first = watcher.replay(replay_consumer)
    second = watcher.replay(replay_consumer)

    assert first.delivered == 1
    assert first.skipped == 0
    assert second.delivered == 0
    assert second.skipped == 1
    assert replay_consumer.attempts == 1
    assert replay_consumer.envelopes[0] == live_consumer.envelopes[0]


def test_historical_raw_row_can_enter_the_same_normalization_and_delivery_path(conn):
    receipt_no = "20260907000001"
    DisclosureStore(conn).ingest_records([_record(receipt_no)])
    source = FakeDartSource([])
    replay_consumer = RecordingConsumer("raw-history")
    watcher = _watcher(conn, source)

    result = watcher.replay(
        replay_consumer,
        trigger_receipt_nos=(receipt_no,),
    )

    assert result.delivered == 1
    assert replay_consumer.envelopes[0].trigger_receipt_no == receipt_no
    assert replay_consumer.envelopes[0].event.lineage_status == "self_contained"
    assert LiveEventStore(conn).get_snapshot(receipt_no) is not None
    assert (
        conn.execute("SELECT COUNT(*) FROM live_receipt_processing").fetchone()[0] == 0
    )


def test_watcher_policy_enforces_documented_page_and_poll_bounds():
    assert WatcherPolicy().page_count == 100
    with pytest.raises(ValueError, match="at least 60"):
        WatcherPolicy(poll_interval_seconds=59.9)
    with pytest.raises(ValueError, match="between 1 and 100"):
        WatcherPolicy(page_count=101)
    with pytest.raises(ValueError, match="positive"):
        WatcherPolicy(processing_batch_size=0)


@pytest.mark.parametrize("interval", [float("nan"), float("inf"), float("-inf")])
def test_watcher_policy_rejects_non_finite_poll_intervals(interval):
    with pytest.raises(ValueError, match="finite number"):
        WatcherPolicy(poll_interval_seconds=interval)


def test_continuous_watcher_waits_for_remaining_poll_interval(conn):
    source = FakeDartSource([()])
    sleeps: list[float] = []
    monotonic_values = iter((100.0, 101.25))

    class StopLoop(Exception):
        pass

    def sleep_then_stop(seconds: float) -> None:
        sleeps.append(seconds)
        raise StopLoop

    watcher = LiveDartWatcher(
        source=source,
        store=LiveEventStore(conn),
        policy=WatcherPolicy(poll_interval_seconds=60),
        clock=AdvancingClock(),
        sleeper=sleep_then_stop,
        monotonic=lambda: next(monotonic_values),
    )

    with pytest.raises(StopLoop):
        watcher.run_forever(target_date=date(2026, 9, 7))

    assert sleeps == [58.75]


def test_watcher_has_no_strategy_risk_signal_or_execution_dependencies():
    repo_root = Path(__file__).resolve().parents[1]
    module_paths = (
        repo_root / "src/kdtb/live/dart_watcher.py",
        repo_root / "src/kdtb/live/store.py",
        repo_root / "scripts/watch_dart.py",
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
        "kdtb.strategy",
        "kdtb.risk",
        "kdtb.schemas.signal",
        "broker",
    )
    assert not any(
        dependency.startswith(prefix) for dependency in imported for prefix in forbidden
    )
