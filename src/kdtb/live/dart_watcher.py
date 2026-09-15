from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Callable, Iterable, Protocol, Sequence
from zoneinfo import ZoneInfo

from kdtb.data.dart_client import DartRelationshipCapture
from kdtb.events.normalizer import classify_event_action, normalize_disclosures
from kdtb.event_identity import delivery_id_for_snapshot
from kdtb.live.store import LiveEventStore, StoredEventSnapshot
from kdtb.schemas.economic_event import DartReportRelations, EconomicEvent

logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DartWatcherSource(Protocol):
    def iter_disclosure_pages(
        self,
        target_date: date,
        corp_cls: str | None = None,
        page_count: int = 100,
    ) -> Iterable[tuple[dict, ...]]: ...

    def fetch_report_relations_capture(
        self, rcept_no: str
    ) -> DartRelationshipCapture: ...


@dataclass(frozen=True)
class EventEnvelope:
    """Stable idempotency key plus the immutable canonical event snapshot."""

    delivery_id: str
    trigger_receipt_no: str
    normalized_at: datetime
    event: EconomicEvent


class EventConsumer(Protocol):
    consumer_name: str

    def consume(self, envelope: EventEnvelope) -> None: ...


@dataclass(frozen=True)
class WatcherPolicy:
    poll_interval_seconds: float = 60.0
    page_count: int = 100
    processing_batch_size: int = 100

    def __post_init__(self) -> None:
        # The provider documents a general (not guaranteed) 20,000-request
        # daily threshold. A conservative one-minute floor, maximum page size,
        # and explicit status-020 failure avoid an accidental tight-loop flood.
        # Multi-page cycles and other API clients still share the key's quota.
        interval = self.poll_interval_seconds
        if isinstance(interval, bool) or not isinstance(interval, (int, float)):
            raise ValueError("poll_interval_seconds must be a finite number")
        interval = float(interval)
        if not math.isfinite(interval):
            raise ValueError("poll_interval_seconds must be a finite number")
        if interval < 60:
            raise ValueError("poll_interval_seconds must be at least 60")
        object.__setattr__(self, "poll_interval_seconds", interval)
        if not 1 <= self.page_count <= 100:
            raise ValueError("page_count must be between 1 and 100")
        if self.processing_batch_size < 1:
            raise ValueError("processing_batch_size must be positive")


@dataclass(frozen=True)
class PollResult:
    poll_run_id: int
    pages_fetched: int
    records_seen: int
    new_receipts: int
    new_observations: int


@dataclass(frozen=True)
class ProcessingResult:
    processed_receipt_nos: tuple[str, ...]
    failed_receipt_nos: tuple[str, ...]
    remaining_receipts: int


@dataclass(frozen=True)
class ReplayResult:
    delivered: int
    skipped: int
    failed_trigger_receipt_nos: tuple[str, ...]


@dataclass(frozen=True)
class CycleResult:
    poll: PollResult
    processing: ProcessingResult


class DownstreamDeliveryError(RuntimeError):
    pass


class LiveDartWatcher:
    """Incremental raw-first DART ingestion and canonical event delivery.

    Successful consumer deliveries are at-most-once from this journal. A crash
    after the consumer performs its side effect but before acknowledgement can
    produce an at-least-once retry, so consumers must use ``delivery_id`` as
    their idempotency key.
    """

    def __init__(
        self,
        *,
        source: DartWatcherSource,
        store: LiveEventStore,
        consumers: Sequence[EventConsumer] = (),
        policy: WatcherPolicy | None = None,
        clock: Callable[[], datetime] = utc_now,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        consumer_names = [consumer.consumer_name for consumer in consumers]
        if any(not name.strip() for name in consumer_names):
            raise ValueError("consumer_name must not be empty")
        if len(consumer_names) != len(set(consumer_names)):
            raise ValueError("consumer_name values must be unique")
        self.source = source
        self.store = store
        self.consumers = tuple(consumers)
        self.policy = policy or WatcherPolicy()
        self.clock = clock
        self.sleeper = sleeper
        self.monotonic = monotonic
        self._recovered = False

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("watcher clock must return a timezone-aware datetime")
        return value

    def _recover_once(self) -> None:
        if self._recovered:
            return
        polls, receipts, deliveries = self.store.recover_interrupted(
            recovered_at=self._now()
        )
        if polls or receipts or deliveries:
            logger.warning(
                "recovered interrupted live work polls=%d receipts=%d deliveries=%d",
                polls,
                receipts,
                deliveries,
            )
        self._recovered = True

    def poll_once(
        self, target_date: date, *, corp_cls: str | None = None
    ) -> PollResult:
        started_at = self._now()
        run_id = self.store.start_poll_run(
            target_date=target_date.isoformat(),
            corp_cls=corp_cls,
            started_at=started_at,
        )
        pages_fetched = 0
        records_seen = 0
        new_receipts = 0
        new_observations = 0
        try:
            for page in self.source.iter_disclosure_pages(
                target_date,
                corp_cls=corp_cls,
                page_count=self.policy.page_count,
            ):
                page_new, page_observations = self.store.persist_poll_page(
                    poll_run_id=run_id,
                    records=page,
                    observed_at=self._now(),
                )
                pages_fetched += 1
                records_seen += len(page)
                new_receipts += page_new
                new_observations += page_observations
        except Exception as exc:
            self.store.fail_poll_run(run_id, failed_at=self._now(), error=exc)
            raise
        self.store.finish_poll_run(run_id, completed_at=self._now())
        return PollResult(
            poll_run_id=run_id,
            pages_fetched=pages_fetched,
            records_seen=records_seen,
            new_receipts=new_receipts,
            new_observations=new_observations,
        )

    @staticmethod
    def _relation_receipts(
        disclosure_action: str,
        relation: DartReportRelations,
    ) -> set[str]:
        receipt_no = relation.receipt_no
        linked = {
            value
            for value in (
                *relation.family_receipt_nos,
                *relation.attachment_receipt_nos,
            )
            if value <= receipt_no
        }
        if disclosure_action in {"cancellation", "completion"}:
            linked.update(
                value for value in relation.related_receipt_nos if value <= receipt_no
            )
        return linked

    def _build_snapshot(self, trigger_receipt_no: str) -> StoredEventSnapshot:
        existing = self.store.get_snapshot(trigger_receipt_no)
        if existing is not None:
            return existing

        trigger = self.store.get_disclosure(trigger_receipt_no)
        if trigger is None:
            raise ValueError(f"raw disclosure is missing for {trigger_receipt_no}")

        receipt_nodes = {trigger_receipt_no}
        relations: dict[str, DartReportRelations] = {}
        inspected: set[str] = set()
        while True:
            remaining = sorted(receipt_nodes - inspected)
            if not remaining:
                break
            receipt_no = remaining[0]
            inspected.add(receipt_no)
            disclosure = self.store.get_disclosure(receipt_no)
            if disclosure is None:
                continue
            action = classify_event_action(disclosure.report_name)
            if action == "original":
                continue
            relation = self.store.get_latest_relationship(receipt_no)
            if relation is None:
                capture = self.source.fetch_report_relations_capture(receipt_no)
                # Commit the raw viewer bytes before they can affect a snapshot.
                self.store.save_relationship_capture(
                    capture,
                    fetched_at=self._now(),
                )
                relation = capture.relations
            relations[receipt_no] = relation
            receipt_nodes.update(self._relation_receipts(action, relation))

        disclosures = tuple(
            disclosure
            for receipt_no in sorted(receipt_nodes)
            if (disclosure := self.store.get_disclosure(receipt_no)) is not None
        )
        result = normalize_disclosures(
            disclosures,
            relationships=relations.values(),
        )
        event_id = result.receipt_to_economic_event_id.get(trigger_receipt_no)
        if event_id is None:
            raise ValueError(f"normalizer omitted trigger receipt {trigger_receipt_no}")
        event = next(
            event for event in result.events if event.economic_event_id == event_id
        )
        return self.store.save_snapshot(
            trigger_receipt_no=trigger_receipt_no,
            event=event,
            normalized_at=self._now(),
        )

    @staticmethod
    def _delivery_id(snapshot: StoredEventSnapshot) -> str:
        return delivery_id_for_snapshot(
            trigger_receipt_no=snapshot.trigger_receipt_no,
            event_sha256=snapshot.event_sha256,
        )

    def _deliver_snapshot(
        self,
        snapshot: StoredEventSnapshot,
        consumer: EventConsumer,
    ) -> bool:
        envelope = EventEnvelope(
            delivery_id=self._delivery_id(snapshot),
            trigger_receipt_no=snapshot.trigger_receipt_no,
            normalized_at=snapshot.normalized_at,
            event=snapshot.event,
        )
        should_deliver = self.store.begin_delivery(
            consumer_name=consumer.consumer_name,
            trigger_receipt_no=snapshot.trigger_receipt_no,
            delivery_id=envelope.delivery_id,
            attempted_at=self._now(),
        )
        if not should_deliver:
            return False
        try:
            consumer.consume(envelope)
        except Exception as exc:
            self.store.fail_delivery(
                consumer_name=consumer.consumer_name,
                trigger_receipt_no=snapshot.trigger_receipt_no,
                failed_at=self._now(),
                error=exc,
            )
            raise
        self.store.finish_delivery(
            consumer_name=consumer.consumer_name,
            trigger_receipt_no=snapshot.trigger_receipt_no,
            delivered_at=self._now(),
        )
        return True

    def process_pending(
        self, *, exclude_receipt_nos: Iterable[str] = ()
    ) -> ProcessingResult:
        self._recover_once()
        processed: list[str] = []
        failed: list[str] = []
        for receipt_no in self.store.pending_receipts(
            limit=self.policy.processing_batch_size,
            exclude_receipt_nos=exclude_receipt_nos,
        ):
            self.store.mark_receipt_processing(
                receipt_no,
                attempted_at=self._now(),
            )
            try:
                snapshot = self._build_snapshot(receipt_no)
                delivery_errors: list[tuple[str, Exception]] = []
                for consumer in self.consumers:
                    try:
                        self._deliver_snapshot(snapshot, consumer)
                    except Exception as exc:
                        delivery_errors.append((consumer.consumer_name, exc))
                if delivery_errors:
                    details = "; ".join(
                        f"{name}: {error}" for name, error in delivery_errors
                    )
                    raise DownstreamDeliveryError(details)
            except Exception as exc:
                self.store.mark_receipt_failed(
                    receipt_no,
                    failed_at=self._now(),
                    error=exc,
                )
                failed.append(receipt_no)
                logger.exception(
                    "live receipt processing failed receipt=%s", receipt_no
                )
                continue
            self.store.mark_receipt_processed(
                receipt_no,
                processed_at=self._now(),
            )
            processed.append(receipt_no)
        return ProcessingResult(
            processed_receipt_nos=tuple(processed),
            failed_receipt_nos=tuple(failed),
            remaining_receipts=self.store.count_unfinished_receipts(),
        )

    def replay(
        self,
        consumer: EventConsumer,
        *,
        trigger_receipt_nos: Iterable[str] | None = None,
    ) -> ReplayResult:
        """Normalize selected historical raw rows and use the live delivery path.

        Explicit receipt numbers may refer either to immutable snapshots or to
        raw rows already present in ``disclosures``. With no explicit selection,
        all existing snapshots are replayed; the method never implicitly scans
        the full historical disclosure table.
        """

        if not consumer.consumer_name.strip():
            raise ValueError("consumer_name must not be empty")
        self._recover_once()
        delivered = 0
        skipped = 0
        failed: list[str] = []
        requested = (
            None
            if trigger_receipt_nos is None
            else tuple(sorted(dict.fromkeys(trigger_receipt_nos)))
        )
        if requested is None:
            candidates: tuple[StoredEventSnapshot | str, ...] = (
                self.store.list_snapshots()
            )
        else:
            candidates = requested
        for candidate in candidates:
            try:
                snapshot = (
                    candidate
                    if isinstance(candidate, StoredEventSnapshot)
                    else self.store.get_snapshot(candidate)
                    or self._build_snapshot(candidate)
                )
                if self._deliver_snapshot(snapshot, consumer):
                    delivered += 1
                else:
                    skipped += 1
            except Exception:
                receipt_no = (
                    candidate.trigger_receipt_no
                    if isinstance(candidate, StoredEventSnapshot)
                    else candidate
                )
                failed.append(receipt_no)
                logger.exception(
                    "historical event replay failed receipt=%s",
                    receipt_no,
                )
        return ReplayResult(
            delivered=delivered,
            skipped=skipped,
            failed_trigger_receipt_nos=tuple(failed),
        )

    def run_cycle(
        self, target_date: date, *, corp_cls: str | None = None
    ) -> CycleResult:
        # Retry durable prior work before touching the provider. A failed poll
        # therefore cannot indefinitely strand already persisted raw events.
        prior = self.process_pending()
        already_attempted = {
            *prior.processed_receipt_nos,
            *prior.failed_receipt_nos,
        }
        poll = self.poll_once(target_date, corp_cls=corp_cls)
        current = self.process_pending(exclude_receipt_nos=already_attempted)
        processing = ProcessingResult(
            processed_receipt_nos=tuple(
                dict.fromkeys(
                    (*prior.processed_receipt_nos, *current.processed_receipt_nos)
                )
            ),
            failed_receipt_nos=tuple(
                dict.fromkeys((*prior.failed_receipt_nos, *current.failed_receipt_nos))
            ),
            remaining_receipts=current.remaining_receipts,
        )
        return CycleResult(poll=poll, processing=processing)

    def run_forever(
        self,
        *,
        corp_cls: str | None = None,
        target_date: date | None = None,
    ) -> None:
        while True:
            cycle_started = self.monotonic()
            current_target = target_date or datetime.now(ZoneInfo("Asia/Seoul")).date()
            try:
                result = self.run_cycle(current_target, corp_cls=corp_cls)
                logger.info(
                    "DART cycle date=%s records=%d new=%d processed=%d failed=%d pending=%d",
                    current_target,
                    result.poll.records_seen,
                    result.poll.new_receipts,
                    len(result.processing.processed_receipt_nos),
                    len(result.processing.failed_receipt_nos),
                    result.processing.remaining_receipts,
                )
            except Exception:
                logger.exception("DART poll cycle failed date=%s", current_target)
            elapsed = self.monotonic() - cycle_started
            self.sleeper(max(0.0, self.policy.poll_interval_seconds - elapsed))
