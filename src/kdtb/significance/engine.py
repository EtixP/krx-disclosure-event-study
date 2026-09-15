from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from numbers import Real
from typing import Iterable, Mapping

from kdtb.events.chronology import latest_receipt_no, validate_event_available_at
from kdtb.schemas.economic_event import EconomicEvent
from kdtb.schemas.significance import (
    ComparableSignificanceDataset,
    ComparableSignificanceObservation,
    ComparisonScope,
    EventSignificance,
    SignificanceComparison,
    SignificanceMeasurement,
)

_HISTORICAL_SCOPE: ComparisonScope = "prior_same_event_type_and_market"
_ISSUER_SCOPE: ComparisonScope = "prior_same_event_type_market_and_issuer"


@dataclass(frozen=True)
class SignificancePolicy:
    minimum_comparable_events: int = 20
    minimum_issuer_events: int = 5
    ratio_relative_tolerance: float = 0.05

    def __post_init__(self) -> None:
        for field_name in ("minimum_comparable_events", "minimum_issuer_events"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{field_name} must be a positive integer")
        tolerance = self.ratio_relative_tolerance
        if (
            isinstance(tolerance, bool)
            or not isinstance(tolerance, (int, float))
            or not math.isfinite(float(tolerance))
            or not 0 <= float(tolerance) < 1
        ):
            raise ValueError("ratio_relative_tolerance must be finite in [0, 1)")
        object.__setattr__(self, "ratio_relative_tolerance", float(tolerance))


@dataclass(frozen=True)
class _ExtractedMeasurement:
    value: float
    derivation: str
    source_fields: tuple[str, ...]
    numerator_krw: int | None
    denominator_krw: int | None
    missing_inputs: tuple[str, ...]


def _optional_positive_integer(
    fields: Mapping[str, object], field_name: str
) -> int | None:
    value = fields.get(field_name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer when present")
    return value


def _optional_positive_ratio(
    fields: Mapping[str, object], field_name: str
) -> float | None:
    value = fields.get(field_name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{field_name} must be numeric when present")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{field_name} must be positive and finite when present")
    return result


class SignificanceEngine:
    """Pure, deterministic magnitude assessment over canonical events."""

    def __init__(self, policy: SignificancePolicy | None = None) -> None:
        self.policy = policy or SignificancePolicy()

    def _extract_supply_contract(
        self, fields: Mapping[str, object]
    ) -> _ExtractedMeasurement | None:
        contract_value = _optional_positive_integer(fields, "contract_value_krw")
        prior_revenue = _optional_positive_integer(fields, "prior_year_revenue_krw")
        reported_ratio = _optional_positive_ratio(fields, "contract_to_revenue_ratio")
        missing = tuple(
            name
            for name, value in (
                ("contract_value_krw", contract_value),
                ("prior_year_revenue_krw", prior_revenue),
            )
            if value is None
        )

        computed_ratio = (
            contract_value / prior_revenue
            if contract_value is not None and prior_revenue is not None
            else None
        )
        if reported_ratio is not None and computed_ratio is not None:
            relative_difference = abs(reported_ratio - computed_ratio) / computed_ratio
            if relative_difference > self.policy.ratio_relative_tolerance:
                raise ValueError(
                    "contract_to_revenue_ratio conflicts with its normalized components"
                )

        if reported_ratio is not None:
            source_fields = ["contract_to_revenue_ratio"]
            if computed_ratio is not None:
                source_fields.extend(("contract_value_krw", "prior_year_revenue_krw"))
            return _ExtractedMeasurement(
                value=reported_ratio,
                derivation="reported_normalized_field",
                source_fields=tuple(source_fields),
                numerator_krw=contract_value if computed_ratio is not None else None,
                denominator_krw=prior_revenue if computed_ratio is not None else None,
                missing_inputs=missing,
            )
        if computed_ratio is None:
            return None
        return _ExtractedMeasurement(
            value=computed_ratio,
            derivation="derived_from_contract_value_and_prior_year_revenue",
            source_fields=("contract_value_krw", "prior_year_revenue_krw"),
            numerator_krw=contract_value,
            denominator_krw=prior_revenue,
            missing_inputs=(),
        )

    def _extract(
        self, event: EconomicEvent
    ) -> tuple[_ExtractedMeasurement | None, tuple[str, ...]]:
        if event.event_type != "major_supply_contract":
            return None, ()
        measurement = self._extract_supply_contract(event.normalized_fields)
        if measurement is not None:
            return measurement, measurement.missing_inputs
        missing = tuple(
            name
            for name in ("contract_value_krw", "prior_year_revenue_krw")
            if event.normalized_fields.get(name) is None
        )
        return None, missing

    def observe(
        self,
        event: EconomicEvent,
        *,
        known_at: datetime,
    ) -> tuple[ComparableSignificanceObservation, ...]:
        """Project supported event measurements into a comparable dataset row."""

        cutoff_receipt_no = latest_receipt_no(event)
        validate_event_available_at(event, known_at)
        measurement, _ = self._extract(event)
        if measurement is None:
            return ()
        return (
            ComparableSignificanceObservation(
                economic_event_id=event.economic_event_id,
                event_type=event.event_type,
                market=event.market,
                issuer_corp_code=event.issuer.corp_code,
                source_receipt_no=cutoff_receipt_no,
                known_at=known_at,
                metric="contract_to_revenue_ratio",
                value=measurement.value,
            ),
        )

    @staticmethod
    def _latest_eligible_observations(
        event: EconomicEvent,
        *,
        assessed_at: datetime,
        cutoff_receipt_no: str,
        dataset: ComparableSignificanceDataset | None,
    ) -> tuple[ComparableSignificanceObservation, ...]:
        if dataset is None:
            return ()
        latest_by_event: dict[str, ComparableSignificanceObservation] = {}
        for observation in dataset.observations:
            if (
                observation.metric != "contract_to_revenue_ratio"
                or observation.event_type != event.event_type
                or observation.market != event.market
                or observation.economic_event_id == event.economic_event_id
                or observation.source_receipt_no >= cutoff_receipt_no
                or observation.known_at > assessed_at
            ):
                continue
            existing = latest_by_event.get(observation.economic_event_id)
            if existing is None:
                latest_by_event[observation.economic_event_id] = observation
                continue
            candidate_key = (
                observation.source_receipt_no,
                observation.known_at,
            )
            existing_key = (existing.source_receipt_no, existing.known_at)
            if candidate_key > existing_key:
                latest_by_event[observation.economic_event_id] = observation
            elif candidate_key == existing_key and observation != existing:
                raise ValueError(
                    "conflicting comparable observations for one event and vintage"
                )
        return tuple(
            sorted(
                latest_by_event.values(),
                key=lambda item: (item.source_receipt_no, item.economic_event_id),
            )
        )

    @staticmethod
    def _comparison(
        *,
        scope: ComparisonScope,
        observations: Iterable[ComparableSignificanceObservation],
        current_value: float,
        dataset_id: str | None,
        dataset_sha256: str | None,
        cutoff_receipt_no: str,
        assessed_at: datetime,
        minimum_event_count: int,
    ) -> SignificanceComparison:
        values = tuple(observations)
        descriptions = {
            _HISTORICAL_SCOPE: (
                "one latest-known measurement per supplied prior canonical event "
                "with the same event type and market; source receipt is strictly "
                "before the cutoff and known_at is no later than assessed_at"
            ),
            _ISSUER_SCOPE: (
                "one latest-known measurement per supplied prior canonical event "
                "with the same event type, market, and issuer; source receipt is "
                "strictly before the cutoff and known_at is no later than assessed_at"
            ),
        }
        event_ids = tuple(item.economic_event_id for item in values)
        if dataset_id is None:
            status = "no_comparable_dataset"
            percentile = None
        elif len(values) < minimum_event_count:
            status = "insufficient_population"
            percentile = None
        else:
            lower = sum(item.value < current_value for item in values)
            equal = sum(item.value == current_value for item in values)
            percentile = 100.0 * (lower + 0.5 * equal) / len(values)
            status = "ranked"
        return SignificanceComparison(
            scope=scope,
            population_description=descriptions[scope],
            dataset_id=dataset_id,
            dataset_sha256=dataset_sha256,
            cutoff_receipt_no=cutoff_receipt_no,
            cutoff_known_at=assessed_at,
            minimum_event_count=minimum_event_count,
            comparable_event_ids=event_ids,
            event_count=len(values),
            status=status,
            percentile_rank=percentile,
        )

    def assess(
        self,
        event: EconomicEvent,
        *,
        assessed_at: datetime,
        comparable_dataset: ComparableSignificanceDataset | None = None,
    ) -> EventSignificance:
        """Measure one canonical event using only fields known by ``assessed_at``."""

        cutoff_receipt_no = latest_receipt_no(event)
        validate_event_available_at(event, assessed_at)
        extracted, missing_inputs = self._extract(event)
        limitations = (
            "Magnitude measurements use normalized decision-time fields only; missing values are never imputed.",
            "Percentile ranks describe event magnitude, not expected return or trading merit.",
        )

        if event.event_type != "major_supply_contract":
            return EventSignificance(
                economic_event_id=event.economic_event_id,
                event_type=event.event_type,
                event_status=event.status,
                lineage_status=event.lineage_status,
                assessed_at=assessed_at,
                cutoff_receipt_no=cutoff_receipt_no,
                status="unsupported_event_type",
                measurements=(),
                missing_inputs=(),
                limitations=limitations,
                explanation=(
                    f"No reliable M1.3 magnitude extractor is registered for {event.event_type}.",
                    "No trading recommendation was evaluated.",
                ),
            )
        if extracted is None:
            missing_text = ", ".join(missing_inputs) or "supported normalized fields"
            return EventSignificance(
                economic_event_id=event.economic_event_id,
                event_type=event.event_type,
                event_status=event.status,
                lineage_status=event.lineage_status,
                assessed_at=assessed_at,
                cutoff_receipt_no=cutoff_receipt_no,
                status="missing_supported_inputs",
                measurements=(),
                missing_inputs=missing_inputs,
                limitations=limitations,
                explanation=(
                    f"Supply-contract magnitude is unavailable; missing: {missing_text}.",
                    "No missing value was replaced with zero.",
                    "No trading recommendation was evaluated.",
                ),
            )

        eligible = self._latest_eligible_observations(
            event,
            assessed_at=assessed_at,
            cutoff_receipt_no=cutoff_receipt_no,
            dataset=comparable_dataset,
        )
        dataset_id = (
            comparable_dataset.dataset_id if comparable_dataset is not None else None
        )
        dataset_sha256 = (
            comparable_dataset.content_sha256()
            if comparable_dataset is not None
            else None
        )
        historical = self._comparison(
            scope=_HISTORICAL_SCOPE,
            observations=eligible,
            current_value=extracted.value,
            dataset_id=dataset_id,
            dataset_sha256=dataset_sha256,
            cutoff_receipt_no=cutoff_receipt_no,
            assessed_at=assessed_at,
            minimum_event_count=self.policy.minimum_comparable_events,
        )
        issuer = self._comparison(
            scope=_ISSUER_SCOPE,
            observations=(
                item
                for item in eligible
                if item.issuer_corp_code == event.issuer.corp_code
            ),
            current_value=extracted.value,
            dataset_id=dataset_id,
            dataset_sha256=dataset_sha256,
            cutoff_receipt_no=cutoff_receipt_no,
            assessed_at=assessed_at,
            minimum_event_count=self.policy.minimum_issuer_events,
        )
        measurement = SignificanceMeasurement(
            metric="contract_to_revenue_ratio",
            label="Supply contract value / prior-year revenue",
            value=extracted.value,
            derivation=extracted.derivation,
            source_fields=extracted.source_fields,
            numerator_krw=extracted.numerator_krw,
            denominator_krw=extracted.denominator_krw,
            historical_comparison=historical,
            issuer_history_comparison=issuer,
        )
        historical_text = (
            f"{historical.percentile_rank:.1f}th percentile (n={historical.event_count})"
            if historical.percentile_rank is not None
            else f"unranked ({historical.status}, n={historical.event_count})"
        )
        issuer_text = (
            f"{issuer.percentile_rank:.1f}th percentile (n={issuer.event_count})"
            if issuer.percentile_rank is not None
            else f"unranked ({issuer.status}, n={issuer.event_count})"
        )
        return EventSignificance(
            economic_event_id=event.economic_event_id,
            event_type=event.event_type,
            event_status=event.status,
            lineage_status=event.lineage_status,
            assessed_at=assessed_at,
            cutoff_receipt_no=cutoff_receipt_no,
            status="measured",
            measurements=(measurement,),
            missing_inputs=missing_inputs,
            limitations=limitations,
            explanation=(
                f"Supply contract value / prior-year revenue: {extracted.value:.2%}.",
                f"Prior same-type/market magnitude: {historical_text}.",
                f"Issuer-history magnitude: {issuer_text}.",
                "No trading recommendation was evaluated.",
            ),
        )
