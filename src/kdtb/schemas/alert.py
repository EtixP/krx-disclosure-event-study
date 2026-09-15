from __future__ import annotations

import math
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from kdtb.schemas.economic_event import EconomicEvent
from kdtb.schemas.historical_context import HistoricalContextResult
from kdtb.schemas.significance import EventSignificance

AlertContextStatus = Literal["available", "insufficient_history", "unavailable"]
AlertContextReason = Literal[
    "unsupported_event_type",
    "unsupported_market",
    "incomplete_lineage",
    "context_query_failed",
]
ImportantFieldKey = Literal[
    "contract_value_krw",
    "prior_year_revenue_krw",
    "contract_to_revenue_ratio",
]


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("alert timestamps must be timezone-aware")
    return value


class AlertImportantField(BaseModel):
    """One verified economic field selected for deterministic presentation."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    key: ImportantFieldKey
    value: int | float
    unit: Literal["KRW", "ratio"]

    @model_validator(mode="after")
    def field_is_consistent(self) -> "AlertImportantField":
        if isinstance(self.value, bool) or not math.isfinite(float(self.value)):
            raise ValueError("alert field values must be finite numbers")
        if self.value <= 0:
            raise ValueError("alert field values must be positive")
        expected_unit = "ratio" if self.key == "contract_to_revenue_ratio" else "KRW"
        if self.unit != expected_unit:
            raise ValueError("alert field unit does not match its key")
        if self.unit == "KRW" and not isinstance(self.value, int):
            raise ValueError("KRW alert fields must be exact integers")
        if self.unit == "ratio" and not isinstance(self.value, float):
            raise ValueError("ratio alert fields must be floats")
        return self


class AlertHistoricalContext(BaseModel):
    """Historical-context result or a structured explanation of unavailability."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    status: AlertContextStatus
    result: HistoricalContextResult | None = None
    unavailable_reason: AlertContextReason | None = None
    detail: str | None = None

    @model_validator(mode="after")
    def context_is_consistent(self) -> "AlertHistoricalContext":
        if self.status == "unavailable":
            if self.result is not None or self.unavailable_reason is None:
                raise ValueError("unavailable context requires a reason and no result")
            if self.detail is None or not self.detail.strip():
                raise ValueError("unavailable context requires explanatory detail")
        else:
            if self.result is None or self.result.status != self.status:
                raise ValueError("available context status must match its result")
            if self.unavailable_reason is not None or self.detail is not None:
                raise ValueError("available context cannot claim an error")
        return self


class AlertStrategyDisposition(BaseModel):
    """Truthful pre-M2.1 strategy boundary; not a historical strategy signal."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    status: Literal["NO_REGISTERED_STRATEGY"] = "NO_REGISTERED_STRATEGY"
    action: Literal["NO_TRADE"] = "NO_TRADE"
    registered_experiment_id: None = None
    registered_experiment_version: None = None
    basis: Literal["no_active_frozen_experiment_registry"] = (
        "no_active_frozen_experiment_registry"
    )


class ResearchAlert(BaseModel):
    """Immutable structured state rendered by the M1.5 text formatter."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    alert_version: Literal["m1.5-v1"] = "m1.5-v1"
    generation_method: Literal["deterministic_structured_formatter"] = (
        "deterministic_structured_formatter"
    )
    delivery_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    trigger_receipt_no: str = Field(pattern=r"^[0-9]{14}$")
    processed_at: datetime
    assessed_at: datetime
    event: EconomicEvent
    important_fields: tuple[AlertImportantField, ...]
    missing_important_fields: tuple[ImportantFieldKey, ...]
    significance: EventSignificance
    historical_context: AlertHistoricalContext
    strategy: AlertStrategyDisposition
    is_trade_recommendation: Literal[False] = False
    trading_recommendation: None = None

    _processed_at = field_validator("processed_at")(_aware)
    _assessed_at = field_validator("assessed_at")(_aware)

    @model_validator(mode="after")
    def alert_is_consistent(self) -> "ResearchAlert":
        event_receipts = {
            self.event.primary_receipt_no,
            *self.event.related_receipt_nos,
        }
        if self.trigger_receipt_no not in event_receipts:
            raise ValueError("alert trigger must belong to its canonical event")
        # Import lazily because the identity module reaches EconomicEvent
        # through this schema package during module initialization.
        from kdtb.event_identity import delivery_id_for_event

        expected_delivery_id = delivery_id_for_event(
            trigger_receipt_no=self.trigger_receipt_no,
            event=self.event,
        )
        if self.delivery_id != expected_delivery_id:
            raise ValueError(
                "alert delivery ID must match its trigger and canonical event snapshot"
            )
        if self.assessed_at > self.processed_at:
            raise ValueError("alert assessment cannot follow processing time")

        significance_identity = (
            self.significance.economic_event_id,
            self.significance.event_type,
            self.significance.event_status,
            self.significance.lineage_status,
            self.significance.assessed_at,
        )
        event_identity = (
            self.event.economic_event_id,
            self.event.event_type,
            self.event.status,
            self.event.lineage_status,
            self.assessed_at,
        )
        if significance_identity != event_identity:
            raise ValueError("alert significance does not match its event and cutoff")
        # Import lazily because the shared chronology module itself imports the
        # economic-event schema through this package.
        from kdtb.significance.engine import SignificanceEngine

        expected_significance = SignificanceEngine().assess(
            self.event,
            assessed_at=self.assessed_at,
        )
        if self.significance != expected_significance:
            raise ValueError(
                "alert significance must match the verified structured engine"
            )

        expected_fields: list[AlertImportantField] = []
        for measurement in self.significance.measurements:
            if measurement.numerator_krw is not None:
                expected_fields.extend(
                    (
                        AlertImportantField(
                            key="contract_value_krw",
                            value=measurement.numerator_krw,
                            unit="KRW",
                        ),
                        AlertImportantField(
                            key="prior_year_revenue_krw",
                            value=measurement.denominator_krw,
                            unit="KRW",
                        ),
                    )
                )
            expected_fields.append(
                AlertImportantField(
                    key="contract_to_revenue_ratio",
                    value=measurement.value,
                    unit="ratio",
                )
            )
        if self.important_fields != tuple(expected_fields):
            raise ValueError("alert important fields must match significance inputs")
        if self.missing_important_fields != self.significance.missing_inputs:
            raise ValueError("alert missing fields must match significance inputs")

        context = self.historical_context.result
        if context is not None:
            context_identity = (
                context.economic_event_id,
                context.event_type,
                context.market,
                context.event_status,
                context.lineage_status,
                context.assessed_at,
            )
            expected_context_identity = (
                self.event.economic_event_id,
                self.event.event_type,
                self.event.market,
                self.event.status,
                self.event.lineage_status,
                self.assessed_at,
            )
            if context_identity != expected_context_identity:
                raise ValueError(
                    "alert historical context does not match its event and cutoff"
                )
        else:
            from kdtb.context.service import EVENT_TYPE_CATEGORIES

            expected_unavailable_reason: AlertContextReason
            if self.event.event_type not in EVENT_TYPE_CATEGORIES:
                expected_unavailable_reason = "unsupported_event_type"
            elif self.event.market not in {"KOSPI", "KOSDAQ"}:
                expected_unavailable_reason = "unsupported_market"
            elif self.event.lineage_status not in {"self_contained", "complete"}:
                expected_unavailable_reason = "incomplete_lineage"
            else:
                expected_unavailable_reason = "context_query_failed"
            if (
                self.historical_context.unavailable_reason
                != expected_unavailable_reason
            ):
                raise ValueError(
                    "alert context-unavailability reason does not match its event"
                )
        return self
