from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from kdtb.event_identity import (
    canonical_event_snapshot,
    delivery_id_for_event,
    event_from_canonical_snapshot,
)
from kdtb.schemas.alert import AlertHistoricalContext
from kdtb.schemas.economic_event import EconomicEvent
from kdtb.schemas.experiment import (
    ExperimentSpecification,
    FeatureDefinition,
    Predicate,
    require_utc,
)
from kdtb.schemas.significance import EventSignificance

DecisionValue = bool | int | float | str
FeatureStatus = Literal[
    "available",
    "missing",
    "source_unavailable",
    "source_version_mismatch",
    "type_mismatch",
]
DecisionDisposition = Literal["eligible", "rejected"]
_PATH_COMPONENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)(?:\[([0-9]+)\])?$")
_MISSING = object()
_INVALID = object()


def _canonical_json(value: object) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _model_sha256(value: BaseModel) -> str:
    return _sha256(_canonical_json(value))


def decision_id_for(
    *,
    experiment_sha256: str,
    decision_input_sha256: str,
) -> str:
    value = f"m2.2:{experiment_sha256}:{decision_input_sha256}"
    return _sha256(value)


def _resolve_path(value: object, field_path: str) -> object:
    current = value
    for raw_component in field_path.split("."):
        match = _PATH_COMPONENT.fullmatch(raw_component)
        if match is None:
            return _MISSING
        key, raw_index = match.groups()
        if isinstance(current, BaseModel):
            current = getattr(current, key, _MISSING)
        elif isinstance(current, dict):
            current = current.get(key, _MISSING)
        else:
            return _MISSING
        if current is _MISSING:
            return _MISSING
        if raw_index is not None:
            if not isinstance(current, (list, tuple)):
                return _MISSING
            index = int(raw_index)
            if index >= len(current):
                return _MISSING
            current = current[index]
    return current


def _valid_feature_value(value: object, value_type: str) -> bool:
    if value_type == "boolean":
        return isinstance(value, bool)
    if value_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if value_type == "number":
        if isinstance(value, bool):
            return False
        if isinstance(value, int):
            return True
        return isinstance(value, float) and math.isfinite(value)
    if value_type == "category":
        return isinstance(value, str)
    if value_type == "timestamp":
        if isinstance(value, datetime):
            return value.tzinfo is not None and value.utcoffset() is not None
        if not isinstance(value, str):
            return False
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return False
        return parsed.tzinfo is not None and parsed.utcoffset() is not None
    return False


def _source_material(
    *,
    source: str,
    event: EconomicEvent,
    significance: EventSignificance,
    historical_context: AlertHistoricalContext,
) -> tuple[object | None, str | None, str]:
    if source == "economic_event":
        _, source_sha256 = canonical_event_snapshot(event)
        return event, event.normalization_version, source_sha256
    if source == "significance":
        return (
            significance,
            significance.assessment_version,
            _model_sha256(significance),
        )
    if source == "historical_context":
        context = historical_context.result
        return (
            context,
            context.context_version if context is not None else None,
            _model_sha256(historical_context),
        )
    raise ValueError(f"unsupported feature source {source}")


class DecisionFeatureSnapshot(BaseModel):
    """One frozen feature value and the exact source boundary that produced it."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    source: Literal["economic_event", "historical_context", "significance"]
    source_version: str = Field(min_length=1, max_length=64)
    actual_source_version: str | None = Field(default=None, max_length=64)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    field_path: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.\[\]-]*$")
    value_type: Literal["boolean", "category", "integer", "number", "timestamp"]
    missing_policy: Literal["exclude", "fail"]
    status: FeatureStatus
    value: DecisionValue | None = None

    @field_validator("value")
    @classmethod
    def finite_value(cls, value: DecisionValue | None) -> DecisionValue | None:
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("decision feature values must be finite")
        return value

    @model_validator(mode="after")
    def status_matches_value(self) -> "DecisionFeatureSnapshot":
        if self.status == "available":
            if self.value is None or not _valid_feature_value(
                self.value, self.value_type
            ):
                raise ValueError("available feature value does not match its type")
        elif self.value is not None:
            raise ValueError("unavailable feature snapshots cannot contain a value")
        return self


def feature_snapshot(
    definition: FeatureDefinition,
    *,
    event: EconomicEvent,
    significance: EventSignificance,
    historical_context: AlertHistoricalContext,
) -> DecisionFeatureSnapshot:
    material, actual_version, source_sha256 = _source_material(
        source=definition.source,
        event=event,
        significance=significance,
        historical_context=historical_context,
    )
    if material is None:
        status: FeatureStatus = "source_unavailable"
        value: DecisionValue | None = None
    elif actual_version != definition.source_version:
        status = "source_version_mismatch"
        value = None
    else:
        resolved = _resolve_path(material, definition.field_path)
        if resolved is _MISSING or resolved is None:
            status = "missing"
            value = None
        elif not _valid_feature_value(resolved, definition.value_type):
            status = "type_mismatch"
            value = None
        else:
            status = "available"
            value = (
                resolved.isoformat()
                if definition.value_type == "timestamp"
                else resolved
            )
    return DecisionFeatureSnapshot(
        name=definition.name,
        source=definition.source,
        source_version=definition.source_version,
        actual_source_version=actual_version,
        source_sha256=source_sha256,
        field_path=definition.field_path,
        value_type=definition.value_type,
        missing_policy=definition.missing_policy,
        status=status,
        value=value,
    )


class DecisionInputSnapshot(BaseModel):
    """Canonical M1 event intelligence exactly as available at decision time."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    input_schema_version: Literal["m2.2-v1"] = "m2.2-v1"
    delivery_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    trigger_receipt_no: str = Field(pattern=r"^[0-9]{14}$")
    event_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    event_snapshot_json: str = Field(min_length=1)
    normalized_at: datetime
    assessed_at: datetime
    significance: EventSignificance
    historical_context: AlertHistoricalContext
    features: tuple[DecisionFeatureSnapshot, ...] = Field(min_length=1)

    @property
    def event(self) -> EconomicEvent:
        """Reconstruct the exact M1.2 event retained as source JSON."""

        return event_from_canonical_snapshot(self.event_snapshot_json)

    @field_validator("normalized_at", "assessed_at")
    @classmethod
    def utc_timestamps(cls, value: datetime, info) -> datetime:
        return require_utc(value, field=info.field_name)

    @model_validator(mode="after")
    def inputs_are_consistent(self) -> "DecisionInputSnapshot":
        if self.assessed_at != self.normalized_at:
            raise ValueError(
                "forward assessment must equal snapshot normalization time"
            )
        try:
            event = self.event
        except Exception as error:
            raise ValueError("decision event snapshot JSON is invalid") from error
        actual_snapshot_json, actual_event_sha256 = canonical_event_snapshot(event)
        if self.event_snapshot_json != actual_snapshot_json:
            raise ValueError("decision event snapshot JSON is not canonical")
        event_receipts = {
            event.primary_receipt_no,
            *event.related_receipt_nos,
        }
        if self.trigger_receipt_no not in event_receipts:
            raise ValueError("decision trigger must belong to its canonical event")
        if self.event_sha256 != actual_event_sha256:
            raise ValueError("decision event hash does not match its event snapshot")
        expected_delivery_id = delivery_id_for_event(
            trigger_receipt_no=self.trigger_receipt_no,
            event=event,
        )
        if self.delivery_id != expected_delivery_id:
            raise ValueError("decision delivery ID does not match its event snapshot")
        from kdtb.events.chronology import validate_event_available_at

        validate_event_available_at(event, self.assessed_at)

        from kdtb.significance.engine import SignificanceEngine

        expected_significance = SignificanceEngine().assess(
            event,
            assessed_at=self.assessed_at,
        )
        if self.significance != expected_significance:
            raise ValueError(
                "decision significance does not match its event and cutoff"
            )
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
            event_identity = (
                event.economic_event_id,
                event.event_type,
                event.market,
                event.status,
                event.lineage_status,
                self.assessed_at,
            )
            if context_identity != event_identity:
                raise ValueError(
                    "decision historical context does not match its event and cutoff"
                )
        else:
            from kdtb.context.service import EVENT_TYPE_CATEGORIES

            if event.event_type not in EVENT_TYPE_CATEGORIES:
                expected_reason = "unsupported_event_type"
            elif event.market not in {"KOSPI", "KOSDAQ"}:
                expected_reason = "unsupported_market"
            elif event.lineage_status not in {"self_contained", "complete"}:
                expected_reason = "incomplete_lineage"
            else:
                expected_reason = "context_query_failed"
            if self.historical_context.unavailable_reason != expected_reason:
                raise ValueError(
                    "decision context-unavailability reason does not match its event"
                )

        names = tuple(feature.name for feature in self.features)
        if names != tuple(sorted(names)) or len(names) != len(set(names)):
            raise ValueError("decision features must be unique and sorted")
        expected_features = tuple(
            feature_snapshot(
                FeatureDefinition(
                    name=feature.name,
                    source=feature.source,
                    source_version=feature.source_version,
                    field_path=feature.field_path,
                    value_type=feature.value_type,
                    missing_policy=feature.missing_policy,
                ),
                event=event,
                significance=self.significance,
                historical_context=self.historical_context,
            )
            for feature in self.features
        )
        if self.features != expected_features:
            raise ValueError("decision feature snapshots do not match their sources")
        return self

    def canonical_json(self) -> str:
        return _canonical_json(self)

    def sha256(self) -> str:
        return _sha256(self.canonical_json())


class DecisionRejectionReason(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")
    rule_id: str | None = Field(default=None, min_length=1, max_length=64)
    detail: str = Field(min_length=1, max_length=500)


def _is_number(value: object) -> bool:
    return _valid_feature_value(value, "number")


def _is_numeric_type(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _values_equal(actual: object, expected: object) -> bool | object:
    if _is_numeric_type(actual) or _is_numeric_type(expected):
        if not _is_number(actual) or not _is_number(expected):
            return _INVALID
        return actual == expected
    if type(actual) is not type(expected):
        return _INVALID
    return actual == expected


def _predicate_result(predicate: Predicate, context: dict[str, object]) -> object:
    actual = _resolve_path(context, predicate.field_path)
    if actual is _MISSING or actual is None:
        return None
    if predicate.operator in {"eq", "ne"}:
        result = _values_equal(actual, predicate.value)
        if result is _INVALID:
            return _INVALID
        return result if predicate.operator == "eq" else not result
    if predicate.operator in {"gt", "gte", "lt", "lte"}:
        if not _is_number(actual) or not _is_number(predicate.value):
            return _INVALID
        if predicate.operator == "gt":
            return actual > predicate.value
        if predicate.operator == "gte":
            return actual >= predicate.value
        if predicate.operator == "lt":
            return actual < predicate.value
        return actual <= predicate.value
    if predicate.operator in {"in", "not_in"}:
        expected_values = predicate.value
        if not isinstance(expected_values, tuple):
            return _INVALID
        comparisons = tuple(
            _values_equal(actual, expected) for expected in expected_values
        )
        if any(result is _INVALID for result in comparisons):
            return _INVALID
        result = any(comparisons)
        return result if predicate.operator == "in" else not result
    raise ValueError(f"unsupported predicate operator {predicate.operator}")


def _trigger_source_timestamp(inputs: DecisionInputSnapshot) -> datetime | None:
    from kdtb.events.chronology import as_seoul_timestamp

    for source in inputs.event.source_provenance:
        if source.receipt_no == inputs.trigger_receipt_no:
            return as_seoul_timestamp(source.receipt_timestamp).astimezone(timezone.utc)
    return None


def decision_rejection_reasons(
    specification: ExperimentSpecification,
    inputs: DecisionInputSnapshot,
) -> tuple[DecisionRejectionReason, ...]:
    reasons: list[DecisionRejectionReason] = []

    trigger_timestamp = _trigger_source_timestamp(inputs)
    if trigger_timestamp is None:
        reasons.append(
            DecisionRejectionReason(
                code="TRIGGER_PROVENANCE_MISSING",
                detail="Trigger receipt has no source timestamp in the event snapshot.",
            )
        )
    elif trigger_timestamp < specification.forward_test_start:
        reasons.append(
            DecisionRejectionReason(
                code="PRE_FORWARD_SOURCE",
                detail="Trigger source timestamp precedes the frozen forward start.",
            )
        )

    event_definition = specification.event_definition
    event = inputs.event
    definition_checks = (
        (
            event.event_type in event_definition.event_types,
            "EVENT_TYPE_NOT_INCLUDED",
            "Event type is outside the frozen experiment definition.",
        ),
        (
            event.market in event_definition.markets,
            "MARKET_NOT_INCLUDED",
            "Listing market is outside the frozen experiment definition.",
        ),
        (
            event.status in event_definition.event_statuses,
            "EVENT_STATUS_NOT_INCLUDED",
            "Event status is outside the frozen experiment definition.",
        ),
        (
            event.lineage_status in event_definition.lineage_statuses,
            "LINEAGE_STATUS_NOT_INCLUDED",
            "Lineage status is outside the frozen experiment definition.",
        ),
        (
            event.normalization_version == event_definition.normalization_version,
            "NORMALIZATION_VERSION_MISMATCH",
            "Event normalization version differs from the frozen definition.",
        ),
    )
    reasons.extend(
        DecisionRejectionReason(code=code, detail=detail)
        for passed, code, detail in definition_checks
        if not passed
    )

    available_features: dict[str, object] = {}
    for feature in inputs.features:
        if feature.status == "available":
            available_features[feature.name] = feature.value
            continue
        if feature.status in {"missing", "source_unavailable"}:
            suffix = "EXCLUDED" if feature.missing_policy == "exclude" else "FAILED"
            code = f"FEATURE_MISSING_{suffix}"
        elif feature.status == "source_version_mismatch":
            code = "FEATURE_SOURCE_VERSION_MISMATCH"
        else:
            code = "FEATURE_TYPE_MISMATCH"
        reasons.append(
            DecisionRejectionReason(
                code=code,
                rule_id=feature.name,
                detail=f"Feature {feature.name} is {feature.status} at decision time.",
            )
        )

    context: dict[str, object] = {
        "event": event.model_dump(mode="python"),
        "economic_event": event.model_dump(mode="python"),
        "significance": inputs.significance.model_dump(mode="python"),
        "historical_context": inputs.historical_context.model_dump(mode="python"),
        **available_features,
    }
    for rule in specification.eligibility_rules:
        result = _predicate_result(rule.predicate, context)
        if result is None:
            reasons.append(
                DecisionRejectionReason(
                    code="ELIGIBILITY_INPUT_MISSING",
                    rule_id=rule.rule_id,
                    detail=(
                        f"Eligibility input {rule.predicate.field_path} is missing "
                        f"with {rule.on_missing} policy."
                    ),
                )
            )
        elif result is _INVALID:
            reasons.append(
                DecisionRejectionReason(
                    code="ELIGIBILITY_PREDICATE_INVALID",
                    rule_id=rule.rule_id,
                    detail="Eligibility input type is incompatible with its predicate.",
                )
            )
        elif not result:
            reasons.append(
                DecisionRejectionReason(
                    code="ELIGIBILITY_RULE_NOT_MET",
                    rule_id=rule.rule_id,
                    detail="Frozen eligibility predicate did not match.",
                )
            )

    for exclusion in specification.exclusions:
        result = _predicate_result(exclusion.predicate, context)
        if result is None:
            reasons.append(
                DecisionRejectionReason(
                    code="EXCLUSION_INPUT_MISSING",
                    rule_id=exclusion.code,
                    detail=(
                        f"Exclusion input {exclusion.predicate.field_path} is "
                        "missing; decision fails closed."
                    ),
                )
            )
        elif result is _INVALID:
            reasons.append(
                DecisionRejectionReason(
                    code="EXCLUSION_PREDICATE_INVALID",
                    rule_id=exclusion.code,
                    detail="Exclusion input type is incompatible; decision fails closed.",
                )
            )
        elif result:
            reasons.append(
                DecisionRejectionReason(
                    code=exclusion.code,
                    rule_id=exclusion.code,
                    detail=exclusion.reason,
                )
            )

    unique = {
        (reason.code, reason.rule_id or "", reason.detail): reason for reason in reasons
    }
    return tuple(unique[key] for key in sorted(unique))


class ForwardDecision(BaseModel):
    """One immutable eligibility decision; prospective outcomes live elsewhere."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    decision_schema_version: Literal["m2.2-v1"] = "m2.2-v1"
    decision_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    experiment: ExperimentSpecification
    experiment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    experiment_activated_at: datetime
    inputs: DecisionInputSnapshot
    decision_input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    disposition: DecisionDisposition
    rejection_reasons: tuple[DecisionRejectionReason, ...]
    authorizes_execution: Literal[False] = False
    is_outcome: Literal[False] = False

    @field_validator("experiment_activated_at")
    @classmethod
    def utc_activation(cls, value: datetime) -> datetime:
        return require_utc(value, field="experiment_activated_at")

    @model_validator(mode="after")
    def decision_is_consistent(self) -> "ForwardDecision":
        if self.experiment_sha256 != self.experiment.sha256():
            raise ValueError("decision experiment hash does not match its definition")
        if self.decision_input_sha256 != self.inputs.sha256():
            raise ValueError("decision input hash does not match its snapshot")
        expected_id = decision_id_for(
            experiment_sha256=self.experiment_sha256,
            decision_input_sha256=self.decision_input_sha256,
        )
        if self.decision_id != expected_id:
            raise ValueError("decision ID does not match experiment and input hashes")
        if self.experiment_activated_at > self.experiment.forward_test_start:
            raise ValueError(
                "decision experiment was activated after its forward start"
            )
        if not (
            self.experiment.forward_test_start
            <= self.inputs.assessed_at
            <= self.experiment.evaluation.evaluation_end
        ):
            raise ValueError("decision time is outside the experiment forward window")

        definitions = tuple(
            (
                feature.name,
                feature.source,
                feature.source_version,
                feature.field_path,
                feature.value_type,
                feature.missing_policy,
            )
            for feature in self.experiment.features
        )
        snapshots = tuple(
            (
                feature.name,
                feature.source,
                feature.source_version,
                feature.field_path,
                feature.value_type,
                feature.missing_policy,
            )
            for feature in self.inputs.features
        )
        if snapshots != definitions:
            raise ValueError("decision features do not match the experiment definition")

        expected_reasons = decision_rejection_reasons(self.experiment, self.inputs)
        if self.rejection_reasons != expected_reasons:
            raise ValueError("decision rejection reasons do not match frozen rules")
        expected_disposition: DecisionDisposition = (
            "rejected" if expected_reasons else "eligible"
        )
        if self.disposition != expected_disposition:
            raise ValueError(
                "decision disposition does not match its rejection reasons"
            )
        return self

    def canonical_json(self) -> str:
        return _canonical_json(self)

    def sha256(self) -> str:
        return _sha256(self.canonical_json())
