"""Invented arithmetic/time adversaries; no provider rows or source admission."""

from __future__ import annotations

import copy
import json
import math
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from kdtb.learning.buyback_features import (
    BuybackFeatureFacts,
    BuybackHistoryObservation,
    calculate_buyback_features,
)
from kdtb.schemas.session import TradingSession

UTC = timezone.utc
KST = ZoneInfo("Asia/Seoul")
EPSILON = timedelta(microseconds=1)


def session(day, close=time(15, 30)):
    return TradingSession(
        trading_date=day,
        close_at=datetime.combine(day, close, KST).astimezone(UTC),
    )


def declared_case():
    # Twenty-one declared weekday sessions, not a source-verified calendar.
    days = [
        date(2025, 1, 6) + timedelta(days=i)
        for i in range(29)
        if (date(2025, 1, 6) + timedelta(days=i)).weekday() < 5
    ]
    history = []
    for i, day in enumerate(days):
        item = session(day)
        available = item.close_at + timedelta(minutes=15)
        history.append(
            BuybackHistoryObservation(
                ordinary_security_id="ordinary-A",
                market="KOSPI",
                session=item,
                stock_close=150 if i == 20 else 100,
                stock_close_available_at=available,
                index_close=80 if i == 20 else 100,
                index_close_available_at=available,
                stock_traded_value_krw=10**12 if i == 0 else 9 + i,
                stock_traded_value_krw_available_at=available,
            )
        )
    reference = history[-1].session.close_at
    publication = reference + timedelta(hours=1)
    facts = BuybackFeatureFacts(
        decision_id="decision-A",
        issuer_id="issuer-A",
        ordinary_security_id="ordinary-A",
        market="KOSPI",
        decision_at=datetime(2025, 2, 3, 23, 55, tzinfo=UTC),
        original_publication_lower_at=publication,
        original_publication_upper_at=publication,
        terms_available_at=publication,
        announced_ordinary_shares=20,
        announced_ordinary_value_krw=390,
        listed_ordinary_shares=1000,
        listed_shares_reference_at=reference,
        listed_shares_available_at=reference - timedelta(hours=2),
    )
    return facts, history, session(date(2025, 2, 4))


def replace(item, **updates):
    return type(item).model_validate(item.model_dump() | updates)


def calculate(case):
    facts, history, next_session = case
    return calculate_buyback_features(facts, history, next_session=next_session)


def value(report, name):
    return report["features"][name]["value"]


def reasons(report, name):
    return report["features"][name]["reasons"]


def missing(row, field):
    return replace(row, **{field: None, f"{field}_available_at": None})


def test_hand_calculated_six_values_and_declared_metadata():
    facts, history, boundary = declared_case()
    result = calculate((facts, history, boundary))
    assert value(result, "announced_share_fraction") == pytest.approx(0.02)
    # V1..V20 = 10..29. Their central pair is 19,20; V0 is a large sentinel.
    assert value(result, "announced_value_to_median_turnover_20") == pytest.approx(20)
    assert value(result, "stock_return_20") == pytest.approx(0.5)
    assert value(result, "market_return_20") == pytest.approx(-0.2)
    # Nineteen zero daily returns and one x have sample SD |x|/sqrt(20).
    assert value(result, "stock_daily_volatility_20") == pytest.approx(
        0.5 / math.sqrt(20)
    )
    assert value(result, "market_daily_volatility_20") == pytest.approx(
        0.2 / math.sqrt(20)
    )
    assert all(not feature["reasons"] for feature in result["features"].values())
    assert (
        result["scope"]
        == "OFFLINE_DECLARED_INPUT_CALCULATIONS_ONLY_NO_SOURCE_ADMISSION"
    )
    assert result["decision_id"] == facts.decision_id
    assert result["issuer_id"] == facts.issuer_id
    assert result["ordinary_security_id"] == facts.ordinary_security_id
    assert result["decision_at"] == facts.decision_at.isoformat()
    assert result["reference_session"] == history[-1].session.model_dump(mode="json")
    assert result["next_session"] == boundary.model_dump(mode="json")
    assert result["window"]["declared_session_count"] == 21
    assert result["window"]["daily_return_count"] == 20
    assert result["window"]["turnover_session_count"] == 20
    assert all(
        feature["formula"] and feature["unit"]
        for feature in result["features"].values()
    )
    json.dumps(result, allow_nan=False)


def test_alternating_returns_use_twenty_values_and_sample_ddof_one():
    facts, history, boundary = declared_case()
    history = [
        replace(row, stock_close=100 if i % 2 == 0 else 200)
        for i, row in enumerate(history)
    ]
    result = calculate((facts, history, boundary))
    # Ten returns +1 and ten -1/2: mean 1/4; sum squares 45/4, divided by 19.
    assert value(result, "stock_return_20") == 0
    assert value(result, "stock_daily_volatility_20") == pytest.approx(
        math.sqrt(45 / 76)
    )


@pytest.mark.parametrize("offset", [-EPSILON, timedelta(0)])
def test_original_publication_before_or_at_reference_close_rejects(offset):
    facts, history, boundary = declared_case()
    publication = history[-1].session.close_at + offset
    facts = replace(
        facts,
        original_publication_lower_at=publication,
        original_publication_upper_at=publication,
    )
    with pytest.raises(ValueError, match="reference close"):
        calculate((facts, history, boundary))


def test_publication_after_reference_close_and_next_boundary_after_cutoff_are_valid():
    facts, history, boundary = declared_case()
    publication = history[-1].session.close_at + EPSILON
    facts = replace(
        facts,
        original_publication_lower_at=publication,
        original_publication_upper_at=publication,
    )
    assert boundary.close_at > facts.decision_at
    assert value(
        calculate((facts, history, boundary)), "stock_return_20"
    ) == pytest.approx(0.5)


def test_publication_exactly_next_close_belongs_to_prior_close():
    facts, history, boundary = declared_case()
    facts = replace(
        facts,
        decision_at=facts.decision_at + timedelta(days=1),
        original_publication_lower_at=boundary.close_at,
        original_publication_upper_at=boundary.close_at,
        terms_available_at=boundary.close_at,
    )
    assert calculate((facts, history, boundary))["reference_session"] == history[
        -1
    ].session.model_dump(mode="json")


@pytest.mark.parametrize("straddles", [True, False])
def test_interval_crossing_or_after_next_close_is_unresolved(straddles):
    facts, history, boundary = declared_case()
    facts = replace(
        facts,
        decision_at=facts.decision_at + timedelta(days=1),
        original_publication_lower_at=boundary.close_at
        + (-EPSILON if straddles else EPSILON),
        original_publication_upper_at=boundary.close_at + EPSILON,
        terms_available_at=boundary.close_at + EPSILON,
    )
    with pytest.raises(ValueError, match="reference close"):
        calculate((facts, history, boundary))


def test_later_correction_changes_terms_without_advancing_original_anchor():
    facts, history, boundary = declared_case()
    baseline = calculate((facts, history, boundary))
    corrected = replace(
        facts,
        terms_available_at=facts.decision_at - EPSILON,
        announced_ordinary_shares=40,
        announced_ordinary_value_krw=780,
    )
    result = calculate((corrected, history, boundary))
    assert value(result, "announced_share_fraction") == 2 * value(
        baseline, "announced_share_fraction"
    )
    assert value(result, "announced_value_to_median_turnover_20") == 2 * value(
        baseline, "announced_value_to_median_turnover_20"
    )
    assert result["reference_session"] == baseline["reference_session"]
    assert (
        result["original_publication_interval"]
        == baseline["original_publication_interval"]
    )
    advanced = history[1:] + [
        replace(
            history[-1],
            session=boundary,
            stock_close_available_at=boundary.close_at,
            index_close_available_at=boundary.close_at,
            stock_traded_value_krw_available_at=boundary.close_at,
        )
    ]
    # Even a later decision/correction cannot replace original publication with its time.
    corrected = replace(
        corrected,
        decision_at=corrected.decision_at + timedelta(days=1),
        listed_shares_reference_at=boundary.close_at,
    )
    with pytest.raises(ValueError, match="reference close"):
        calculate((corrected, advanced, session(date(2025, 2, 5))))


def test_exceptional_declared_close_and_preclose_listed_share_availability():
    facts, history, boundary = declared_case()
    exceptional = session(history[-1].session.trading_date, time(16, 30))
    available = exceptional.close_at
    history[-1] = replace(
        history[-1],
        session=exceptional,
        stock_close_available_at=available,
        index_close_available_at=available,
        stock_traded_value_krw_available_at=available,
    )
    publication = exceptional.close_at + EPSILON
    facts = replace(
        facts,
        original_publication_lower_at=publication,
        original_publication_upper_at=publication,
        terms_available_at=publication,
        listed_shares_reference_at=exceptional.close_at,
    )
    assert facts.listed_shares_available_at < exceptional.close_at
    assert (
        calculate((facts, history, boundary))["reference_session"]["close_at"]
        == "2025-02-03T07:30:00Z"
    )


@pytest.mark.parametrize(
    "difference",
    [timedelta(minutes=-1), timedelta(seconds=1), EPSILON, timedelta(hours=9)],
)
def test_decision_requires_exact_085500_kst(difference):
    facts, _, _ = declared_case()
    with pytest.raises(ValueError, match="08:55"):
        replace(facts, decision_at=facts.decision_at + difference)


@pytest.mark.parametrize(
    "field",
    [
        "original_publication_lower_at",
        "original_publication_upper_at",
        "terms_available_at",
        "listed_shares_available_at",
    ],
)
def test_scalar_availability_equal_cutoff_rejects(field):
    facts, _, _ = declared_case()
    with pytest.raises(ValueError):
        replace(facts, **{field: facts.decision_at})


def test_reversed_publication_interval_and_terms_before_original_lower_reject():
    facts, _, _ = declared_case()
    with pytest.raises(ValueError, match="interval"):
        replace(
            facts,
            original_publication_lower_at=facts.original_publication_upper_at + EPSILON,
        )
    with pytest.raises(ValueError, match="applicable terms"):
        replace(facts, terms_available_at=facts.original_publication_lower_at - EPSILON)


@pytest.mark.parametrize(
    "field", ["stock_close", "index_close", "stock_traded_value_krw"]
)
@pytest.mark.parametrize("offset", [timedelta(0), EPSILON])
def test_old_session_future_or_cutoff_vintage_rejects(field, offset):
    facts, history, boundary = declared_case()
    history[3] = replace(
        history[3], **{f"{field}_available_at": facts.decision_at + offset}
    )
    with pytest.raises(ValueError, match="vintage"):
        calculate((facts, history, boundary))


@pytest.mark.parametrize(
    "field", ["stock_close", "index_close", "stock_traded_value_krw"]
)
def test_observation_available_at_close_valid_but_before_close_invalid(field):
    facts, history, boundary = declared_case()
    history[3] = replace(
        history[3], **{f"{field}_available_at": history[3].session.close_at}
    )
    calculate((facts, history, boundary))
    with pytest.raises(ValueError, match="before close"):
        replace(
            history[3],
            **{f"{field}_available_at": history[3].session.close_at - EPSILON},
        )


@pytest.mark.parametrize(
    "field",
    [
        "decision_at",
        "original_publication_lower_at",
        "original_publication_upper_at",
        "terms_available_at",
        "listed_shares_reference_at",
        "listed_shares_available_at",
    ],
)
@pytest.mark.parametrize("kind", ["naive", "non_utc", "string"])
def test_facts_timestamps_are_strict_utc(field, kind):
    facts, _, _ = declared_case()
    current = getattr(facts, field)
    invalid = (
        current.replace(tzinfo=None)
        if kind == "naive"
        else current.astimezone(KST) if kind == "non_utc" else current.isoformat()
    )
    with pytest.raises(ValueError):
        replace(facts, **{field: invalid})


@pytest.mark.parametrize(
    "field", ["stock_close", "index_close", "stock_traded_value_krw"]
)
@pytest.mark.parametrize("kind", ["naive", "non_utc"])
def test_series_vintages_are_strict_utc(field, kind):
    _, history, _ = declared_case()
    current = getattr(history[0], f"{field}_available_at")
    invalid = (
        current.replace(tzinfo=None) if kind == "naive" else current.astimezone(KST)
    )
    with pytest.raises(ValueError):
        replace(history[0], **{f"{field}_available_at": invalid})


@pytest.mark.parametrize(
    "field", ["stock_close", "index_close", "stock_traded_value_krw"]
)
@pytest.mark.parametrize("remove", ["value", "timestamp"])
def test_per_series_value_time_pair_cannot_be_half_missing(field, remove):
    _, history, _ = declared_case()
    with pytest.raises(ValueError, match="both"):
        replace(
            history[4],
            **{field if remove == "value" else f"{field}_available_at": None},
        )


@pytest.mark.parametrize(
    "field", ["listed_ordinary_shares", "listed_shares_available_at"]
)
def test_listed_value_time_pair_cannot_be_half_missing(field):
    facts, _, _ = declared_case()
    with pytest.raises(ValueError, match="both"):
        replace(facts, **{field: None})


@pytest.mark.parametrize(
    "field", ["announced_ordinary_shares", "listed_ordinary_shares"]
)
@pytest.mark.parametrize(
    "invalid", [True, False, -1, 1.0, "1", float("nan"), float("inf")]
)
def test_share_counts_are_nonnegative_strict_integers(field, invalid):
    facts, _, _ = declared_case()
    with pytest.raises(ValueError):
        replace(facts, **{field: invalid})


@pytest.mark.parametrize(
    "field",
    [
        "stock_close",
        "index_close",
        "stock_traded_value_krw",
        "announced_ordinary_value_krw",
    ],
)
@pytest.mark.parametrize(
    "invalid",
    [True, False, -1, "1", float("nan"), float("inf"), -float("inf"), 10**1000],
)
def test_numbers_reject_boolean_string_negative_nonfinite_and_unrepresentable(
    field, invalid
):
    facts, history, _ = declared_case()
    item = facts if field == "announced_ordinary_value_krw" else history[0]
    with pytest.raises(ValueError):
        replace(item, **{field: invalid})


@pytest.mark.parametrize("field", ["stock_close", "index_close"])
def test_zero_prices_are_invalid(field):
    _, history, _ = declared_case()
    with pytest.raises(ValueError):
        replace(history[0], **{field: 0})


@pytest.mark.parametrize("field", ["stock_close", "index_close"])
def test_missing_interior_close_prevents_both_returns_and_sd_without_erasing_other_blocks(
    field,
):
    facts, history, boundary = declared_case()
    history[7] = missing(history[7], field)
    result = calculate((facts, history, boundary))
    prefix = "stock" if field == "stock_close" else "market"
    other = "market" if prefix == "stock" else "stock"
    for suffix in ("return_20", "daily_volatility_20"):
        assert value(result, f"{prefix}_{suffix}") is None
        assert reasons(result, f"{prefix}_{suffix}") == [
            f"INCOMPLETE_{field.upper()}_21"
        ]
        assert value(result, f"{other}_{suffix}") is not None
    assert value(result, "announced_share_fraction") == pytest.approx(0.02)
    assert value(result, "announced_value_to_median_turnover_20") == pytest.approx(20)
    assert result["window"]["declared_session_count"] == 21


def test_missing_v0_turnover_is_irrelevant_but_one_missing_final_value_is_not_averaged():
    facts, history, boundary = declared_case()
    history[0] = missing(history[0], "stock_traded_value_krw")
    assert value(
        calculate((facts, history, boundary)), "announced_value_to_median_turnover_20"
    ) == pytest.approx(20)
    history[8] = missing(history[8], "stock_traded_value_krw")
    result = calculate((facts, history, boundary))
    assert value(result, "announced_value_to_median_turnover_20") is None
    assert reasons(result, "announced_value_to_median_turnover_20") == [
        "INCOMPLETE_STOCK_TRADED_VALUE_KRW_20"
    ]
    assert value(result, "stock_return_20") == pytest.approx(0.5)


@pytest.mark.parametrize(
    "missing_field,missing_feature,other_feature",
    [
        (
            "announced_ordinary_shares",
            "announced_share_fraction",
            "announced_value_to_median_turnover_20",
        ),
        (
            "announced_ordinary_value_krw",
            "announced_value_to_median_turnover_20",
            "announced_share_fraction",
        ),
    ],
)
def test_independently_missing_announced_scalars_share_required_terms_timestamp(
    missing_field, missing_feature, other_feature
):
    facts, history, boundary = declared_case()
    facts = replace(facts, **{missing_field: None})
    result = calculate((facts, history, boundary))
    assert value(result, missing_feature) is None
    assert reasons(result, missing_feature) == [f"MISSING_{missing_field.upper()}"]
    assert value(result, other_feature) is not None
    assert result["terms_available_at"] == facts.terms_available_at.isoformat()


def test_all_scalar_facts_can_be_missing_with_explicit_reasons_and_context_retained():
    facts, history, boundary = declared_case()
    facts = replace(
        facts,
        announced_ordinary_shares=None,
        announced_ordinary_value_krw=None,
        listed_ordinary_shares=None,
        listed_shares_available_at=None,
    )
    result = calculate((facts, history, boundary))
    assert reasons(result, "announced_share_fraction") == [
        "MISSING_ANNOUNCED_ORDINARY_SHARES",
        "MISSING_LISTED_ORDINARY_SHARES",
    ]
    assert reasons(result, "announced_value_to_median_turnover_20") == [
        "MISSING_ANNOUNCED_ORDINARY_VALUE_KRW"
    ]
    assert value(result, "stock_return_20") == pytest.approx(0.5)
    with pytest.raises(ValueError):
        replace(facts, terms_available_at=None)


def test_missing_listed_count_is_independent_and_reference_is_still_required():
    facts, history, boundary = declared_case()
    facts = replace(facts, listed_ordinary_shares=None, listed_shares_available_at=None)
    result = calculate((facts, history, boundary))
    assert reasons(result, "announced_share_fraction") == [
        "MISSING_LISTED_ORDINARY_SHARES"
    ]
    assert value(result, "announced_value_to_median_turnover_20") == pytest.approx(20)
    with pytest.raises(ValueError, match="listed-shares reference"):
        calculate(
            (
                replace(
                    facts,
                    listed_shares_reference_at=facts.listed_shares_reference_at
                    - EPSILON,
                ),
                history,
                boundary,
            )
        )


def test_zero_numerators_produce_zero_only_with_positive_denominators():
    facts, history, boundary = declared_case()
    facts = replace(facts, announced_ordinary_shares=0, announced_ordinary_value_krw=0)
    result = calculate((facts, history, boundary))
    for name in ("announced_share_fraction", "announced_value_to_median_turnover_20"):
        assert value(result, name) == 0
        assert reasons(result, name) == []
    facts = replace(facts, listed_ordinary_shares=0)
    history = [replace(row, stock_traded_value_krw=0) for row in history]
    result = calculate((facts, history, boundary))
    assert value(result, "announced_share_fraction") is None
    assert reasons(result, "announced_share_fraction") == [
        "ZERO_LISTED_ORDINARY_SHARES"
    ]
    assert value(result, "announced_value_to_median_turnover_20") is None
    assert reasons(result, "announced_value_to_median_turnover_20") == [
        "ZERO_MEDIAN_STOCK_TRADED_VALUE_KRW_20"
    ]


def test_zero_turnover_observations_can_have_a_positive_central_pair():
    facts, history, boundary = declared_case()
    history = [
        replace(row, stock_traded_value_krw=0 if 1 <= i <= 10 else 10)
        for i, row in enumerate(history)
    ]
    assert (
        value(
            calculate((facts, history, boundary)),
            "announced_value_to_median_turnover_20",
        )
        == 78
    )


@pytest.mark.parametrize("length", [0, 20, 22])
def test_window_length_is_exact_and_no_gap_is_filled(length):
    facts, history, boundary = declared_case()
    history = history[:length] if length <= 21 else history + [history[0]]
    with pytest.raises(ValueError, match="exactly 21"):
        calculate((facts, history, boundary))


@pytest.mark.parametrize(
    "mismatch",
    [
        "security",
        "market",
        "duplicate_date",
        "wrong_date",
        "naive_close",
        "non_utc_close",
    ],
)
def test_identity_and_session_mismatches_reject_including_bypassed_nested_models(
    mismatch,
):
    facts, history, boundary = declared_case()
    if mismatch == "security":
        history[5] = replace(history[5], ordinary_security_id="ordinary-B")
    elif mismatch == "market":
        history[5] = replace(history[5], market="KOSDAQ")
    elif mismatch == "duplicate_date":
        history[5] = replace(history[5], session=history[4].session)
    else:
        original = history[5].session
        update = {
            "wrong_date": {"trading_date": original.trading_date - timedelta(days=1)},
            "naive_close": {"close_at": original.close_at.replace(tzinfo=None)},
            "non_utc_close": {"close_at": original.close_at.astimezone(KST)},
        }[mismatch]
        bad_session = original.model_copy(update=update)
        history[5] = history[5].model_copy(update={"session": bad_session})
    with pytest.raises(ValueError):
        calculate((facts, history, boundary))


def test_boundary_is_typed_revalidated_and_must_follow_reference():
    facts, history, boundary = declared_case()
    with pytest.raises(ValueError, match="next session"):
        calculate((facts, history, history[-1].session))
    with pytest.raises(ValueError):
        calculate(
            (
                facts,
                history,
                boundary.model_copy(
                    update={"close_at": boundary.close_at.replace(tzinfo=None)}
                ),
            )
        )
    with pytest.raises(TypeError):
        calculate((facts, history, boundary.model_dump()))


@pytest.mark.parametrize("target", ["facts", "observation"])
def test_model_copy_or_construct_cannot_bypass_numeric_validation(target):
    facts, history, boundary = declared_case()
    if target == "facts":
        facts = facts.model_copy(update={"announced_ordinary_shares": True})
    else:
        history[2] = BuybackHistoryObservation.model_construct(
            **(history[2].model_dump() | {"stock_close": -1})
        )
    with pytest.raises(ValueError):
        calculate((facts, history, boundary))


@pytest.mark.parametrize("target", ["facts", "observation", "sequence"])
def test_untyped_inputs_are_rejected(target):
    facts, history, boundary = declared_case()
    if target == "facts":
        facts = facts.model_dump()
    elif target == "observation":
        history[0] = history[0].model_dump()
    else:
        history = iter(history)
    with pytest.raises(TypeError):
        calculate((facts, history, boundary))


@pytest.mark.parametrize("identity", ["", " ordinary-A", "ordinary-A "])
def test_untrimmed_or_blank_identity_is_not_silently_normalized(identity):
    facts, _, _ = declared_case()
    with pytest.raises(ValueError):
        replace(facts, ordinary_security_id=identity)


@pytest.mark.parametrize("unsupported", ["KONEX", "kospi", True])
def test_unsupported_market_rejects(unsupported):
    facts, _, _ = declared_case()
    with pytest.raises(ValueError):
        replace(facts, market=unsupported)


def test_no_hash_verified_flag_or_retrieval_time_can_establish_admission():
    facts, history, boundary = declared_case()
    # Old historical vintages remain valid without any assertion of old retrieval.
    assert calculate((facts, history, boundary))["scope"].endswith(
        "NO_SOURCE_ADMISSION"
    )
    for extra in (
        {"verified": True},
        {"source_sha256": "a" * 64},
        {"retrieved_at": datetime(2026, 10, 25, tzinfo=UTC)},
    ):
        with pytest.raises(ValueError):
            replace(facts, **extra)


def test_derived_ratio_overflow_rejects_for_share_and_krw_ratios():
    facts, history, boundary = declared_case()
    with pytest.raises(ValueError, match="finite"):
        calculate(
            (
                replace(
                    facts, announced_ordinary_shares=10**1000, listed_ordinary_shares=1
                ),
                history,
                boundary,
            )
        )
    history = [replace(row, stock_traded_value_krw=1e-300) for row in history]
    with pytest.raises(ValueError, match="finite"):
        calculate(
            (replace(facts, announced_ordinary_value_krw=1e300), history, boundary)
        )


def test_daily_return_overflow_rejects_even_with_finite_endpoint_return():
    facts, history, boundary = declared_case()
    history = [
        replace(row, stock_close=1e300 if i == 1 else 1e-300)
        for i, row in enumerate(history)
    ]
    with pytest.raises(ValueError, match="finite"):
        calculate((facts, history, boundary))


def test_finite_extreme_prices_do_not_overflow_squared_deviations():
    facts, history, boundary = declared_case()
    history = [
        replace(row, stock_close=1 if i % 2 == 0 else 1e200)
        for i, row in enumerate(history)
    ]
    result = calculate((facts, history, boundary))
    assert value(result, "stock_return_20") == 0
    # Ten approximately 1e200 returns, ten -1; analytic SD is half-range*sqrt(20/19).
    assert value(result, "stock_daily_volatility_20") == pytest.approx(
        5e199 * math.sqrt(20 / 19)
    )
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("turnover", [1e308, 5e-324])
def test_median_avoids_sum_overflow_and_smallest_positive_halving_loss(turnover):
    facts, history, boundary = declared_case()
    facts = replace(facts, announced_ordinary_value_krw=turnover)
    history = [replace(row, stock_traded_value_krw=turnover) for row in history]
    result = calculate((facts, history, boundary))
    assert value(result, "announced_value_to_median_turnover_20") == 1
    assert reasons(result, "announced_value_to_median_turnover_20") == []


def test_tiny_unequal_central_pair_uses_correctly_rounded_mean():
    facts, history, boundary = declared_case()
    facts = replace(facts, announced_ordinary_value_krw=1e-323)
    history = [
        replace(row, stock_traded_value_krw=5e-324 if i <= 10 else 1e-323)
        for i, row in enumerate(history)
    ]
    # V1..V20 has ten of each; the exact midpoint rounds to 1e-323, not 5e-324.
    assert (
        value(
            calculate((facts, history, boundary)),
            "announced_value_to_median_turnover_20",
        )
        == 1
    )


def test_positive_median_underflow_raises_instead_of_claiming_zero_turnover():
    facts, history, boundary = declared_case()
    history = [
        replace(row, stock_traded_value_krw=0 if i <= 10 else 5e-324)
        for i, row in enumerate(history)
    ]
    with pytest.raises(ValueError, match="positive median.*not representable"):
        calculate((facts, history, boundary))


def test_excluded_v0_vintage_is_still_checked_before_any_calculation():
    facts, history, boundary = declared_case()
    history[0] = replace(
        history[0], stock_traded_value_krw_available_at=facts.decision_at
    )
    with pytest.raises(ValueError, match="vintage"):
        calculate((facts, history, boundary))


def test_share_krw_and_price_scaling_invariance():
    facts, history, boundary = declared_case()
    baseline = calculate((facts, history, boundary))
    scaled_facts = replace(
        facts,
        announced_ordinary_shares=140,
        listed_ordinary_shares=7000,
        announced_ordinary_value_krw=390 * 11,
    )
    scaled_history = [
        replace(
            row,
            stock_close=row.stock_close * 13,
            index_close=row.index_close * 17,
            stock_traded_value_krw=row.stock_traded_value_krw * 11,
        )
        for row in history
    ]
    scaled = calculate((scaled_facts, scaled_history, boundary))
    for name in baseline["features"]:
        assert value(scaled, name) == pytest.approx(value(baseline, name))


def test_permutation_freezing_and_caller_report_mutation_are_isolated():
    facts, history, boundary = declared_case()
    original = copy.deepcopy(history)
    baseline = calculate((facts, history, boundary))
    assert calculate((facts, list(reversed(history)), boundary)) == baseline
    assert history == original
    with pytest.raises(ValidationError):
        facts.announced_ordinary_shares = 7
    with pytest.raises(ValidationError):
        history[0].stock_close = 7
    history.clear()
    assert baseline["window"]["declared_session_count"] == 21
    # Reports own their lists and dicts, including independent missing-price reasons.
    missing_history = [missing(row, "stock_close") for row in original]
    result = calculate((facts, missing_history, boundary))
    result["features"]["stock_return_20"]["reasons"].append("CALLER_EDIT")
    assert reasons(result, "stock_daily_volatility_20") == ["INCOMPLETE_STOCK_CLOSE_21"]
    assert reasons(
        calculate((facts, missing_history, boundary)), "stock_return_20"
    ) == ["INCOMPLETE_STOCK_CLOSE_21"]


def test_large_buyback_can_coexist_with_weak_volatile_context_without_trade_classification():
    facts, history, boundary = declared_case()
    facts = replace(
        facts, announced_ordinary_shares=10000, announced_ordinary_value_krw=39000
    )
    history = [
        replace(row, stock_close=50 if i == 20 else 100 if i % 2 == 0 else 200)
        for i, row in enumerate(history)
    ]
    result = calculate((facts, history, boundary))
    assert value(result, "announced_share_fraction") == 10
    assert value(result, "announced_value_to_median_turnover_20") == 2000
    assert value(result, "stock_return_20") == -0.5
    assert value(result, "stock_daily_volatility_20") > 0.7
    assert value(result, "market_return_20") < 0
    assert set(result["features"]) == {
        "announced_share_fraction",
        "announced_value_to_median_turnover_20",
        "stock_return_20",
        "market_return_20",
        "stock_daily_volatility_20",
        "market_daily_volatility_20",
    }
    assert (
        not {"eligible", "trade", "buy", "skip", "performance", "label", "prediction"}
        & result.keys()
    )
