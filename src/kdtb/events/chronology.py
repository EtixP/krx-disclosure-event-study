from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from kdtb.schemas.economic_event import EconomicEvent

SEOUL = ZoneInfo("Asia/Seoul")


def as_seoul_timestamp(value: datetime) -> datetime:
    """Interpret source-naive DART timestamps in their Korean local time."""

    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=SEOUL)
    return value.astimezone(SEOUL)


def latest_receipt_no(event: EconomicEvent) -> str:
    receipts = tuple(source.receipt_no for source in event.source_provenance)
    if not receipts:
        raise ValueError("event source provenance is required")
    return max(receipts)


def latest_source_timestamp(event: EconomicEvent) -> datetime:
    timestamps = (
        event.latest_update_timestamp,
        *(source.receipt_timestamp for source in event.source_provenance),
    )
    return max(as_seoul_timestamp(value) for value in timestamps)


def validate_event_available_at(event: EconomicEvent, decision_time: datetime) -> None:
    """Reject a decision made before any source included in the event existed."""

    if decision_time.tzinfo is None or decision_time.utcoffset() is None:
        raise ValueError("decision_time must be timezone-aware")
    if decision_time.astimezone(SEOUL) < latest_source_timestamp(event):
        raise ValueError("decision time is before its latest source timestamp")
