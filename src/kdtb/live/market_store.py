"""Durable raw-first storage for prospective intraday market collection."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from kdtb.data.kis_intraday import (
    KIS_MINUTE_PAGE_SIZE,
    KisMinuteCapture,
    KisMinuteRequest,
    ParsedMinutePage,
    parse_kis_minute_capture,
)
from kdtb.event_identity import canonical_event_snapshot
from kdtb.schemas.intraday_market_data import (
    IntradayCollectionTarget,
    MarketDataGap,
    make_intraday_collection_target,
    make_market_data_gap,
)

if TYPE_CHECKING:
    from kdtb.live.dart_watcher import EventEnvelope

SEOUL = ZoneInfo("Asia/Seoul")


class MarketDataStoreError(RuntimeError):
    pass


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _initial_collection_cursor(target_date: date, started_at: datetime) -> time:
    local_start = _utc(started_at, "started_at").astimezone(SEOUL)
    if target_date > local_start.date():
        raise ValueError("collector cannot request a future market date")
    if target_date == local_start.date():
        return local_start.time().replace(tzinfo=None, microsecond=0)
    return time(23, 59, 59)


def _cursor_from_text(value: object) -> time:
    if not isinstance(value, str):
        raise MarketDataStoreError("collection run has no durable initial cursor")
    try:
        cursor = datetime.strptime(value, "%H:%M:%S").time()
    except ValueError as exc:
        raise MarketDataStoreError(
            "collection run has an invalid initial cursor"
        ) from exc
    if cursor.strftime("%H:%M:%S") != value:
        raise MarketDataStoreError("collection run initial cursor is not canonical")
    return cursor


@dataclass(frozen=True)
class CollectionWork:
    run_id: int
    target: IntradayCollectionTarget
    initial_cursor: time


@dataclass(frozen=True)
class _StoredCapturePage:
    capture_id: int
    run_id: int
    target_id: str
    page_number: int
    request: KisMinuteRequest
    received_at: datetime
    raw_sha256: str
    page: ParsedMinutePage


class MarketDataStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    @staticmethod
    def _target_from_json(
        payload: str, expected_sha256: str
    ) -> IntradayCollectionTarget:
        if _sha256_text(payload) != expected_sha256:
            raise MarketDataStoreError("stored intraday target hash mismatch")
        try:
            target = IntradayCollectionTarget.model_validate_json(payload)
        except ValueError as exc:
            raise MarketDataStoreError("stored intraday target is invalid") from exc
        if target.canonical_json() != payload:
            raise MarketDataStoreError("stored intraday target is not canonical")
        return target

    def get_target(self, target_id: str) -> IntradayCollectionTarget | None:
        row = self.conn.execute(
            """
            SELECT target_json, target_sha256
            FROM intraday_collection_targets
            WHERE target_id = ?
            """,
            (target_id,),
        ).fetchone()
        if row is None:
            return None
        target = self._target_from_json(row[0], row[1])
        if target.target_id != target_id:
            raise MarketDataStoreError("stored intraday target ID mismatch")
        return target

    def register_event(
        self,
        envelope: EventEnvelope,
        *,
        registered_at: datetime,
        target_date: date | None = None,
    ) -> IntradayCollectionTarget:
        """Register one event/date target idempotently without inventing a symbol."""

        registered_at = _utc(registered_at, "registered_at")
        event_json, event_sha256 = canonical_event_snapshot(envelope.event)
        snapshot = self.conn.execute(
            """
            SELECT normalized_at, event_json, event_sha256
            FROM canonical_event_snapshots
            WHERE trigger_receipt_no = ?
            """,
            (envelope.trigger_receipt_no,),
        ).fetchone()
        if snapshot is None:
            raise MarketDataStoreError(
                "intraday target references an unstored event snapshot"
            )
        if snapshot[1:] != (event_json, event_sha256):
            raise MarketDataStoreError(
                "event envelope differs from its stored snapshot"
            )
        if datetime.fromisoformat(snapshot[0]) != envelope.normalized_at:
            raise MarketDataStoreError(
                "event normalization time differs from its snapshot"
            )

        processing = self.conn.execute(
            """
            SELECT first_seen_at
            FROM live_receipt_processing
            WHERE receipt_no = ?
            """,
            (envelope.trigger_receipt_no,),
        ).fetchone()
        if processing is None:
            raise MarketDataStoreError(
                "event has no durable first-observation timestamp"
            )
        observation_rows = self.conn.execute(
            """
            SELECT observed_at
            FROM dart_disclosure_observations
            WHERE receipt_no = ?
            """,
            (envelope.trigger_receipt_no,),
        ).fetchall()
        if not observation_rows:
            raise MarketDataStoreError("event has no retained raw-list observation")
        event_observed_at = _utc(
            datetime.fromisoformat(processing[0]), "event_observed_at"
        )
        earliest_raw_observation = min(
            _utc(datetime.fromisoformat(row[0]), "raw observed_at")
            for row in observation_rows
        )
        if event_observed_at != earliest_raw_observation:
            raise MarketDataStoreError(
                "event first-observation time differs from retained raw observations"
            )
        event_normalized_at = _utc(envelope.normalized_at, "event_normalized_at")
        provenance = next(
            (
                item
                for item in envelope.event.source_provenance
                if item.receipt_no == envelope.trigger_receipt_no
            ),
            None,
        )
        if provenance is None:
            raise MarketDataStoreError(
                "trigger receipt is absent from event provenance"
            )
        selected_date = target_date or event_observed_at.astimezone(SEOUL).date()
        if selected_date > registered_at.astimezone(SEOUL).date():
            raise ValueError(
                "a market-data target cannot be registered for a future date"
            )

        target = make_intraday_collection_target(
            trigger_receipt_no=envelope.trigger_receipt_no,
            event_sha256=event_sha256,
            economic_event_id=envelope.event.economic_event_id,
            stock_code=envelope.event.issuer.stock_code,
            source_event_date=provenance.receipt_timestamp.date(),
            event_observed_at=event_observed_at,
            event_normalized_at=event_normalized_at,
            target_date=selected_date,
            registered_at=registered_at,
        )
        payload = target.canonical_json()
        payload_sha256 = _sha256_text(payload)
        try:
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO intraday_collection_targets (
                        target_id, trigger_receipt_no, event_sha256,
                        economic_event_id, stock_code, source_event_date,
                        event_observed_at, event_normalized_at, target_date,
                        provider, provider_endpoint, target_sha256, target_json,
                        registered_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        target.target_id,
                        target.trigger_receipt_no,
                        target.event_sha256,
                        target.economic_event_id,
                        target.stock_code,
                        target.source_event_date.isoformat(),
                        target.event_observed_at.isoformat(),
                        target.event_normalized_at.isoformat(),
                        target.target_date.isoformat(),
                        target.provider,
                        target.provider_endpoint,
                        payload_sha256,
                        payload,
                        target.registered_at.isoformat(),
                    ),
                )
                status = "pending" if target.stock_code is not None else "complete"
                self.conn.execute(
                    """
                    INSERT INTO intraday_collection_state (target_id, status)
                    VALUES (?, ?)
                    """,
                    (target.target_id, status),
                )
                if target.stock_code is None:
                    gap = make_market_data_gap(
                        target_id=target.target_id,
                        run_id=None,
                        capture_id=None,
                        reason="missing_stock_code",
                        recorded_at=registered_at,
                        capture_sha256=None,
                        detail="canonical event has no KRX stock code; provider was not called",
                    )
                    self._insert_gap(gap)
        except sqlite3.IntegrityError:
            existing = self.get_target(target.target_id)
            if (
                existing is None
                or existing.identity_payload() != target.identity_payload()
            ):
                raise MarketDataStoreError(
                    "intraday target identity conflicts with durable storage"
                )
            return existing
        return target

    def _insert_gap(self, gap: MarketDataGap) -> None:
        payload = gap.canonical_json()
        digest = _sha256_text(payload)
        self.conn.execute(
            """
            INSERT OR IGNORE INTO market_data_gaps (
                gap_id, target_id, run_id, capture_id, capture_sha256,
                reason, gap_sha256, gap_json, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                gap.gap_id,
                gap.target_id,
                gap.run_id,
                gap.capture_id,
                gap.capture_sha256,
                gap.reason,
                digest,
                payload,
                gap.recorded_at.isoformat(),
            ),
        )
        stored = self.conn.execute(
            """
            SELECT gap_json, gap_sha256
            FROM market_data_gaps
            WHERE gap_id = ?
            """,
            (gap.gap_id,),
        ).fetchone()
        if stored != (payload, digest):
            raise MarketDataStoreError(
                "market-data gap identity conflicts with storage"
            )

    def recover_interrupted(self, *, recovered_at: datetime) -> int:
        recovered_at = _utc(recovered_at, "recovered_at")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            run_ids = self.conn.execute(
                "SELECT id FROM intraday_collection_runs WHERE status = 'running'"
            ).fetchall()
            for (run_id,) in run_ids:
                self._assert_terminal_chronology(run_id, recovered_at)
            run_count = self.conn.execute(
                """
                UPDATE intraday_collection_runs
                SET status = 'failed', finished_at = ?,
                    error_type = 'InterruptedCollection',
                    error_message = 'collector stopped before the run completed'
                WHERE status = 'running'
                """,
                (recovered_at.isoformat(),),
            ).rowcount
            self.conn.execute(
                """
                UPDATE intraday_collection_state
                SET status = 'pending',
                    last_error_type = 'InterruptedCollection',
                    last_error_message = 'collector stopped before the run completed'
                WHERE status = 'collecting'
                """
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return run_count

    def requeue_failed(self) -> int:
        with self.conn:
            return self.conn.execute(
                """
                UPDATE intraday_collection_state
                SET status = 'pending', last_error_type = NULL,
                    last_error_message = NULL
                WHERE status = 'failed'
                """
            ).rowcount

    def requeue_complete(self, target_ids: tuple[str, ...]) -> int:
        """Explicitly collect a later provider vintage for selected targets."""

        requested = tuple(dict.fromkeys(target_ids))
        if not requested:
            return 0
        placeholders = ",".join("?" for _ in requested)
        with self.conn:
            return self.conn.execute(
                f"""
                UPDATE intraday_collection_state
                SET status = 'pending', completed_at = NULL,
                    last_error_type = NULL, last_error_message = NULL
                WHERE status = 'complete'
                  AND target_id IN ({placeholders})
                  AND target_id IN (
                      SELECT target_id
                      FROM intraday_collection_targets
                      WHERE stock_code IS NOT NULL
                  )
                """,
                requested,
            ).rowcount

    def claim_next(self, *, started_at: datetime) -> CollectionWork | None:
        started_at = _utc(started_at, "started_at")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                """
                SELECT target_id
                FROM intraday_collection_state
                WHERE status = 'pending'
                ORDER BY target_id
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                self.conn.commit()
                return None
            target_id = row[0]
            target = self.get_target(target_id)
            if target is None or target.stock_code is None:
                raise MarketDataStoreError("pending target is not provider-requestable")
            self._assert_run_follows_target(target, started_at)
            initial_cursor = _initial_collection_cursor(target.target_date, started_at)
            updated = self.conn.execute(
                """
                UPDATE intraday_collection_state
                SET status = 'collecting', attempts = attempts + 1,
                    last_started_at = ?, completed_at = NULL,
                    last_error_type = NULL, last_error_message = NULL
                WHERE target_id = ? AND status = 'pending'
                """,
                (started_at.isoformat(), target_id),
            ).rowcount
            if updated != 1:
                raise MarketDataStoreError(
                    "intraday target claim lost its pending state"
                )
            cursor = self.conn.execute(
                """
                INSERT INTO intraday_collection_runs (
                    target_id, started_at, initial_cursor, status
                ) VALUES (?, ?, ?, 'running')
                """,
                (
                    target_id,
                    started_at.isoformat(),
                    initial_cursor.strftime("%H:%M:%S"),
                ),
            )
            run_id = int(cursor.lastrowid)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return CollectionWork(
            run_id=run_id,
            target=target,
            initial_cursor=initial_cursor,
        )

    def persist_capture(self, run_id: int, capture: KisMinuteCapture) -> int:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            run = self.conn.execute(
                """
                SELECT r.target_id, r.started_at, r.status, s.status
                FROM intraday_collection_runs AS r
                JOIN intraday_collection_state AS s ON s.target_id = r.target_id
                WHERE r.id = ?
                """,
                (run_id,),
            ).fetchone()
            if run is None or run[2:] != ("running", "collecting"):
                raise MarketDataStoreError("capture requires a running collection run")
            target = self.get_target(run[0])
            if target is None or target.stock_code is None:
                raise MarketDataStoreError("capture run has no requestable target")
            if (
                capture.request.stock_code != target.stock_code
                or capture.request.target_date != target.target_date
            ):
                raise MarketDataStoreError("capture request differs from its target")
            if capture.request.requested_at < datetime.fromisoformat(run[1]):
                raise MarketDataStoreError(
                    "capture request predates its collection run"
                )
            request_json = capture.request.canonical_json()
            cursor = self.conn.execute(
                """
                INSERT INTO intraday_provider_captures (
                    run_id, page_number, source_url, requested_at, received_at,
                    http_status, request_sha256, request_json,
                    raw_sha256, raw_content
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    capture.request.page_number,
                    capture.source_url,
                    capture.request.requested_at.isoformat(),
                    capture.received_at.isoformat(),
                    capture.status_code,
                    _sha256_text(request_json),
                    request_json,
                    capture.raw_sha256,
                    capture.raw_content,
                ),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return int(cursor.lastrowid)

    @staticmethod
    def _request_from_json(payload: str, expected_sha256: str) -> KisMinuteRequest:
        if _sha256_text(payload) != expected_sha256:
            raise MarketDataStoreError("stored KIS request hash mismatch")
        try:
            value = json.loads(payload)
            parameters = value["parameters"]
            request = KisMinuteRequest(
                stock_code=parameters["FID_INPUT_ISCD"],
                target_date=datetime.strptime(
                    parameters["FID_INPUT_DATE_1"], "%Y%m%d"
                ).date(),
                through_time=datetime.strptime(
                    parameters["FID_INPUT_HOUR_1"], "%H%M%S"
                ).time(),
                page_number=value["page_number"],
                requested_at=datetime.fromisoformat(value["requested_at"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise MarketDataStoreError("stored KIS request is invalid") from exc
        if request.canonical_json() != payload:
            raise MarketDataStoreError("stored KIS request is not canonical")
        return request

    @staticmethod
    def _assert_run_follows_target(
        target: IntradayCollectionTarget, started_at: datetime
    ) -> None:
        registered_at = _utc(target.registered_at, "target registered_at")
        if started_at < registered_at:
            raise MarketDataStoreError(
                "collection run cannot start before target registration"
            )

    def _assert_terminal_chronology(self, run_id: int, terminal_at: datetime) -> None:
        rows = self.conn.execute(
            """
            SELECT r.target_id, r.started_at, c.requested_at, c.received_at
            FROM intraday_collection_runs AS r
            LEFT JOIN intraday_provider_captures AS c ON c.run_id = r.id
            WHERE r.id = ?
            """,
            (run_id,),
        ).fetchall()
        if not rows:
            raise MarketDataStoreError("unknown collection run")
        try:
            started_at = _utc(
                datetime.fromisoformat(rows[0][1]), "stored run started_at"
            )
            evidence_times = [
                _utc(datetime.fromisoformat(value), "stored collection timestamp")
                for row in rows
                for value in row[2:]
                if value is not None
            ]
        except (TypeError, ValueError) as exc:
            raise MarketDataStoreError(
                "collection run contains an invalid evidence timestamp"
            ) from exc
        target = self.get_target(rows[0][0])
        if target is None:
            raise MarketDataStoreError("collection target is missing")
        self._assert_run_follows_target(target, started_at)
        evidence_times.append(started_at)
        if any(terminal_at < value for value in evidence_times):
            raise MarketDataStoreError(
                "terminal timestamp predates the run or retained provider traffic"
            )

    def _load_capture_page(
        self, capture_id: int, *, require_running: bool
    ) -> _StoredCapturePage:
        row = self.conn.execute(
            """
            SELECT c.run_id, c.page_number, c.source_url, c.requested_at,
                   c.received_at, c.http_status, c.request_sha256, c.request_json,
                   c.raw_sha256, c.raw_content, r.target_id, r.status, s.status
            FROM intraday_provider_captures AS c
            JOIN intraday_collection_runs AS r ON r.id = c.run_id
            JOIN intraday_collection_state AS s ON s.target_id = r.target_id
            WHERE c.id = ?
            """,
            (capture_id,),
        ).fetchone()
        if row is None:
            raise MarketDataStoreError("unknown intraday provider capture")
        if require_running and (row[11] != "running" or row[12] != "collecting"):
            raise MarketDataStoreError(
                "capture normalization requires a running collection run"
            )
        if hashlib.sha256(row[9]).hexdigest() != row[8]:
            raise MarketDataStoreError("stored intraday raw-capture hash mismatch")
        request = self._request_from_json(row[7], row[6])
        if request.page_number != row[1]:
            raise MarketDataStoreError(
                "stored KIS request page differs from its capture"
            )
        if request.requested_at != datetime.fromisoformat(row[3]):
            raise MarketDataStoreError(
                "stored KIS request time differs from its capture"
            )
        target = self.get_target(row[10])
        if target is None:
            raise MarketDataStoreError("capture target is missing")
        try:
            capture = KisMinuteCapture(
                request=request,
                received_at=datetime.fromisoformat(row[4]),
                status_code=row[5],
                raw_content=row[9],
                source_url=row[2],
            )
            page = parse_kis_minute_capture(capture, target)
        except (ValueError, RuntimeError) as exc:
            raise MarketDataStoreError(
                f"stored KIS capture cannot be normalized: {exc}"
            ) from exc
        return _StoredCapturePage(
            capture_id=capture_id,
            run_id=row[0],
            target_id=row[10],
            page_number=row[1],
            request=request,
            received_at=capture.received_at,
            raw_sha256=row[8],
            page=page,
        )

    @staticmethod
    def _observation_storage_row(observation, capture_id: int) -> tuple:
        payload = observation.canonical_json()
        return (
            observation.observation_id,
            observation.target_id,
            capture_id,
            observation.capture_sha256,
            observation.market_timestamp.isoformat(),
            observation.captured_at.isoformat(),
            _sha256_text(payload),
            payload,
        )

    def normalize_capture(self, capture_id: int) -> ParsedMinutePage:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            stored_capture = self._load_capture_page(capture_id, require_running=True)
            for observation in stored_capture.page.observations:
                values = self._observation_storage_row(observation, capture_id)
                self.conn.execute(
                    """
                    INSERT OR IGNORE INTO intraday_bar_observations (
                        observation_id, target_id, capture_id, capture_sha256,
                        market_timestamp, captured_at, observation_sha256,
                        observation_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
                stored = self.conn.execute(
                    """
                    SELECT observation_id, target_id, capture_id, capture_sha256,
                           market_timestamp, captured_at, observation_sha256,
                           observation_json
                    FROM intraday_bar_observations
                    WHERE observation_id = ?
                    """,
                    (observation.observation_id,),
                ).fetchone()
                if stored != values:
                    raise MarketDataStoreError(
                        "intraday observation identity conflicts with storage"
                    )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return stored_capture.page

    def finish_run(
        self,
        run_id: int,
        *,
        finished_at: datetime,
    ) -> int:
        finished_at = _utc(finished_at, "finished_at")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            run = self.conn.execute(
                """
                SELECT r.target_id, r.started_at, r.initial_cursor,
                       r.status, s.status
                FROM intraday_collection_runs AS r
                JOIN intraday_collection_state AS s ON s.target_id = r.target_id
                WHERE r.id = ?
                """,
                (run_id,),
            ).fetchone()
            if run is None or run[3:] != ("running", "collecting"):
                raise MarketDataStoreError("only a running collection can finish")
            try:
                started_at = _utc(
                    datetime.fromisoformat(run[1]), "stored run started_at"
                )
            except (TypeError, ValueError) as exc:
                raise MarketDataStoreError(
                    "collection run has an invalid start time"
                ) from exc
            initial_cursor = _cursor_from_text(run[2])
            target = self.get_target(run[0])
            if target is None:
                raise MarketDataStoreError("collection target is missing")
            if initial_cursor != _initial_collection_cursor(
                target.target_date, started_at
            ):
                raise MarketDataStoreError(
                    "collection run initial cursor differs from its start time"
                )
            self._assert_terminal_chronology(run_id, finished_at)
            captures = self.conn.execute(
                """
                SELECT id, page_number
                FROM intraday_provider_captures
                WHERE run_id = ?
                ORDER BY page_number
                """,
                (run_id,),
            ).fetchall()
            if not captures:
                raise MarketDataStoreError(
                    "collection completion requires at least one raw capture"
                )
            if [row[1] for row in captures] != list(range(1, len(captures) + 1)):
                raise MarketDataStoreError(
                    "collection captures do not form a contiguous page sequence"
                )

            parsed_captures: list[_StoredCapturePage] = []
            previous: _StoredCapturePage | None = None
            observations_seen = 0
            for capture_id, _ in captures:
                current = self._load_capture_page(capture_id, require_running=True)
                if current.run_id != run_id or current.target_id != run[0]:
                    raise MarketDataStoreError(
                        "collection capture differs from its running target"
                    )
                if current.page_number == 1 and (
                    current.request.through_time != initial_cursor
                ):
                    raise MarketDataStoreError(
                        "first capture differs from the run initial cursor"
                    )
                if previous is not None:
                    if current.request.requested_at < previous.received_at:
                        raise MarketDataStoreError(
                            "collection capture chronology is not source-contiguous"
                        )
                    if previous.page.source_row_count != KIS_MINUTE_PAGE_SIZE:
                        raise MarketDataStoreError(
                            "a short KIS page cannot be followed by another page"
                        )
                    if not previous.page.observations:
                        raise MarketDataStoreError(
                            "a full KIS page cannot normalize to no observations"
                        )
                    earliest = min(
                        item.market_timestamp for item in previous.page.observations
                    )
                    expected_cursor = earliest - timedelta(seconds=1)
                    if (
                        expected_cursor.date() < previous.request.target_date
                        or current.request.through_time
                        != expected_cursor.time().replace(tzinfo=None)
                    ):
                        raise MarketDataStoreError(
                            "collection capture cursor is not source-contiguous"
                        )

                expected_rows = sorted(
                    self._observation_storage_row(item, current.capture_id)
                    for item in current.page.observations
                )
                actual_rows = self.conn.execute(
                    """
                    SELECT observation_id, target_id, capture_id, capture_sha256,
                           market_timestamp, captured_at, observation_sha256,
                           observation_json
                    FROM intraday_bar_observations
                    WHERE capture_id = ?
                    ORDER BY observation_id
                    """,
                    (current.capture_id,),
                ).fetchall()
                if actual_rows != expected_rows:
                    raise MarketDataStoreError(
                        "persisted minute bars do not match their retained capture"
                    )
                observations_seen += len(expected_rows)
                parsed_captures.append(current)
                previous = current

            final_capture = parsed_captures[-1]
            if final_capture.page.source_row_count == KIS_MINUTE_PAGE_SIZE:
                if not final_capture.page.observations:
                    raise MarketDataStoreError(
                        "a full KIS page cannot normalize to no observations"
                    )
                earliest = min(
                    item.market_timestamp for item in final_capture.page.observations
                )
                if (
                    earliest - timedelta(seconds=1)
                ).date() >= final_capture.request.target_date:
                    raise MarketDataStoreError(
                        "collection has no source-confirmed terminal page"
                    )

            existing_gaps = self.conn.execute(
                "SELECT COUNT(*) FROM market_data_gaps WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
            if existing_gaps:
                raise MarketDataStoreError(
                    "a running collection cannot already have a terminal gap"
                )
            if observations_seen == 0:
                if (
                    len(parsed_captures) != 1
                    or final_capture.page.source_row_count != 0
                ):
                    raise MarketDataStoreError(
                        "no-row completion is not supported by retained source rows"
                    )
                capture = self.conn.execute(
                    """
                    SELECT id, raw_sha256
                    FROM intraday_provider_captures
                    WHERE run_id = ?
                    ORDER BY page_number DESC
                    LIMIT 1
                    """,
                    (run_id,),
                ).fetchone()
                gap = make_market_data_gap(
                    target_id=run[0],
                    run_id=run_id,
                    capture_id=capture[0],
                    reason="provider_no_rows",
                    recorded_at=finished_at,
                    capture_sha256=capture[1],
                    detail="verified KIS response contained no minute rows",
                )
                self._insert_gap(gap)
            updated = self.conn.execute(
                """
                UPDATE intraday_collection_runs
                SET status = 'succeeded', finished_at = ?, observations_seen = ?
                WHERE id = ? AND status = 'running'
                """,
                (finished_at.isoformat(), observations_seen, run_id),
            ).rowcount
            if updated != 1:
                raise MarketDataStoreError("collection run changed before completion")
            state_updated = self.conn.execute(
                """
                UPDATE intraday_collection_state
                SET status = 'complete', completed_at = ?,
                    last_error_type = NULL, last_error_message = NULL
                WHERE target_id = ? AND status = 'collecting'
                """,
                (finished_at.isoformat(), run[0]),
            ).rowcount
            if state_updated != 1:
                raise MarketDataStoreError(
                    "collection target changed before completion"
                )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return observations_seen

    def fail_run(self, run_id: int, *, failed_at: datetime, error: Exception) -> None:
        failed_at = _utc(failed_at, "failed_at")
        error_type = type(error).__name__
        message = str(error)[:2000]
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                "SELECT target_id, status FROM intraday_collection_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise MarketDataStoreError("unknown collection run")
            if row[1] != "running":
                self.conn.commit()
                return
            self._assert_terminal_chronology(run_id, failed_at)
            run_updated = self.conn.execute(
                """
                UPDATE intraday_collection_runs
                SET status = 'failed', finished_at = ?, error_type = ?,
                    error_message = ?
                WHERE id = ? AND status = 'running'
                """,
                (failed_at.isoformat(), error_type, message, run_id),
            ).rowcount
            if run_updated != 1:
                raise MarketDataStoreError("collection run changed before failure")
            state_updated = self.conn.execute(
                """
                UPDATE intraday_collection_state
                SET status = 'failed', last_error_type = ?, last_error_message = ?
                WHERE target_id = ? AND status = 'collecting'
                """,
                (error_type, message, row[0]),
            ).rowcount
            if state_updated != 1:
                raise MarketDataStoreError("collection target changed before failure")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def pending_count(self) -> int:
        return int(
            self.conn.execute(
                "SELECT COUNT(*) FROM intraday_collection_state WHERE status = 'pending'"
            ).fetchone()[0]
        )

    def failed_count(self) -> int:
        return int(
            self.conn.execute(
                "SELECT COUNT(*) FROM intraday_collection_state WHERE status = 'failed'"
            ).fetchone()[0]
        )
