"""Strict prospective market-data records for M3.1.

These records describe what the collector actually observed.  They do not
infer fills, synthesize missing bars, or turn a provider response into a trade.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MARKET_DATA_SCHEMA_VERSION = "m3.1-v1"
KIS_PROVIDER = "KIS_OPEN_API"
KIS_MINUTE_ENDPOINT = "/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice"
KIS_MINUTE_TR_ID = "FHKST03010230"


def _canonical_json(value: dict[str, object]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_json(value: dict[str, object]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


class IntradayCollectionTarget(BaseModel):
    """Immutable selection of one event, symbol, provider, and market date."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_id: str = Field(pattern=r"^mdt_[0-9a-f]{64}$")
    schema_version: Literal["m3.1-v1"] = MARKET_DATA_SCHEMA_VERSION
    provider: Literal["KIS_OPEN_API"] = KIS_PROVIDER
    provider_endpoint: Literal[
        "/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice"
    ] = KIS_MINUTE_ENDPOINT
    trigger_receipt_no: str = Field(pattern=r"^[0-9]{14}$")
    event_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    economic_event_id: str
    stock_code: str | None = Field(default=None, pattern=r"^[0-9]{6}$")
    source_event_date: date
    event_observed_at: datetime
    event_normalized_at: datetime
    target_date: date
    registered_at: datetime

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "provider": self.provider,
            "provider_endpoint": self.provider_endpoint,
            "trigger_receipt_no": self.trigger_receipt_no,
            "event_sha256": self.event_sha256,
            "economic_event_id": self.economic_event_id,
            "stock_code": self.stock_code,
            "source_event_date": self.source_event_date.isoformat(),
            "event_observed_at": self.event_observed_at.isoformat(),
            "event_normalized_at": self.event_normalized_at.isoformat(),
            "target_date": self.target_date.isoformat(),
        }

    def canonical_json(self) -> str:
        return _canonical_json(self.model_dump(mode="json"))

    @model_validator(mode="after")
    def validate_target(self) -> "IntradayCollectionTarget":
        observed = _aware(self.event_observed_at, "event_observed_at")
        normalized = _aware(self.event_normalized_at, "event_normalized_at")
        registered = _aware(self.registered_at, "registered_at")
        if normalized < observed:
            raise ValueError("event normalization cannot precede first observation")
        if registered < normalized:
            raise ValueError("target registration cannot precede event normalization")
        if self.target_date < self.source_event_date:
            raise ValueError("target date cannot precede the disclosed source date")
        expected = "mdt_" + _sha256_json(self.identity_payload())
        if self.target_id != expected:
            raise ValueError("target_id does not match immutable target identity")
        return self


def make_intraday_collection_target(
    *,
    trigger_receipt_no: str,
    event_sha256: str,
    economic_event_id: str,
    stock_code: str | None,
    source_event_date: date,
    event_observed_at: datetime,
    event_normalized_at: datetime,
    target_date: date,
    registered_at: datetime,
) -> IntradayCollectionTarget:
    payload: dict[str, object] = {
        "schema_version": MARKET_DATA_SCHEMA_VERSION,
        "provider": KIS_PROVIDER,
        "provider_endpoint": KIS_MINUTE_ENDPOINT,
        "trigger_receipt_no": trigger_receipt_no,
        "event_sha256": event_sha256,
        "economic_event_id": economic_event_id,
        "stock_code": stock_code,
        "source_event_date": source_event_date.isoformat(),
        "event_observed_at": event_observed_at.isoformat(),
        "event_normalized_at": event_normalized_at.isoformat(),
        "target_date": target_date.isoformat(),
    }
    return IntradayCollectionTarget(
        target_id="mdt_" + _sha256_json(payload),
        trigger_receipt_no=trigger_receipt_no,
        event_sha256=event_sha256,
        economic_event_id=economic_event_id,
        stock_code=stock_code,
        source_event_date=source_event_date,
        event_observed_at=event_observed_at,
        event_normalized_at=event_normalized_at,
        target_date=target_date,
        registered_at=registered_at,
    )


class IntradayBarObservation(BaseModel):
    """One provider row bound to its exact event target and raw capture."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    observation_id: str = Field(pattern=r"^mdo_[0-9a-f]{64}$")
    schema_version: Literal["m3.1-v1"] = MARKET_DATA_SCHEMA_VERSION
    provider: Literal["KIS_OPEN_API"] = KIS_PROVIDER
    target_id: str = Field(pattern=r"^mdt_[0-9a-f]{64}$")
    stock_code: str = Field(pattern=r"^[0-9]{6}$")
    market_timestamp: datetime
    captured_at: datetime
    capture_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_row_index: int = Field(ge=0)
    open: int = Field(gt=0)
    high: int = Field(gt=0)
    low: int = Field(gt=0)
    close: int = Field(gt=0)
    volume: int = Field(ge=0)
    cumulative_volume: int = Field(ge=0)

    def identity_payload(self) -> dict[str, object]:
        return {
            key: value
            for key, value in self.model_dump(mode="json").items()
            if key != "observation_id"
        }

    def canonical_json(self) -> str:
        return _canonical_json(self.model_dump(mode="json"))

    @model_validator(mode="after")
    def validate_observation(self) -> "IntradayBarObservation":
        market_time = _aware(self.market_timestamp, "market_timestamp")
        captured = _aware(self.captured_at, "captured_at")
        if market_time.utcoffset() != timedelta(hours=9):
            raise ValueError("market_timestamp must use Korea Standard Time")
        if captured.utcoffset() != timedelta(0):
            raise ValueError("captured_at must use UTC")
        if market_time > captured:
            raise ValueError("market timestamp cannot follow the raw capture")
        if self.low > min(self.open, self.close) or self.high < max(
            self.open, self.close
        ):
            raise ValueError("OHLC values are internally inconsistent")
        if self.low > self.high:
            raise ValueError("bar low cannot exceed bar high")
        expected = "mdo_" + _sha256_json(self.identity_payload())
        if self.observation_id != expected:
            raise ValueError("observation_id does not match observation content")
        return self


def make_intraday_bar_observation(**values: object) -> IntradayBarObservation:
    payload = {
        "schema_version": MARKET_DATA_SCHEMA_VERSION,
        "provider": KIS_PROVIDER,
        **values,
    }
    draft = IntradayBarObservation.model_construct(
        observation_id="mdo_" + "0" * 64,
        **payload,
    )
    return IntradayBarObservation(
        observation_id="mdo_" + _sha256_json(draft.identity_payload()),
        **payload,
    )


GapReason = Literal["missing_stock_code", "provider_no_rows"]


class MarketDataGap(BaseModel):
    """A durable, explicit absence of a requestable or returned observation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    gap_id: str = Field(pattern=r"^mdg_[0-9a-f]{64}$")
    schema_version: Literal["m3.1-v1"] = MARKET_DATA_SCHEMA_VERSION
    target_id: str = Field(pattern=r"^mdt_[0-9a-f]{64}$")
    run_id: int | None = Field(default=None, ge=1)
    capture_id: int | None = Field(default=None, ge=1)
    reason: GapReason
    recorded_at: datetime
    capture_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    detail: str

    def identity_payload(self) -> dict[str, object]:
        return {
            key: value
            for key, value in self.model_dump(mode="json").items()
            if key != "gap_id"
        }

    def canonical_json(self) -> str:
        return _canonical_json(self.model_dump(mode="json"))

    @model_validator(mode="after")
    def validate_gap(self) -> "MarketDataGap":
        recorded = _aware(self.recorded_at, "recorded_at")
        if recorded.utcoffset() != timedelta(0):
            raise ValueError("recorded_at must use UTC")
        if self.reason == "missing_stock_code":
            if (
                self.run_id is not None
                or self.capture_id is not None
                or self.capture_sha256 is not None
            ):
                raise ValueError("missing-stock gap cannot claim a provider request")
        elif (
            self.run_id is None
            or self.capture_id is None
            or self.capture_sha256 is None
        ):
            raise ValueError("provider no-row gap requires its run and capture")
        expected = "mdg_" + _sha256_json(self.identity_payload())
        if self.gap_id != expected:
            raise ValueError("gap_id does not match gap content")
        return self


def make_market_data_gap(**values: object) -> MarketDataGap:
    payload = {"schema_version": MARKET_DATA_SCHEMA_VERSION, **values}
    draft = MarketDataGap.model_construct(gap_id="mdg_" + "0" * 64, **payload)
    return MarketDataGap(
        gap_id="mdg_" + _sha256_json(draft.identity_payload()),
        **payload,
    )
