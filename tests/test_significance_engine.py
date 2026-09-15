from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from kdtb.schemas.economic_event import (
    EconomicEvent,
    EventIssuer,
    EventSourceProvenance,
)
from kdtb.schemas.significance import (
    ComparableSignificanceDataset,
    ComparableSignificanceObservation,
)
from kdtb.significance import SignificanceEngine, SignificancePolicy


def _event(
    receipt_no: str,
    *,
    event_type: str = "major_supply_contract",
    corp_code: str = "00126380",
    market: str = "KOSPI",
    fields: dict[str, object] | None = None,
    receipt_time: datetime | None = None,
) -> EconomicEvent:
    receipt_time = receipt_time or datetime.strptime(receipt_no[:8], "%Y%m%d")
    return EconomicEvent(
        economic_event_id=f"dart:{receipt_no}",
        event_type=event_type,
        issuer=EventIssuer(
            corp_code=corp_code,
            corp_name=f"issuer-{corp_code}",
            stock_code="005930",
        ),
        market=market,
        primary_receipt_no=receipt_no,
        original_timestamp=receipt_time,
        latest_update_timestamp=receipt_time,
        status="active",
        normalized_fields=fields or {},
        source_provenance=(
            EventSourceProvenance(
                receipt_no=receipt_no,
                report_name="단일판매ㆍ공급계약체결",
                receipt_timestamp=receipt_time,
                source="DART",
                action="original",
                relationship="primary",
                raw_payload_sha256="a" * 64,
            ),
        ),
        lineage_status="self_contained",
    )


def _known_at(receipt_no: str, hour: int = 12) -> datetime:
    return datetime.strptime(receipt_no[:8], "%Y%m%d").replace(
        hour=hour,
        tzinfo=timezone.utc,
    )


def _observation(
    event_receipt_no: str,
    *,
    value: float,
    source_receipt_no: str | None = None,
    corp_code: str = "OTHER",
    market: str = "KOSPI",
    event_type: str = "major_supply_contract",
    known_at: datetime | None = None,
) -> ComparableSignificanceObservation:
    source_receipt_no = source_receipt_no or event_receipt_no
    return ComparableSignificanceObservation(
        economic_event_id=f"dart:{event_receipt_no}",
        event_type=event_type,
        market=market,
        issuer_corp_code=corp_code,
        source_receipt_no=source_receipt_no,
        known_at=known_at or _known_at(source_receipt_no),
        metric="contract_to_revenue_ratio",
        value=value,
    )


def test_supply_contract_measurement_is_interpretable_and_deterministic():
    event = _event(
        "20260907000001",
        fields={
            "contract_value_krw": 34_000_000_000,
            "prior_year_revenue_krw": 100_000_000_000,
            "contract_to_revenue_ratio": 0.34,
        },
    )
    engine = SignificanceEngine()

    first = engine.assess(event, assessed_at=_known_at("20260907000001"))
    second = engine.assess(event, assessed_at=_known_at("20260907000001"))

    assert first == second
    assert first.status == "measured"
    assert first.event_status == "active"
    assert first.lineage_status == "self_contained"
    measurement = first.measurements[0]
    assert measurement.metric == "contract_to_revenue_ratio"
    assert measurement.value == 0.34
    assert measurement.numerator_krw == 34_000_000_000
    assert measurement.denominator_krw == 100_000_000_000
    assert measurement.derivation == "reported_normalized_field"
    assert measurement.historical_comparison.status == "no_comparable_dataset"
    assert measurement.historical_comparison.percentile_rank is None
    assert first.is_trade_recommendation is False
    assert first.trading_recommendation is None
    assert first.explanation[-1] == "No trading recommendation was evaluated."


def test_ratio_can_be_derived_from_normalized_components():
    event = _event(
        "20260907000001",
        fields={
            "contract_value_krw": 5_000_000_000,
            "prior_year_revenue_krw": 100_000_000_000,
        },
    )
    engine = SignificanceEngine()

    result = engine.assess(event, assessed_at=_known_at("20260907000001"))
    observations = engine.observe(
        event,
        known_at=_known_at("20260907000001"),
    )

    assert result.measurements[0].value == 0.05
    assert result.measurements[0].derivation == (
        "derived_from_contract_value_and_prior_year_revenue"
    )
    assert observations[0].value == 0.05
    assert observations[0].economic_event_id == event.economic_event_id


def test_historical_percentile_population_and_midrank_are_explicit():
    current = _event(
        "20260907000001",
        corp_code="CURRENT",
        fields={"contract_to_revenue_ratio": 0.34},
    )
    observations = (
        _observation("20260101000001", value=0.10, corp_code="CURRENT"),
        _observation("20260201000001", value=0.15),
        _observation(
            "20260201000001",
            source_receipt_no="20260801000001",
            value=0.20,
        ),
        _observation("20260301000001", value=0.34, corp_code="CURRENT"),
        _observation("20260401000001", value=0.50),
        # Every row below is deliberately ineligible.
        _observation("20260907000001", value=0.01),
        _observation("20261001000001", value=0.01),
        _observation("20260501000001", value=0.01, market="KOSDAQ"),
        _observation(
            "20260502000001",
            value=0.01,
            event_type="share_buyback",
        ),
        _observation(
            "20260503000001",
            value=0.01,
            known_at=datetime(2026, 9, 8, 12, tzinfo=timezone.utc),
        ),
    )
    policy = SignificancePolicy(
        minimum_comparable_events=4,
        minimum_issuer_events=2,
    )
    engine = SignificanceEngine(policy)
    dataset = ComparableSignificanceDataset(
        dataset_id="fixture-prior-magnitudes-v1",
        observations=observations,
    )
    assessed_at = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)

    result = engine.assess(
        current,
        assessed_at=assessed_at,
        comparable_dataset=dataset,
    )
    reordered = engine.assess(
        current,
        assessed_at=assessed_at,
        comparable_dataset=ComparableSignificanceDataset(
            dataset_id=dataset.dataset_id,
            observations=tuple(reversed(observations)),
        ),
    )

    assert result == reordered
    historical = result.measurements[0].historical_comparison
    issuer = result.measurements[0].issuer_history_comparison
    assert historical.status == "ranked"
    assert historical.event_count == 4
    assert historical.percentile_rank == 62.5
    assert "same event type and market" in historical.population_description
    assert "strictly before the cutoff" in historical.population_description
    assert "count_less + 0.5 * count_equal" in historical.rank_method
    assert historical.dataset_id == "fixture-prior-magnitudes-v1"
    assert historical.dataset_sha256 == dataset.content_sha256()
    assert len(historical.dataset_sha256) == 64
    changed_dataset = ComparableSignificanceDataset(
        dataset_id=dataset.dataset_id,
        observations=(
            _observation("20260101000001", value=0.11, corp_code="CURRENT"),
            *observations[1:],
        ),
    )
    assert changed_dataset.content_sha256() != dataset.content_sha256()
    assert issuer.status == "ranked"
    assert issuer.event_count == 2
    assert issuer.percentile_rank == 75.0


def test_small_comparable_populations_do_not_emit_percentiles():
    current = _event(
        "20260907000001",
        fields={"contract_to_revenue_ratio": 0.34},
    )
    dataset = ComparableSignificanceDataset(
        dataset_id="too-small-v1",
        observations=(_observation("20260101000001", value=0.10),),
    )

    comparison = (
        SignificanceEngine()
        .assess(
            current,
            assessed_at=_known_at("20260907000001"),
            comparable_dataset=dataset,
        )
        .measurements[0]
        .historical_comparison
    )

    assert comparison.status == "insufficient_population"
    assert comparison.event_count == 1
    assert comparison.percentile_rank is None


def test_missing_fundamentals_remain_explicitly_missing_not_zero():
    event = _event("20260907000001")

    result = SignificanceEngine().assess(
        event,
        assessed_at=_known_at("20260907000001"),
    )

    assert result.status == "missing_supported_inputs"
    assert result.measurements == ()
    assert result.missing_inputs == (
        "contract_value_krw",
        "prior_year_revenue_krw",
    )
    assert "replaced with zero" in result.explanation[1]
    assert '"value":0' not in result.model_dump_json()


def test_reported_ratio_does_not_fabricate_missing_components():
    event = _event(
        "20260907000001",
        fields={"contract_to_revenue_ratio": 0.12},
    )

    result = SignificanceEngine().assess(
        event,
        assessed_at=_known_at("20260907000001"),
    )
    measurement = result.measurements[0]

    assert result.status == "measured"
    assert result.missing_inputs == (
        "contract_value_krw",
        "prior_year_revenue_krw",
    )
    assert measurement.numerator_krw is None
    assert measurement.denominator_krw is None


def test_inconsistent_reported_and_computed_ratio_fails():
    event = _event(
        "20260907000001",
        fields={
            "contract_value_krw": 10_000_000_000,
            "prior_year_revenue_krw": 100_000_000_000,
            "contract_to_revenue_ratio": 0.50,
        },
    )

    with pytest.raises(ValueError, match="conflicts with its normalized components"):
        SignificanceEngine().assess(
            event,
            assessed_at=_known_at("20260907000001"),
        )


def test_tiny_component_ratio_uses_true_relative_error():
    event = _event(
        "20260907000001",
        fields={
            "contract_value_krw": 1,
            "prior_year_revenue_krw": 1_000_000_000_000_000,
            "contract_to_revenue_ratio": 5e-14,
        },
    )

    with pytest.raises(ValueError, match="conflicts with its normalized components"):
        SignificanceEngine().assess(
            event,
            assessed_at=_known_at("20260907000001"),
        )


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("contract_to_revenue_ratio", float("nan")),
        ("contract_to_revenue_ratio", float("inf")),
        ("contract_to_revenue_ratio", 0.0),
        ("contract_to_revenue_ratio", True),
        ("contract_value_krw", "1000"),
        ("prior_year_revenue_krw", -1),
    ],
)
def test_invalid_normalized_measurements_fail_instead_of_becoming_missing(
    field_name, value
):
    fields: dict[str, object] = {
        "contract_value_krw": 10_000_000_000,
        "prior_year_revenue_krw": 100_000_000_000,
    }
    fields[field_name] = value
    event = _event("20260907000001", fields=fields)

    with pytest.raises(ValueError):
        SignificanceEngine().assess(
            event,
            assessed_at=_known_at("20260907000001"),
        )


def test_unsupported_event_is_explained_without_a_trade_opinion():
    event = _event(
        "20260907000001",
        event_type="share_buyback",
        fields={"buyback_amount_krw": 100_000_000_000},
    )

    result = SignificanceEngine().assess(
        event,
        assessed_at=_known_at("20260907000001"),
    )

    assert result.status == "unsupported_event_type"
    assert result.measurements == ()
    assert result.trading_recommendation is None
    assert "No reliable M1.3 magnitude extractor" in result.explanation[0]


def test_assessment_rejects_pre_filing_or_naive_decision_times():
    event = _event(
        "20260907000001",
        fields={"contract_to_revenue_ratio": 0.10},
    )
    engine = SignificanceEngine()

    with pytest.raises(ValueError, match="timezone-aware"):
        engine.assess(event, assessed_at=datetime(2026, 9, 7, 12))
    with pytest.raises(ValueError, match="before its latest source timestamp"):
        engine.assess(
            event,
            assessed_at=datetime(2026, 9, 5, 12, tzinfo=timezone.utc),
        )


def test_assessment_and_observation_reject_same_day_future_source_fields():
    event = _event(
        "20260907000001",
        fields={"contract_to_revenue_ratio": 0.10},
        receipt_time=datetime(2026, 9, 7, 15),
    )
    engine = SignificanceEngine()
    noon_kst = datetime(2026, 9, 7, 3, tzinfo=timezone.utc)

    with pytest.raises(ValueError, match="before its latest source timestamp"):
        engine.assess(event, assessed_at=noon_kst)
    with pytest.raises(ValueError, match="before its latest source timestamp"):
        engine.observe(event, known_at=noon_kst)

    at_source_time = datetime(2026, 9, 7, 6, tzinfo=timezone.utc)
    assert engine.assess(event, assessed_at=at_source_time).status == "measured"
    assert len(engine.observe(event, known_at=at_source_time)) == 1


def test_comparable_observation_cannot_predate_its_filing():
    with pytest.raises(ValidationError, match="cannot predate its filing"):
        _observation(
            "20260907000001",
            value=0.10,
            known_at=datetime(2026, 9, 5, 12, tzinfo=timezone.utc),
        )


def test_comparable_primary_receipt_cannot_follow_its_source():
    with pytest.raises(ValidationError, match="primary receipt cannot follow"):
        _observation(
            "20260907000001",
            source_receipt_no="20260801000001",
            value=0.10,
        )


@pytest.mark.parametrize(
    "policy_kwargs",
    [
        {"minimum_comparable_events": 0},
        {"minimum_issuer_events": True},
        {"ratio_relative_tolerance": float("nan")},
        {"ratio_relative_tolerance": float("inf")},
        {"ratio_relative_tolerance": 1.0},
    ],
)
def test_significance_policy_rejects_invalid_bounds(policy_kwargs):
    with pytest.raises(ValueError):
        SignificancePolicy(**policy_kwargs)


def test_live_and_historical_event_serializations_use_identical_feature_logic():
    live_event = _event(
        "20260907000001",
        fields={
            "contract_value_krw": 25_000_000_000,
            "prior_year_revenue_krw": 100_000_000_000,
        },
    )
    replayed_event = EconomicEvent.model_validate_json(live_event.model_dump_json())
    dataset = ComparableSignificanceDataset(
        dataset_id="shared-input-v1",
        observations=tuple(
            _observation(f"20260{month}01000001", value=month / 100)
            for month in range(1, 7)
        ),
    )
    replayed_dataset = ComparableSignificanceDataset.model_validate_json(
        dataset.model_dump_json()
    )
    engine = SignificanceEngine(SignificancePolicy(minimum_comparable_events=5))
    assessed_at = _known_at("20260907000001")

    live_result = engine.assess(
        live_event,
        assessed_at=assessed_at,
        comparable_dataset=dataset,
    )
    replay_result = engine.assess(
        replayed_event,
        assessed_at=assessed_at,
        comparable_dataset=replayed_dataset,
    )

    assert live_result == replay_result
