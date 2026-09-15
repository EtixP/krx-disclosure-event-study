from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from typing import Literal, Mapping, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Scalar: TypeAlias = bool | int | float | str
PredicateValue: TypeAlias = Scalar | tuple[Scalar, ...]


def require_utc(value: datetime, *, field: str = "experiment timestamp") -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must use UTC")
    return value.astimezone(timezone.utc)


def _sorted_unique(values: tuple, *, field: str, key=None) -> tuple:
    if not values:
        raise ValueError(f"{field} must not be empty")
    keys = [key(value) if key is not None else value for value in values]
    if len(keys) != len(set(keys)):
        raise ValueError(f"{field} must not contain duplicates")
    if keys != sorted(keys):
        raise ValueError(f"{field} must use deterministic sorted order")
    return values


def _finite_scalar(value: PredicateValue, *, field: str) -> PredicateValue:
    values = value if isinstance(value, tuple) else (value,)
    if not values:
        raise ValueError(f"{field} collection must not be empty")
    for item in values:
        if isinstance(item, str) and not item.strip():
            raise ValueError(f"{field} strings must not be blank")
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError(f"{field} numbers must be finite")
    return value


class FrozenSpecificationModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        strict=True,
        extra="forbid",
        str_strip_whitespace=True,
    )


class EventDefinition(FrozenSpecificationModel):
    event_types: tuple[str, ...] = Field(min_length=1)
    markets: tuple[Literal["KOSDAQ", "KOSPI"], ...] = Field(min_length=1)
    event_statuses: tuple[
        Literal["active", "amended", "cancelled", "completed", "correction_required"],
        ...,
    ] = Field(min_length=1)
    lineage_statuses: tuple[
        Literal["complete", "partial", "self_contained", "unresolved"], ...
    ] = Field(min_length=1)
    normalization_version: str = Field(min_length=1, max_length=64)

    @field_validator("event_types", "markets", "event_statuses", "lineage_statuses")
    @classmethod
    def deterministic_sets(cls, values: tuple, info) -> tuple:
        if info.field_name == "event_types" and any(
            not value.strip() or len(value) > 128 for value in values
        ):
            raise ValueError("event types must be nonblank and at most 128 characters")
        return _sorted_unique(values, field=info.field_name)


class FeatureDefinition(FrozenSpecificationModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    source: Literal["economic_event", "historical_context", "significance"]
    source_version: str = Field(min_length=1, max_length=64)
    field_path: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.\[\]-]*$")
    value_type: Literal["boolean", "category", "integer", "number", "timestamp"]
    missing_policy: Literal["exclude", "fail"]
    decision_time_only: Literal[True] = True


class Predicate(FrozenSpecificationModel):
    field_path: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.\[\]-]*$")
    operator: Literal["eq", "gt", "gte", "in", "lt", "lte", "ne", "not_in"]
    value: PredicateValue

    @model_validator(mode="after")
    def operator_matches_value(self) -> "Predicate":
        _finite_scalar(self.value, field="predicate value")
        is_collection = isinstance(self.value, tuple)
        if self.operator in {"in", "not_in"} and not is_collection:
            raise ValueError(f"{self.operator} predicates require a tuple value")
        if self.operator not in {"in", "not_in"} and is_collection:
            raise ValueError(f"{self.operator} predicates require a scalar value")
        if is_collection:
            keys = [(type(item).__name__, repr(item)) for item in self.value]
            if len({type(item) for item in self.value}) != 1:
                raise ValueError("predicate collection values must share one type")
            if len(keys) != len(set(keys)) or keys != sorted(keys):
                raise ValueError(
                    "predicate collection values must be unique and sorted"
                )
        elif self.operator in {"gt", "gte", "lt", "lte"} and (
            isinstance(self.value, bool) or not isinstance(self.value, (int, float))
        ):
            raise ValueError(f"{self.operator} predicates require a numeric value")
        return self


class EligibilityRule(FrozenSpecificationModel):
    rule_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    predicate: Predicate
    on_missing: Literal["exclude", "fail"]


class ExclusionRule(FrozenSpecificationModel):
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")
    predicate: Predicate
    reason: str = Field(min_length=1, max_length=500)


class RuleParameter(FrozenSpecificationModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    value: Scalar

    @field_validator("value")
    @classmethod
    def finite_value(cls, value: Scalar) -> Scalar:
        return _finite_scalar(value, field="rule parameter")


class ExecutionRule(FrozenSpecificationModel):
    rule_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    rule_version: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")
    parameters: tuple[RuleParameter, ...] = ()

    @field_validator("parameters")
    @classmethod
    def deterministic_parameters(
        cls, values: tuple[RuleParameter, ...]
    ) -> tuple[RuleParameter, ...]:
        if not values:
            return values
        return _sorted_unique(values, field="parameters", key=lambda value: value.name)


class BenchmarkMapping(FrozenSpecificationModel):
    market: Literal["KOSDAQ", "KOSPI"]
    symbol: str = Field(min_length=1, max_length=64)


class BenchmarkDefinition(FrozenSpecificationModel):
    benchmark_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    source: str = Field(min_length=1, max_length=128)
    source_version: str = Field(min_length=1, max_length=64)
    market_mappings: tuple[BenchmarkMapping, ...] = Field(min_length=1)
    alignment: Literal["exact_trading_date"]
    return_type: Literal["price_return"]
    missing_data_policy: Literal["fail"]

    @field_validator("market_mappings")
    @classmethod
    def deterministic_mappings(
        cls, values: tuple[BenchmarkMapping, ...]
    ) -> tuple[BenchmarkMapping, ...]:
        return _sorted_unique(
            values,
            field="market_mappings",
            key=lambda value: value.market,
        )


class CostAssumptions(FrozenSpecificationModel):
    model_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    model_version: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")
    currency: Literal["KRW"] = "KRW"
    commission_per_side: float = Field(ge=0, le=1)
    vat_on_commission: float = Field(ge=0, le=1)
    slippage_bps_per_side: float = Field(ge=0, le=10_000)
    tax_policy_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    tax_policy_version: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")

    @field_validator(
        "commission_per_side", "vat_on_commission", "slippage_bps_per_side"
    )
    @classmethod
    def finite_cost(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("cost assumptions must be finite")
        return value


class OutcomeCriterion(FrozenSpecificationModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    metric: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    operator: Literal["gt", "gte", "lt", "lte"]
    threshold: float

    @field_validator("threshold")
    @classmethod
    def finite_threshold(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("criterion thresholds must be finite")
        return value


class EvaluationPlan(FrozenSpecificationModel):
    evaluation_end: datetime
    primary_metric: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    minimum_events: int = Field(ge=1)
    minimum_issuers: int = Field(ge=1)
    success_criteria: tuple[OutcomeCriterion, ...] = Field(min_length=1)
    failure_criteria: tuple[OutcomeCriterion, ...] = Field(min_length=1)
    success_criteria_policy: Literal["all"] = "all"
    failure_criteria_policy: Literal["any"] = "any"
    simultaneous_match_policy: Literal["failure"] = "failure"
    inconclusive_policy: Literal["report_inconclusive"] = "report_inconclusive"

    @field_validator("evaluation_end")
    @classmethod
    def utc_evaluation_end(cls, value: datetime) -> datetime:
        return require_utc(value, field="evaluation_end")

    @field_validator("success_criteria", "failure_criteria")
    @classmethod
    def deterministic_criteria(
        cls, values: tuple[OutcomeCriterion, ...], info
    ) -> tuple[OutcomeCriterion, ...]:
        return _sorted_unique(
            values,
            field=info.field_name,
            key=lambda value: value.name,
        )

    @model_validator(mode="after")
    def evaluation_is_consistent(self) -> "EvaluationPlan":
        if self.minimum_issuers > self.minimum_events:
            raise ValueError("minimum issuers cannot exceed minimum events")
        criterion_metrics = {
            criterion.metric
            for criterion in (*self.success_criteria, *self.failure_criteria)
        }
        if self.primary_metric not in criterion_metrics:
            raise ValueError("primary metric must appear in evaluation criteria")
        success_signatures = {
            (criterion.metric, criterion.operator, criterion.threshold)
            for criterion in self.success_criteria
        }
        failure_signatures = {
            (criterion.metric, criterion.operator, criterion.threshold)
            for criterion in self.failure_criteria
        }
        if success_signatures & failure_signatures:
            raise ValueError(
                "success and failure criteria must not contain identical conditions"
            )
        return self


class ExperimentSpecification(FrozenSpecificationModel):
    """Complete, hashable definition frozen before prospective observation."""

    specification_schema_version: Literal["m2.1-v1"] = "m2.1-v1"
    experiment_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,63}$")
    version: int = Field(ge=1)
    supersedes_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    name: str = Field(min_length=1, max_length=200)
    objective: str = Field(min_length=1, max_length=1_000)
    created_at: datetime
    historical_cutoff: datetime
    forward_test_start: datetime
    event_definition: EventDefinition
    features: tuple[FeatureDefinition, ...] = Field(min_length=1)
    benchmark: BenchmarkDefinition
    eligibility_rules: tuple[EligibilityRule, ...] = ()
    entry_rule: ExecutionRule
    exit_rule: ExecutionRule
    costs: CostAssumptions
    exclusions: tuple[ExclusionRule, ...] = ()
    evaluation: EvaluationPlan

    @field_validator("created_at", "historical_cutoff", "forward_test_start")
    @classmethod
    def utc_timestamps(cls, value: datetime, info) -> datetime:
        return require_utc(value, field=info.field_name)

    @field_validator("features")
    @classmethod
    def deterministic_features(
        cls, values: tuple[FeatureDefinition, ...]
    ) -> tuple[FeatureDefinition, ...]:
        return _sorted_unique(values, field="features", key=lambda value: value.name)

    @field_validator("eligibility_rules")
    @classmethod
    def deterministic_eligibility(
        cls, values: tuple[EligibilityRule, ...]
    ) -> tuple[EligibilityRule, ...]:
        if not values:
            return values
        return _sorted_unique(
            values,
            field="eligibility_rules",
            key=lambda value: value.rule_id,
        )

    @field_validator("exclusions")
    @classmethod
    def deterministic_exclusions(
        cls, values: tuple[ExclusionRule, ...]
    ) -> tuple[ExclusionRule, ...]:
        if not values:
            return values
        return _sorted_unique(
            values,
            field="exclusions",
            key=lambda value: value.code,
        )

    @model_validator(mode="after")
    def specification_is_consistent(self) -> "ExperimentSpecification":
        if self.version == 1 and self.supersedes_sha256 is not None:
            raise ValueError("version 1 cannot supersede another specification")
        if self.version > 1 and self.supersedes_sha256 is None:
            raise ValueError("later versions must record the superseded SHA-256")
        if self.historical_cutoff > self.created_at:
            raise ValueError("historical cutoff cannot follow specification creation")
        if self.created_at >= self.forward_test_start:
            raise ValueError("specification must be created before forward testing")
        if self.evaluation.evaluation_end <= self.forward_test_start:
            raise ValueError("evaluation end must follow forward-test start")

        mapped_markets = {mapping.market for mapping in self.benchmark.market_mappings}
        missing_markets = set(self.event_definition.markets) - mapped_markets
        if missing_markets:
            raise ValueError(
                "benchmark mappings are missing event markets: "
                + ", ".join(sorted(missing_markets))
            )
        return self

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def new_version(
        self,
        *,
        created_at: datetime,
        forward_test_start: datetime,
        historical_cutoff: datetime | None = None,
        changes: Mapping[str, object] | None = None,
    ) -> "ExperimentSpecification":
        protected = {
            "specification_schema_version",
            "experiment_id",
            "version",
            "supersedes_sha256",
            "created_at",
            "historical_cutoff",
            "forward_test_start",
        }
        updates = dict(changes or {})
        forbidden = protected.intersection(updates)
        unknown = set(updates) - set(type(self).model_fields)
        if forbidden:
            raise ValueError(
                "revision changes cannot override identity/chronology fields: "
                + ", ".join(sorted(forbidden))
            )
        if unknown:
            raise ValueError("unknown revision fields: " + ", ".join(sorted(unknown)))
        created = require_utc(created_at, field="created_at")
        started = require_utc(forward_test_start, field="forward_test_start")
        if created <= self.created_at:
            raise ValueError("a new version must be created after its predecessor")
        if started <= self.forward_test_start:
            raise ValueError("a new version must start after its predecessor")

        payload = self.model_dump(mode="python")
        payload.update(updates)
        payload.update(
            {
                "version": self.version + 1,
                "supersedes_sha256": self.sha256(),
                "created_at": created,
                "historical_cutoff": historical_cutoff or self.historical_cutoff,
                "forward_test_start": started,
            }
        )
        return type(self).model_validate(payload)
