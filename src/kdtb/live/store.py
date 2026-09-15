from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from numbers import Integral
from typing import Iterable, Sequence

from kdtb.data.dart_client import (
    DartRelationshipCapture,
    decode_report_relations_html,
    parse_report_relations_html,
)
from kdtb.data.disclosure_store import DisclosureStore, dart_record_to_disclosure
from kdtb.event_identity import (
    canonical_event_snapshot,
    event_from_canonical_snapshot,
)
from kdtb.schemas.disclosure import Disclosure
from kdtb.schemas.economic_event import DartReportRelations, EconomicEvent


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class StoredEventSnapshot:
    trigger_receipt_no: str
    event: EconomicEvent
    normalized_at: datetime
    event_sha256: str


class LiveEventStore:
    """Durable journal for raw observations, normalization, and delivery."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.disclosures = DisclosureStore(conn)

    def start_poll_run(
        self,
        *,
        target_date: str,
        corp_cls: str | None,
        started_at: datetime,
    ) -> int:
        with self.conn:
            cursor = self.conn.execute(
                """
                INSERT INTO live_poll_runs (
                    target_date, corp_cls, started_at, status
                ) VALUES (?, ?, ?, 'running')
                """,
                (target_date, corp_cls, started_at.isoformat()),
            )
        return int(cursor.lastrowid)

    def persist_poll_page(
        self,
        *,
        poll_run_id: int,
        records: Sequence[dict],
        observed_at: datetime,
    ) -> tuple[int, int]:
        """Commit one provider page before any receipt can be normalized."""

        new_receipts = 0
        new_observations = 0
        observed_at_text = observed_at.isoformat()
        with self.conn:
            for record in records:
                disclosure = dart_record_to_disclosure(record)
                self.disclosures.upsert(disclosure)

                raw_payload_json = _canonical_json(record)
                raw_payload_sha256 = _sha256_text(raw_payload_json)
                observation = self.conn.execute(
                    """
                    INSERT OR IGNORE INTO dart_disclosure_observations (
                        receipt_no, observed_at, raw_payload_sha256, raw_payload_json
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        disclosure.receipt_no,
                        observed_at_text,
                        raw_payload_sha256,
                        raw_payload_json,
                    ),
                )
                new_observations += int(observation.rowcount > 0)

                queued = self.conn.execute(
                    """
                    INSERT OR IGNORE INTO live_receipt_processing (
                        receipt_no, first_seen_at, last_seen_at, state
                    ) VALUES (?, ?, ?, 'queued')
                    """,
                    (
                        disclosure.receipt_no,
                        observed_at_text,
                        observed_at_text,
                    ),
                )
                new_receipts += int(queued.rowcount > 0)
                self.conn.execute(
                    """
                    UPDATE live_receipt_processing
                    SET last_seen_at = ?
                    WHERE receipt_no = ?
                    """,
                    (observed_at_text, disclosure.receipt_no),
                )

            self.conn.execute(
                """
                UPDATE live_poll_runs
                SET pages_fetched = pages_fetched + 1,
                    records_seen = records_seen + ?,
                    new_receipts = new_receipts + ?
                WHERE id = ?
                """,
                (len(records), new_receipts, poll_run_id),
            )
        return new_receipts, new_observations

    def finish_poll_run(self, poll_run_id: int, *, completed_at: datetime) -> None:
        with self.conn:
            self.conn.execute(
                """
                UPDATE live_poll_runs
                SET status = 'succeeded', completed_at = ?,
                    error_type = NULL, error_message = NULL
                WHERE id = ?
                """,
                (completed_at.isoformat(), poll_run_id),
            )

    def fail_poll_run(
        self,
        poll_run_id: int,
        *,
        failed_at: datetime,
        error: BaseException,
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                UPDATE live_poll_runs
                SET status = 'failed', completed_at = ?,
                    error_type = ?, error_message = ?
                WHERE id = ?
                """,
                (
                    failed_at.isoformat(),
                    type(error).__name__,
                    str(error),
                    poll_run_id,
                ),
            )

    def recover_interrupted(self, *, recovered_at: datetime) -> tuple[int, int, int]:
        """Make process-terminated receipt and delivery attempts retryable."""

        recovered_at_text = recovered_at.isoformat()
        with self.conn:
            polls = self.conn.execute(
                """
                UPDATE live_poll_runs
                SET status = 'failed', completed_at = ?,
                    error_type = 'InterruptedPoll',
                    error_message = 'watcher stopped before poll completed'
                WHERE status = 'running'
                """,
                (recovered_at_text,),
            ).rowcount
            receipts = self.conn.execute(
                """
                UPDATE live_receipt_processing
                SET state = 'failed',
                    last_error_type = 'InterruptedProcessing',
                    last_error_message = 'watcher stopped before processing completed',
                    last_attempted_at = ?
                WHERE state = 'processing'
                """,
                (recovered_at_text,),
            ).rowcount
            deliveries = self.conn.execute(
                """
                UPDATE event_deliveries
                SET status = 'failed',
                    last_error_type = 'InterruptedDelivery',
                    last_error_message = 'watcher stopped before delivery acknowledgement',
                    last_attempted_at = ?
                WHERE status = 'delivering'
                """,
                (recovered_at_text,),
            ).rowcount
        return polls, receipts, deliveries

    def pending_receipts(
        self,
        *,
        limit: int,
        exclude_receipt_nos: Iterable[str] = (),
    ) -> tuple[str, ...]:
        excluded = tuple(dict.fromkeys(exclude_receipt_nos))
        exclusion_sql = ""
        parameters: list[object] = []
        if excluded:
            placeholders = ",".join("?" for _ in excluded)
            exclusion_sql = f" AND receipt_no NOT IN ({placeholders})"
            parameters.extend(excluded)
        parameters.append(limit)
        rows = self.conn.execute(
            f"""
            SELECT receipt_no
            FROM live_receipt_processing
            WHERE state IN ('queued', 'failed')
            {exclusion_sql}
            ORDER BY first_seen_at, receipt_no
            LIMIT ?
            """,
            parameters,
        ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def count_unfinished_receipts(self) -> int:
        return int(
            self.conn.execute(
                """
                SELECT COUNT(*)
                FROM live_receipt_processing
                WHERE state <> 'processed'
                """
            ).fetchone()[0]
        )

    def mark_receipt_processing(
        self, receipt_no: str, *, attempted_at: datetime
    ) -> None:
        with self.conn:
            cursor = self.conn.execute(
                """
                UPDATE live_receipt_processing
                SET state = 'processing', attempts = attempts + 1,
                    last_attempted_at = ?, last_error_type = NULL,
                    last_error_message = NULL
                WHERE receipt_no = ? AND state IN ('queued', 'failed')
                """,
                (attempted_at.isoformat(), receipt_no),
            )
        if cursor.rowcount != 1:
            raise RuntimeError(f"receipt {receipt_no} is not available for processing")

    def mark_receipt_processed(
        self, receipt_no: str, *, processed_at: datetime
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                UPDATE live_receipt_processing
                SET state = 'processed', processed_at = ?,
                    last_error_type = NULL, last_error_message = NULL
                WHERE receipt_no = ?
                """,
                (processed_at.isoformat(), receipt_no),
            )

    def mark_receipt_failed(
        self,
        receipt_no: str,
        *,
        failed_at: datetime,
        error: BaseException,
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                UPDATE live_receipt_processing
                SET state = 'failed', last_attempted_at = ?,
                    last_error_type = ?, last_error_message = ?
                WHERE receipt_no = ?
                """,
                (
                    failed_at.isoformat(),
                    type(error).__name__,
                    str(error),
                    receipt_no,
                ),
            )

    def get_disclosure(self, receipt_no: str) -> Disclosure | None:
        row = self.conn.execute(
            """
            SELECT id, receipt_no, corp_code, corp_name, stock_code, report_name,
                   receipt_datetime, market, source, raw_url, raw_payload_json,
                   raw_text, created_at, updated_at
            FROM disclosures
            WHERE receipt_no = ?
            """,
            (receipt_no,),
        ).fetchone()
        if row is None:
            return None
        return Disclosure(
            id=row[0],
            receipt_no=row[1],
            corp_code=row[2],
            corp_name=row[3],
            stock_code=row[4],
            report_name=row[5],
            receipt_datetime=datetime.fromisoformat(row[6]),
            market=row[7],
            source=row[8],
            raw_url=row[9],
            raw_payload=json.loads(row[10]) if row[10] else {},
            raw_text=row[11],
            created_at=datetime.fromisoformat(row[12]) if row[12] else None,
            updated_at=datetime.fromisoformat(row[13]) if row[13] else None,
        )

    def receipt_nos_for_date(
        self,
        target_date: date,
        *,
        limit: int | None = None,
    ) -> tuple[str, ...]:
        """Select a bounded historical stream without changing normalization."""

        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, Integral) or limit < 1
        ):
            raise ValueError("historical replay limit must be a positive integer")
        date_prefix = target_date.strftime("%Y%m%d")
        parameters: list[object] = [f"{date_prefix}000000", f"{date_prefix}999999"]
        limit_sql = ""
        if limit is not None:
            limit_sql = " LIMIT ?"
            parameters.append(int(limit))
        rows = self.conn.execute(
            """
            SELECT receipt_no
            FROM disclosures
            WHERE receipt_no BETWEEN ? AND ?
            ORDER BY receipt_no
            """
            + limit_sql,
            parameters,
        ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def save_relationship_capture(
        self,
        capture: DartRelationshipCapture,
        *,
        fetched_at: datetime,
    ) -> bool:
        relations = capture.relations
        raw_content = bytes(capture.raw_content)
        actual_hash = hashlib.sha256(raw_content).hexdigest()
        if actual_hash != relations.raw_html_sha256:
            raise ValueError("relationship capture bytes do not match evidence hash")
        parsed_from_bytes = parse_report_relations_html(
            decode_report_relations_html(raw_content),
            relations.receipt_no,
            source_url=relations.source_url,
            raw_html_sha256=actual_hash,
        )
        if parsed_from_bytes != relations:
            raise ValueError("relationship capture object does not match captured HTML")
        relations_json = relations.model_dump_json()
        fetched_at_text = fetched_at.isoformat()
        with self.conn:
            cursor = self.conn.execute(
                """
                INSERT OR IGNORE INTO dart_relationship_captures (
                    receipt_no, source_url, fetched_at, last_seen_at,
                    raw_html_sha256, raw_html, relations_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    relations.receipt_no,
                    relations.source_url,
                    fetched_at_text,
                    fetched_at_text,
                    relations.raw_html_sha256,
                    raw_content,
                    relations_json,
                ),
            )
            inserted = cursor.rowcount > 0
            if not inserted:
                self.conn.execute(
                    """
                    UPDATE dart_relationship_captures
                    SET last_seen_at = ?
                    WHERE receipt_no = ? AND raw_html_sha256 = ?
                    """,
                    (
                        fetched_at_text,
                        relations.receipt_no,
                        relations.raw_html_sha256,
                    ),
                )
        return inserted

    def get_latest_relationship(self, receipt_no: str) -> DartReportRelations | None:
        row = self.conn.execute(
            """
            SELECT source_url, raw_html, raw_html_sha256, relations_json
            FROM dart_relationship_captures
            WHERE receipt_no = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (receipt_no,),
        ).fetchone()
        if row is None:
            return None
        source_url = str(row[0])
        raw_content = bytes(row[1])
        actual_hash = hashlib.sha256(raw_content).hexdigest()
        if actual_hash != row[2]:
            raise ValueError(
                f"stored relationship evidence hash mismatch for {receipt_no}"
            )
        relation = DartReportRelations.model_validate_json(row[3])
        if relation.raw_html_sha256 != actual_hash:
            raise ValueError(
                f"stored relationship object hash mismatch for {receipt_no}"
            )
        parsed_from_bytes = parse_report_relations_html(
            decode_report_relations_html(raw_content),
            receipt_no,
            source_url=source_url,
            raw_html_sha256=actual_hash,
        )
        if parsed_from_bytes != relation:
            raise ValueError(
                f"stored relationship object does not match captured HTML for {receipt_no}"
            )
        return relation

    def get_snapshot(self, trigger_receipt_no: str) -> StoredEventSnapshot | None:
        row = self.conn.execute(
            """
            SELECT economic_event_id, normalized_at, event_sha256, event_json
            FROM canonical_event_snapshots
            WHERE trigger_receipt_no = ?
            """,
            (trigger_receipt_no,),
        ).fetchone()
        if row is None:
            return None
        actual_hash = _sha256_text(row[3])
        if actual_hash != row[2]:
            raise ValueError(
                f"stored canonical snapshot hash mismatch for {trigger_receipt_no}"
            )
        event = event_from_canonical_snapshot(row[3])
        if event.economic_event_id != row[0]:
            raise ValueError(
                f"stored canonical snapshot event ID mismatch for {trigger_receipt_no}"
            )
        return StoredEventSnapshot(
            trigger_receipt_no=trigger_receipt_no,
            event=event,
            normalized_at=datetime.fromisoformat(row[1]),
            event_sha256=row[2],
        )

    def save_snapshot(
        self,
        *,
        trigger_receipt_no: str,
        event: EconomicEvent,
        normalized_at: datetime,
    ) -> StoredEventSnapshot:
        payload, payload_hash = canonical_event_snapshot(event)
        event_from_canonical_snapshot(payload)
        event_receipt_ids = {
            f"dart:{receipt_no}"
            for receipt_no in {
                event.primary_receipt_no,
                *event.related_receipt_nos,
            }
        }
        with self.conn:
            cursor = self.conn.execute(
                """
                INSERT OR IGNORE INTO canonical_event_snapshots (
                    trigger_receipt_no, economic_event_id, normalized_at,
                    event_sha256, event_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    trigger_receipt_no,
                    event.economic_event_id,
                    normalized_at.isoformat(),
                    payload_hash,
                    payload,
                ),
            )
            if cursor.rowcount == 0:
                existing = self.get_snapshot(trigger_receipt_no)
                if existing is None or existing.event_sha256 != payload_hash:
                    raise ValueError(
                        f"canonical snapshot for {trigger_receipt_no} is immutable"
                    )
                return existing

            for alias_event_id in sorted(event_receipt_ids - {event.economic_event_id}):
                self.conn.execute(
                    """
                    INSERT OR IGNORE INTO economic_event_aliases (
                        alias_event_id, canonical_event_id,
                        learned_from_receipt_no, created_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        alias_event_id,
                        event.economic_event_id,
                        trigger_receipt_no,
                        normalized_at.isoformat(),
                    ),
                )
        return StoredEventSnapshot(
            trigger_receipt_no=trigger_receipt_no,
            event=event,
            normalized_at=normalized_at,
            event_sha256=payload_hash,
        )

    def list_snapshots(
        self, *, trigger_receipt_nos: Iterable[str] | None = None
    ) -> tuple[StoredEventSnapshot, ...]:
        requested = tuple(dict.fromkeys(trigger_receipt_nos or ()))
        if trigger_receipt_nos is not None and not requested:
            return ()
        if requested:
            placeholders = ",".join("?" for _ in requested)
            rows = self.conn.execute(
                f"""
                SELECT trigger_receipt_no, economic_event_id, normalized_at,
                       event_sha256, event_json
                FROM canonical_event_snapshots
                WHERE trigger_receipt_no IN ({placeholders})
                ORDER BY normalized_at, trigger_receipt_no
                """,
                requested,
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT trigger_receipt_no, economic_event_id, normalized_at,
                       event_sha256, event_json
                FROM canonical_event_snapshots
                ORDER BY normalized_at, trigger_receipt_no
                """
            ).fetchall()
        snapshots: list[StoredEventSnapshot] = []
        for row in rows:
            actual_hash = _sha256_text(row[4])
            if actual_hash != row[3]:
                raise ValueError(
                    f"stored canonical snapshot hash mismatch for {row[0]}"
                )
            event = event_from_canonical_snapshot(row[4])
            if event.economic_event_id != row[1]:
                raise ValueError(
                    f"stored canonical snapshot event ID mismatch for {row[0]}"
                )
            snapshots.append(
                StoredEventSnapshot(
                    trigger_receipt_no=row[0],
                    event=event,
                    normalized_at=datetime.fromisoformat(row[2]),
                    event_sha256=row[3],
                )
            )
        return tuple(snapshots)

    def resolve_event_id(self, event_id: str) -> str:
        current = event_id
        visited: set[str] = set()
        while True:
            if current in visited:
                raise ValueError("economic-event alias cycle detected")
            visited.add(current)
            row = self.conn.execute(
                """
                SELECT canonical_event_id
                FROM economic_event_aliases
                WHERE alias_event_id = ?
                """,
                (current,),
            ).fetchone()
            if row is None:
                return current
            current = str(row[0])

    def begin_delivery(
        self,
        *,
        consumer_name: str,
        trigger_receipt_no: str,
        delivery_id: str,
        attempted_at: datetime,
    ) -> bool:
        """Persist intent and return False when this delivery already succeeded."""

        attempted_at_text = attempted_at.isoformat()
        with self.conn:
            row = self.conn.execute(
                """
                SELECT delivery_id, status
                FROM event_deliveries
                WHERE consumer_name = ? AND trigger_receipt_no = ?
                """,
                (consumer_name, trigger_receipt_no),
            ).fetchone()
            if row is not None:
                if row[0] != delivery_id:
                    raise ValueError("delivery id changed for immutable event snapshot")
                if row[1] == "succeeded":
                    return False
            else:
                self.conn.execute(
                    """
                    INSERT INTO event_deliveries (
                        consumer_name, trigger_receipt_no, delivery_id, status
                    ) VALUES (?, ?, ?, 'pending')
                    """,
                    (consumer_name, trigger_receipt_no, delivery_id),
                )
            self.conn.execute(
                """
                UPDATE event_deliveries
                SET status = 'delivering', attempts = attempts + 1,
                    first_attempted_at = COALESCE(first_attempted_at, ?),
                    last_attempted_at = ?, last_error_type = NULL,
                    last_error_message = NULL
                WHERE consumer_name = ? AND trigger_receipt_no = ?
                """,
                (
                    attempted_at_text,
                    attempted_at_text,
                    consumer_name,
                    trigger_receipt_no,
                ),
            )
        return True

    def finish_delivery(
        self,
        *,
        consumer_name: str,
        trigger_receipt_no: str,
        delivered_at: datetime,
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                UPDATE event_deliveries
                SET status = 'succeeded', delivered_at = ?,
                    last_error_type = NULL, last_error_message = NULL
                WHERE consumer_name = ? AND trigger_receipt_no = ?
                """,
                (delivered_at.isoformat(), consumer_name, trigger_receipt_no),
            )

    def fail_delivery(
        self,
        *,
        consumer_name: str,
        trigger_receipt_no: str,
        failed_at: datetime,
        error: BaseException,
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                UPDATE event_deliveries
                SET status = 'failed', last_attempted_at = ?,
                    last_error_type = ?, last_error_message = ?
                WHERE consumer_name = ? AND trigger_receipt_no = ?
                """,
                (
                    failed_at.isoformat(),
                    type(error).__name__,
                    str(error),
                    consumer_name,
                    trigger_receipt_no,
                ),
            )
