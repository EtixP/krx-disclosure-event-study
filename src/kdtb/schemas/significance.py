from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from kdtb.schemas.disclosure import Market
from kdtb.schemas.economic_event import EventStatus, LineageStatus

SignificanceMetric = Literal["contract_to_revenue_ratio"]
MeasurementDerivation = Literal[
    "reported_normalized_field",
    "derived_from_contract_value_and_prior_year_revenue",
]
ComparisonScope = Literal[
    "prior_same_event_type_and_market",
    "prior_same_event_type_market_and_issuer",
]
ComparisonStatus = Literal[
    "no_comparable_dataset",
    "insufficient_population",
    "ranked",
]
AssessmentStatus = Literal[
    "measured",
    "missing_supported_inputs",
    "unsupported_event_type",
]

_SEOUL = ZoneInfo("Asia/Seoul")


def _validate_receipt_no(value: str) -> str:
    if len(value) != 14 or not value.isdigit():
        raise ValueError("DART receipt numbers must contain exactly 14 digits")
    return value


def _validate_aware_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("decision-time timestamps must be timezone-aware")
    return value


def _validate_positive_finite(value: float) -> float:
    if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        raise ValueError("significance values must be positive and finite")
    return value


class ComparableSignificanceObservation(BaseModel):
    """One decision-time magnitude observation for a canonical prior event."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    economic_event_id: str
    event_type: str
    market: Market
    issuer_corp_code: str
    source_receipt_no: str
    known_at: datetime
    metric: SignificanceMetric
    value: float

    _source_receipt_no = field_validator("source_receipt_no")(_validate_receipt_no)
    _known_at = field_validator("known_at")(_validate_aware_datetime)
    _value = field_validator("value")(_validate_positive_finite)

    @field_validator("economic_event_id")
    @classmethod
    def validate_event_id(cls, value: str) -> str:
        if not value.startswith("dart:"):
            raise ValueError("economic_event_id must use the canonical DART prefix")
        _validate_receipt_no(value.removeprefix("dart:"))
        return value

    @field_validator("event_type", "issuer_corp_code")
    @classmethod
    def validate_nonempty_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("comparable observation labels must not be empty")
        return value

    @model_validator(mode="after")
    def validate_chronology(self) -> "ComparableSignificanceObservation":
        primary_receipt_no = self.economic_event_id.removeprefix("dart:")
        if primary_receipt_no > self.source_receipt_no:
            raise ValueError(
                "canonical primary receipt cannot follow the observation source"
            )
        receipt_date = datetime.strptime(self.source_receipt_no[:8], "%Y%m%d").date()
        if self.known_at.astimezone(_SEOUL).date() < receipt_date:
            raise ValueError("a significance observation cannot predate its filing")
        return self


class ComparableSignificanceDataset(BaseModel):
    """Versioned, caller-supplied magnitude observations; no returns included."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    dataset_id: str
    observations: tuple[ComparableSignificanceObservation, ...] = ()

    @field_validator("dataset_id")
    @classmethod
    def validate_dataset_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("comparable dataset_id must not be empty")
        return value

    def content_sha256(self) -> str:
        """Hash the full observation multiset independently of input order."""

        records = sorted(
            json.dumps(
                observation.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            for observation in self.observations
        )
        payload = ("[" + ",".join(records) + "]").encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


class SignificanceComparison(BaseModel):
    """Auditable empirical percentile over one precisely described population."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    scope: ComparisonScope
    population_description: str
    dataset_id: str | None
    dataset_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    cutoff_receipt_no: str
    cutoff_known_at: datetime
    minimum_event_count: int = Field(ge=1)
    comparable_event_ids: tuple[str, ...]
    event_count: int = Field(ge=0)
    rank_method: Literal[
        "midrank: 100 * (count_less + 0.5 * count_equal) / event_count"
    ] = "midrank: 100 * (count_less + 0.5 * count_equal) / event_count"
    status: ComparisonStatus
    percentile_rank: float | None = Field(default=None, ge=0, le=100)

    _cutoff_receipt_no = field_validator("cutoff_receipt_no")(_validate_receipt_no)
    _cutoff_known_at = field_validator("cutoff_known_at")(_validate_aware_datetime)

    @field_validator("percentile_rank")
    @classmethod
    def validate_percentile(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("percentile_rank must be finite")
        return value

    @model_validator(mode="after")
    def validate_population(self) -> "SignificanceComparison":
        if len(self.comparable_event_ids) != len(set(self.comparable_event_ids)):
            raise ValueError("comparable events must be unique")
        if self.event_count != len(self.comparable_event_ids):
            raise ValueError("event_count must match comparable_event_ids")
        if self.status == "no_comparable_dataset":
            if (
                self.dataset_id is not None
                or self.dataset_sha256 is not None
                or self.event_count
                or self.percentile_rank is not None
            ):
                raise ValueError("missing datasets cannot claim a population or rank")
        elif self.status == "insufficient_population":
            if (
                self.dataset_id is None
                or self.dataset_sha256 is None
                or self.event_count >= self.minimum_event_count
            ):
                raise ValueError("insufficient populations must be below the minimum")
            if self.percentile_rank is not None:
                raise ValueError("insufficient populations cannot claim a percentile")
        else:
            if self.dataset_id is None or self.dataset_sha256 is None:
                raise ValueError("ranked populations require a dataset")
            if self.event_count < self.minimum_event_count:
                raise ValueError("ranked populations must meet the minimum")
            if self.percentile_rank is None:
                raise ValueError("ranked populations require a percentile")
        return self


class SignificanceMeasurement(BaseModel):
    """A directly interpretable event-magnitude measurement."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    metric: SignificanceMetric
    label: str
    value: float
    unit: Literal["ratio"] = "ratio"
    derivation: MeasurementDerivation
    source_fields: tuple[str, ...]
    numerator_krw: int | None = Field(default=None, gt=0)
    denominator_krw: int | None = Field(default=None, gt=0)
    historical_comparison: SignificanceComparison
    issuer_history_comparison: SignificanceComparison

    _value = field_validator("value")(_validate_positive_finite)

    @model_validator(mode="after")
    def validate_measurement(self) -> "SignificanceMeasurement":
        if not self.source_fields or len(self.source_fields) != len(
            set(self.source_fields)
        ):
            raise ValueError("measurement source_fields must be nonempty and unique")
        if (self.numerator_krw is None) != (self.denominator_krw is None):
            raise ValueError("ratio components must be present together or both absent")
        return self


class EventSignificance(BaseModel):
    """Measurement-only answer to why an event matters; never a trade decision."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    assessment_version: Literal["m1.3-v1"] = "m1.3-v1"
    economic_event_id: str
    event_type: str
    event_status: EventStatus
    lineage_status: LineageStatus
    assessed_at: datetime
    cutoff_receipt_no: str
    status: AssessmentStatus
    measurements: tuple[SignificanceMeasurement, ...]
    missing_inputs: tuple[str, ...]
    limitations: tuple[str, ...]
    explanation: tuple[str, ...]
    is_trade_recommendation: Literal[False] = False
    trading_recommendation: None = None

    _assessed_at = field_validator("assessed_at")(_validate_aware_datetime)
    _cutoff_receipt_no = field_validator("cutoff_receipt_no")(_validate_receipt_no)

    @model_validator(mode="after")
    def validate_assessment(self) -> "EventSignificance":
        if not self.economic_event_id.startswith("dart:"):
            raise ValueError("economic_event_id must use the canonical DART prefix")
        primary_receipt_no = _validate_receipt_no(
            self.economic_event_id.removeprefix("dart:")
        )
        if primary_receipt_no > self.cutoff_receipt_no:
            raise ValueError("assessment cutoff cannot precede its canonical event")
        if self.status == "measured" and not self.measurements:
            raise ValueError("measured significance requires a measurement")
        if self.status != "measured" and self.measurements:
            raise ValueError("unmeasured significance cannot contain measurements")
        if len(self.missing_inputs) != len(set(self.missing_inputs)):
            raise ValueError("missing_inputs must be unique")
        if not self.limitations or not self.explanation:
            raise ValueError("significance output must explain its limitations")
        return self
