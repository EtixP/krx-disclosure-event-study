"""Resumable prospective minute-bar collection for selected live events."""

from __future__ import annotations

import math
import time as time_module
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from kdtb.data.kis_intraday import (
    KIS_MINUTE_PAGE_SIZE,
    IntradayBarSource,
)
from kdtb.live.market_store import MarketDataStore


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class IntradayCollectorPolicy:
    max_pages_per_target: int = 16
    max_targets_per_run: int = 100
    request_interval_seconds: float = 0.1

    def __post_init__(self) -> None:
        for name, value in (
            ("max_pages_per_target", self.max_pages_per_target),
            ("max_targets_per_run", self.max_targets_per_run),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        interval = self.request_interval_seconds
        if isinstance(interval, bool) or not isinstance(interval, (int, float)):
            raise ValueError("request_interval_seconds must be finite and nonnegative")
        interval = float(interval)
        if not math.isfinite(interval) or interval < 0:
            raise ValueError("request_interval_seconds must be finite and nonnegative")
        object.__setattr__(self, "request_interval_seconds", interval)


@dataclass(frozen=True)
class IntradayCollectionResult:
    recovered_runs: int
    requeued_targets: int
    completed_target_ids: tuple[str, ...]
    failed_target_ids: tuple[str, ...]
    observations_seen: int
    remaining_targets: int


class IntradayMarketCollector:
    """Collect exact provider rows; never interpolate or synthesize bars."""

    def __init__(
        self,
        *,
        source: IntradayBarSource,
        store: MarketDataStore,
        policy: IntradayCollectorPolicy | None = None,
        clock: Callable[[], datetime] = utc_now,
        sleeper: Callable[[float], None] = time_module.sleep,
    ) -> None:
        self.source = source
        self.store = store
        self.policy = policy or IntradayCollectorPolicy()
        self.clock = clock
        self.sleeper = sleeper

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("collector clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)

    def _collect_target(self, run_id: int, target, initial_cursor) -> int:
        cursor = initial_cursor
        observations_seen = 0
        for page_number in range(1, self.policy.max_pages_per_target + 1):
            capture = self.source.fetch_minute_page(
                target.stock_code,
                target.target_date,
                cursor,
                page_number,
            )
            if (
                capture.request.page_number != page_number
                or capture.request.through_time != cursor
            ):
                raise RuntimeError(
                    "provider capture request differs from collector cursor"
                )
            capture_id = self.store.persist_capture(run_id, capture)
            page = self.store.normalize_capture(capture_id)
            observations_seen += len(page.observations)
            if page.source_row_count < KIS_MINUTE_PAGE_SIZE:
                return observations_seen
            if not page.observations:
                raise RuntimeError("full KIS page normalized to no observations")
            earliest = min(
                observation.market_timestamp for observation in page.observations
            )
            next_cursor_datetime = earliest - timedelta(seconds=1)
            if next_cursor_datetime.date() < target.target_date:
                return observations_seen
            next_cursor = next_cursor_datetime.time().replace(tzinfo=None)
            if next_cursor >= cursor:
                raise RuntimeError("KIS pagination cursor did not move backward")
            cursor = next_cursor
            if self.policy.request_interval_seconds:
                self.sleeper(self.policy.request_interval_seconds)
        raise RuntimeError("KIS minute pagination exceeded the configured page limit")

    def run_pending(self, *, retry_failed: bool = False) -> IntradayCollectionResult:
        recovered = self.store.recover_interrupted(recovered_at=self._now())
        requeued = self.store.requeue_failed() if retry_failed else 0
        completed: list[str] = []
        failed: list[str] = []
        total_observations = 0
        for _ in range(self.policy.max_targets_per_run):
            work = self.store.claim_next(started_at=self._now())
            if work is None:
                break
            try:
                self._collect_target(work.run_id, work.target, work.initial_cursor)
                count = self.store.finish_run(
                    work.run_id,
                    finished_at=self._now(),
                )
            except Exception as exc:
                self.store.fail_run(work.run_id, failed_at=self._now(), error=exc)
                failed.append(work.target.target_id)
            else:
                total_observations += count
                completed.append(work.target.target_id)
        return IntradayCollectionResult(
            recovered_runs=recovered,
            requeued_targets=requeued,
            completed_target_ids=tuple(completed),
            failed_target_ids=tuple(failed),
            observations_seen=total_observations,
            remaining_targets=self.store.pending_count(),
        )
