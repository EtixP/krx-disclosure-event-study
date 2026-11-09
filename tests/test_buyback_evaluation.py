"""Hand-authored accounting adversaries, with no admitted data or model fits."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from kdtb.backtest.cost_model import CostModel
from kdtb.learning.buyback_evaluation import (
    OUTCOME_STATES,
    BuybackOutcome,
    EligibleDecision,
    evaluate_buyback_policies,
)

KST = ZoneInfo("Asia/Seoul")
DAY = date(2025, 1, 6)


def decision(key="a", *, day=DAY, **updates):
    values = {
        "decision_id": key,
        "issuer_id": f"issuer-{key}",
        "decision_at": datetime.combine(day, time(8, 55), KST),
        "entry_date": day,
        "exit_date": day + timedelta(days=4),
        "market": "KOSPI",
    }
    return EligibleDecision(**(values | updates))


def outcome(item, state="COMPLETED", **updates):
    values = {
        "decision_id": item.decision_id,
        "entry_date": item.entry_date,
        "exit_date": item.exit_date,
        "state": state,
    }
    if state in {"COMPLETED", "OUTCOME_UNRESOLVED"}:
        values |= {
            "stock_entry_close": 100.0,
            "stock_exit_close": 101.0 if state == "COMPLETED" else None,
            "benchmark_entry_close": 100.0,
            "benchmark_exit_close": 100.0,
        }
    return BuybackOutcome(**(values | updates))


def reward(report, policy="AlwaysTrade", basis="raw"):
    return report["policies"][policy]["rewards"][basis]


def lift(report, policy, reference, basis="raw"):
    return report["paired_comparisons"][policy][reference]["rewards"][basis]


def cost(item, model=None):
    return (model or CostModel()).roundtrip_cost(
        1, buy_date=item.entry_date, sell_date=item.exit_date, market=item.market
    )


def test_abnormal_gain_can_coexist_with_cash_loss_on_identical_dates():
    item = decision()
    result = evaluate_buyback_policies(
        [item],
        [outcome(item, stock_exit_close=95, benchmark_exit_close=90)],
        {"Supplied": {"a": True}},
    )
    assert reward(result)["point_total"] == pytest.approx(-0.05 - cost(item))
    assert reward(result, basis="abnormal")["point_total"] == pytest.approx(
        0.05 - cost(item)
    )
    assert reward(result)["point_total"] < 0
    assert reward(result, basis="abnormal")["point_total"] > 0
    assert lift(result, "Supplied", "AlwaysTrade")["point_total"] == 0
    assert lift(result, "Supplied", "NeverTrade")["point_total"] == pytest.approx(
        reward(result)["point_total"]
    )
    assert result["reward_unit"] == "return_fraction_on_unit_same_notional"
    assert result["scope"] == "OFFLINE_ACCOUNTING_ONLY_NO_SOURCE_OR_PERFORMANCE_CLAIM"
    json.dumps(result, allow_nan=False)


def test_nine_wins_in_ten_can_have_negative_mean_in_both_bases():
    items = [decision(str(i)) for i in range(10)]
    observed = [
        outcome(item, stock_exit_close=101 if i < 9 else 50)
        for i, item in enumerate(items)
    ]
    result = evaluate_buyback_policies(items, observed, {})
    for basis in ("raw", "abnormal"):
        assert (
            sum(
                row["potential_rewards"][basis]["point"] > 0 for row in result["cohort"]
            )
            == 9
        )
        assert reward(result, basis=basis)["point_mean_per_eligible"] < 0
        assert reward(result, basis=basis)["point_mean_per_entered"] == pytest.approx(
            (9 * 0.01 - 0.5) / 10 - cost(items[0])
        )


def test_regime_only_rewards_do_not_invent_matched_demand_selection_lift():
    items, observed = [], []
    context, demand, bull_only = {}, {}, {}
    for regime, day, stock_exit in (
        ("calm", DAY, 110),
        ("stress", DAY + timedelta(days=14), 88),
    ):
        for issuer in range(4):
            item = decision(f"{regime}-{issuer}", day=day)
            items.append(item)
            observed.append(outcome(item, stock_exit_close=stock_exit))
            context[item.decision_id] = issuer < 2
            demand[item.decision_id] = issuer >= 2
            bull_only[item.decision_id] = regime == "calm"
    result = evaluate_buyback_policies(
        items,
        observed,
        {"ContextOnly": context, "DemandPlusContext": demand, "BullOnly": bull_only},
    )
    for day in {item.entry_date for item in items}:
        day_ids = [item.decision_id for item in items if item.entry_date == day]
        assert (
            sum(context[key] for key in day_ids)
            == sum(demand[key] for key in day_ids)
            == 2
        )
    for basis in ("raw", "abnormal"):
        assert lift(result, "DemandPlusContext", "ContextOnly", basis)[
            "point_total"
        ] == pytest.approx(0)
        assert lift(result, "DemandPlusContext", "ContextOnly", basis)[
            "point_mean_per_eligible"
        ] == pytest.approx(0)
    # Different regime exposure changes totals; accounting assigns no edge label.
    assert reward(result, "BullOnly")["point_total"] > 0
    assert reward(result, "ContextOnly")["point_total"] < 0


def test_planted_conditional_demand_case_has_algebraic_same_cohort_lift():
    items, observed = [], []
    context, demand = {}, {}
    for regime, day, exits, benchmark_exit in (
        ("calm", DAY, (105, 108), 102),
        ("stress", DAY + timedelta(days=14), (85, 97), 90),
    ):
        for high_demand, stock_exit in enumerate(exits):
            item = decision(f"{regime}-{high_demand}", day=day)
            items.append(item)
            observed.append(
                outcome(
                    item,
                    stock_exit_close=stock_exit,
                    benchmark_exit_close=benchmark_exit,
                )
            )
            context[item.decision_id] = not bool(high_demand)
            demand[item.decision_id] = bool(high_demand)
    result = evaluate_buyback_policies(
        items, observed, {"ContextOnly": context, "DemandPlusContext": demand}
    )
    for basis in ("raw", "abnormal"):
        paired = lift(result, "DemandPlusContext", "ContextOnly", basis)
        assert paired["point_total"] == pytest.approx(0.03 + 0.12)
        assert paired["point_mean_per_eligible"] == pytest.approx((0.03 + 0.12) / 4)
        assert result["policies"]["DemandPlusContext"]["eligible_count"] == 4
    stress = next(
        row for row in result["cohort"] if row["decision"]["decision_id"] == "stress-1"
    )
    assert stress["potential_rewards"]["raw"]["point"] < 0
    assert stress["potential_rewards"]["abnormal"]["point"] > 0


def test_six_states_keep_denominator_zeros_unknowns_and_counts_explicit():
    items = [decision(state) for state in OUTCOME_STATES]
    observed = [outcome(item, item.decision_id) for item in items]
    mask = {item.decision_id: True for item in items}
    result = evaluate_buyback_policies(
        items, observed, {"AllCopy": mask, "NoneCopy": dict.fromkeys(mask, False)}
    )
    summary = result["policies"]["AlwaysTrade"]
    assert result["eligible_count"] == 6
    assert result["state_counts"] == dict.fromkeys(OUTCOME_STATES, 1)
    assert summary["selected_state_counts"] == result["state_counts"]
    assert summary["selected_instruction_count"] == 6
    assert summary["known_entered_position_count"] == 2
    assert summary["unknown_execution_count"] == 2
    assert summary["entered_position_count"] is None
    for basis in ("raw", "abnormal"):
        metrics = reward(result, basis=basis)
        assert metrics["unknown_reward_count"] == 3
        assert metrics["known_reward_count"] == 3
        assert metrics["known_reward_sum"] == pytest.approx(0.01 - cost(items[0]))
        assert metrics["point_total"] is None
        assert metrics["lower_bound_total"] is None
        assert metrics["point_mean_per_entered"] is None
        assert metrics["lower_bound_mean_per_entered"] is None
        assert lift(result, "AllCopy", "AlwaysTrade", basis)["point_total"] == 0
        assert (
            lift(result, "AllCopy", "AlwaysTrade", basis)["unknown_difference_count"]
            == 0
        )
        assert lift(result, "AllCopy", "NeverTrade", basis)["point_total"] is None
        assert lift(result, "NoneCopy", "NeverTrade", basis)["point_total"] == 0
        never = reward(result, "NeverTrade", basis)
        assert never["point_total"] == never["lower_bound_total"] == 0
        assert never["point_mean_per_eligible"] == 0
        assert never["point_mean_per_entered"] is None
    assert all(policy["eligible_count"] == 6 for policy in result["policies"].values())


@pytest.mark.parametrize("state", ["ENTRY_UNVERIFIED", "CANCEL_STATUS_UNVERIFIED"])
def test_completed_plus_unknown_execution_has_no_entered_total_or_trade_means(state):
    complete, unknown = decision("complete"), decision("unknown")
    result = evaluate_buyback_policies(
        [complete, unknown],
        [outcome(complete), outcome(unknown, state)],
        {"CompleteOnly": {"complete": True, "unknown": False}},
    )
    summary = result["policies"]["AlwaysTrade"]
    assert summary["known_entered_position_count"] == 1
    assert summary["entered_position_count"] is None
    for basis in ("raw", "abnormal"):
        metrics = reward(result, basis=basis)
        assert metrics["point_mean_per_entered"] is None
        assert metrics["lower_bound_mean_per_entered"] is None
        assert reward(result, "CompleteOnly", basis)[
            "point_mean_per_entered"
        ] == pytest.approx(0.01 - cost(complete))
        assert lift(result, "CompleteOnly", "AlwaysTrade", basis)["point_total"] is None


def test_unresolved_position_bounds_use_all_entered_and_never_become_lift_bounds():
    complete, unresolved, canceled = (
        decision(key) for key in ("complete", "unresolved", "canceled")
    )
    result = evaluate_buyback_policies(
        [complete, unresolved, canceled],
        [
            outcome(complete, stock_exit_close=110),
            outcome(unresolved, "OUTCOME_UNRESOLVED", benchmark_exit_close=120),
            outcome(canceled, "CANCELED_BEFORE_ENTRY"),
        ],
        {"ResolvedOnly": {"complete": True, "unresolved": False, "canceled": True}},
    )
    c = cost(complete)
    assert result["policies"]["AlwaysTrade"]["entered_position_count"] == 2
    for basis, bound in (
        ("raw", 0.10 - c - 1 - c),
        ("abnormal", 0.10 - c - 1 - 0.20 - c),
    ):
        metrics = reward(result, basis=basis)
        assert metrics["point_total"] is None
        assert metrics["point_mean_per_entered"] is None
        assert metrics["known_reward_sum"] == pytest.approx(0.10 - c)
        assert metrics["lower_bound_total"] == pytest.approx(bound)
        assert metrics["lower_bound_mean_per_eligible"] == pytest.approx(bound / 3)
        assert metrics["lower_bound_mean_per_entered"] == pytest.approx(bound / 2)
        paired = lift(result, "ResolvedOnly", "AlwaysTrade", basis)
        assert paired["point_total"] is None
        assert not any("bound" in name for name in paired)


@pytest.mark.parametrize("state", ["COMPLETED", "OUTCOME_UNRESOLVED"])
@pytest.mark.parametrize(
    "missing", ["benchmark_entry_close", "benchmark_exit_close", "both"]
)
def test_missing_benchmark_preserves_raw_and_blocks_abnormal_without_dropping_row(
    state, missing
):
    item = decision()
    updates = (
        {"benchmark_entry_close": None, "benchmark_exit_close": None}
        if missing == "both"
        else {missing: None}
    )
    result = evaluate_buyback_policies([item], [outcome(item, state, **updates)], {})
    assert result["eligible_count"] == 1
    assert result["policies"]["AlwaysTrade"]["entered_position_count"] == 1
    raw, abnormal = reward(result), reward(result, basis="abnormal")
    assert raw["lower_bound_total"] is not None
    assert (raw["point_total"] is not None) == (state == "COMPLETED")
    assert abnormal["point_total"] is abnormal["lower_bound_total"] is None
    assert abnormal["unknown_reward_count"] == 1
    assert reward(result, "NeverTrade", "abnormal")["point_total"] == 0
    assert lift(result, "AlwaysTrade", "NeverTrade", "abnormal")["point_total"] is None


@pytest.mark.parametrize("state", ["CANCELED_BEFORE_ENTRY", "NO_FILL"])
def test_confirmed_nonexecution_is_zero_without_prices_or_entered_mean(state):
    item = decision()
    result = evaluate_buyback_policies([item], [outcome(item, state)], {})
    assert result["policies"]["AlwaysTrade"]["entered_position_count"] == 0
    for basis in ("raw", "abnormal"):
        metrics = reward(result, basis=basis)
        assert metrics["point_total"] == metrics["lower_bound_total"] == 0
        assert metrics["point_mean_per_eligible"] == 0
        assert (
            metrics["point_mean_per_entered"]
            is metrics["lower_bound_mean_per_entered"]
            is None
        )


def test_empty_cohort_has_zero_totals_and_unavailable_fractions_and_means():
    result = evaluate_buyback_policies([], [], {"EmptyMask": {}})
    assert result["status"] == "empty"
    assert result["decision_ids"] == result["cohort"] == []
    assert result["eligible_count"] == result["issuer_count"] == 0
    for name, policy in result["policies"].items():
        assert policy["selection_fraction"] is None
        assert policy["entered_position_count"] == 0
        for basis in ("raw", "abnormal"):
            metrics = reward(result, name, basis)
            assert metrics["point_total"] == metrics["lower_bound_total"] == 0
            assert metrics["point_mean_per_eligible"] is None
            assert metrics["lower_bound_mean_per_eligible"] is None
            assert metrics["point_mean_per_entered"] is None
    assert lift(result, "EmptyMask", "AlwaysTrade")["point_total"] == 0
    assert lift(result, "EmptyMask", "AlwaysTrade")["point_mean_per_eligible"] is None


def test_cost_model_is_reused_with_date_market_and_unit_notional(monkeypatch):
    model = CostModel(
        commission_per_side=0.001, vat_on_commission=0.2, slippage_bps_per_side=3
    )
    items = [
        decision("a", day=date(2025, 12, 30), market="KOSDAQ"),
        decision("b", day=date(2024, 6, 3)),
    ]
    expected = [cost(item, model) for item in items]
    original = CostModel.roundtrip_cost
    calls = []

    def observed(self, notional, **kwargs):
        calls.append((notional, kwargs))
        return original(self, notional, **kwargs)

    monkeypatch.setattr(CostModel, "roundtrip_cost", observed)
    result = evaluate_buyback_policies(
        items, [outcome(items[0]), outcome(items[1], "NO_FILL")], {}, cost_model=model
    )
    assert calls == [
        (
            1,
            {
                "buy_date": item.entry_date,
                "sell_date": item.exit_date,
                "market": item.market,
            },
        )
        for item in items
    ]
    assert [row["cost_fraction_if_entered"] for row in result["cohort"]] == expected
    assert reward(result)["point_total"] == pytest.approx(0.01 - expected[0])
    assert result["cost_assumptions"] == {
        "model": "CostModel",
        "notional": 1,
        **model.model_dump(),
    }


@pytest.mark.parametrize("state", OUTCOME_STATES)
@pytest.mark.parametrize("day", [date(2020, 12, 30), date(2027, 1, 4)])
def test_unsupported_cost_dates_fail_for_every_state_and_abstaining_mask(state, day):
    item = decision(day=day)
    with pytest.raises(ValueError, match="outside"):
        evaluate_buyback_policies(
            [item], [outcome(item, state)], {"Abstain": {"a": False}}
        )


@pytest.mark.parametrize(
    "values",
    [
        [],
        [True],
        {"a": 1},
        {"a": 0},
        {"a": "true"},
        {"a": None},
        {"a": True, "extra": False},
        {},
        {1: True},
    ],
)
def test_mask_requires_exact_ids_and_uncoerced_boolean_values(values):
    item = decision()
    with pytest.raises((ValueError, TypeError)):
        evaluate_buyback_policies([item], [outcome(item)], {"Supplied": values})


@pytest.mark.parametrize("name", ["AlwaysTrade", "NeverTrade", "", " ", " name", 2])
def test_policy_names_are_strict_and_baselines_cannot_be_replaced(name):
    item = decision()
    with pytest.raises((ValueError, TypeError)):
        evaluate_buyback_policies([item], [outcome(item)], {name: {"a": True}})


def test_missing_extra_and_duplicate_cohort_outcome_ids_fail():
    a, b = decision("a"), decision("b")
    for decisions, outcomes in (
        ([a, a], [outcome(a)]),
        ([a], [outcome(a), outcome(a)]),
        ([a, b], [outcome(a)]),
        ([a], [outcome(a), outcome(b)]),
    ):
        with pytest.raises(ValueError, match="duplicate|exactly"):
            evaluate_buyback_policies(decisions, outcomes, {})
    # Repeated issuers remain valid; only decision identities must be unique.
    result = evaluate_buyback_policies(
        [a, b.model_copy(update={"issuer_id": a.issuer_id})],
        [outcome(a), outcome(b)],
        {},
    )
    assert result["issuer_count"] == 1


class DuplicateMapping(Mapping):
    def __getitem__(self, key):
        return True

    def __iter__(self):
        return iter(["a", "a"])

    def __len__(self):
        return 2

    def items(self):
        return [("a", True), ("a", False)]


def test_nonstandard_mapping_cannot_hide_duplicate_mask_ids():
    item = decision()
    with pytest.raises(ValueError, match="duplicate"):
        evaluate_buyback_policies(
            [item], [outcome(item)], {"Supplied": DuplicateMapping()}
        )


@pytest.mark.parametrize("field", ["decision_id", "issuer_id"])
@pytest.mark.parametrize("value", ["", " ", " padded", 7, None])
def test_decision_identifiers_are_strict_nonempty_strings(field, value):
    with pytest.raises(ValidationError):
        decision(**{field: value})


@pytest.mark.parametrize(
    "updates",
    [
        {"entry_date": "2025-01-06"},
        {"exit_date": "2025-01-10"},
        {"entry_date": datetime(2025, 1, 6)},
        {"entry_date": date(2025, 1, 10)},
        {"exit_date": date(2025, 1, 5)},
        {"decision_at": datetime(2025, 1, 6, 8, 55)},
        {"decision_at": "2025-01-06T08:55:00+09:00"},
        {"decision_at": datetime(2025, 1, 6, 16, tzinfo=timezone.utc)},
        {"market": "OTHER"},
        {"market": "kospi"},
        {"market": True},
        {"unexpected": "ignored?"},
    ],
)
def test_decision_dates_timestamps_markets_and_extra_fields_are_strict(updates):
    with pytest.raises(ValidationError):
        decision(**updates)


def test_korean_date_uses_timestamp_offset_and_does_not_infer_a_cutoff():
    item = decision(decision_at=datetime(2025, 1, 5, 23, 55, tzinfo=timezone.utc))
    assert item.decision_at.astimezone(KST).date() == DAY
    late = decision(decision_at=datetime(2025, 1, 6, 17, tzinfo=KST))
    result = evaluate_buyback_policies([late], [outcome(late)], {})
    assert result["eligible_count"] == 1  # declaration, not 08:55 availability proof


@pytest.mark.parametrize("field", ["entry_date", "exit_date"])
def test_outcome_requires_exact_declared_dates(field):
    item = decision()
    wrong = outcome(item, **{field: getattr(item, field) + timedelta(days=1)})
    with pytest.raises(ValueError, match="exactly match declared"):
        evaluate_buyback_policies([item], [wrong], {})
    with pytest.raises(ValidationError):
        outcome(item, **{field: str(getattr(item, field))})


@pytest.mark.parametrize("state", OUTCOME_STATES)
def test_contradictory_state_payloads_fail(state):
    item = decision()
    updates = (
        {"stock_entry_close": None}
        if state in {"COMPLETED", "OUTCOME_UNRESOLVED"}
        else {"benchmark_entry_close": 100}
    )
    with pytest.raises(ValidationError):
        outcome(item, state, **updates)
    if state in {"COMPLETED", "OUTCOME_UNRESOLVED"}:
        with pytest.raises(ValidationError):
            outcome(item, state, stock_exit_close=None if state == "COMPLETED" else 101)


def test_unknown_state_outcome_identity_extra_field_and_dates_fail():
    item = decision()
    for updates in (
        {"state": "MISSING"},
        {"decision_id": ""},
        {"decision_id": 3},
        {"extra": 1},
        {"exit_date": item.entry_date},
    ):
        with pytest.raises(ValidationError):
            outcome(item, **updates)


@pytest.mark.parametrize(
    "field",
    [
        "stock_entry_close",
        "stock_exit_close",
        "benchmark_entry_close",
        "benchmark_exit_close",
    ],
)
@pytest.mark.parametrize(
    "value",
    [True, False, "100", float("inf"), float("-inf"), float("nan"), 0, -1, 10**400],
)
def test_price_payloads_reject_booleans_coercion_nonfinite_and_nonpositive(
    field, value
):
    with pytest.raises(ValidationError):
        outcome(decision(), **{field: value})


def test_record_types_and_bypassed_model_validation_are_rechecked():
    item = decision()
    observed = outcome(item)
    for items, outcomes in (
        ([item.model_dump()], [observed]),
        ([item], [observed.model_dump()]),
        (iter([item]), [observed]),
    ):
        with pytest.raises(TypeError, match="typed|instances"):
            evaluate_buyback_policies(items, outcomes, {})
    with pytest.raises(ValidationError):
        evaluate_buyback_policies(
            [item.model_copy(update={"market": "OTHER"})], [observed], {}
        )
    with pytest.raises(ValidationError):
        evaluate_buyback_policies(
            [item], [observed.model_copy(update={"stock_entry_close": True})], {}
        )


@pytest.mark.parametrize(
    "updates",
    [
        {"stock_entry_close": 1e-308, "stock_exit_close": 1e308},
        {"benchmark_entry_close": 1e-308, "benchmark_exit_close": 1e308},
    ],
)
def test_finite_prices_with_nonfinite_derived_ratios_fail(updates):
    item = decision()
    with pytest.raises(ValueError, match="finite"):
        evaluate_buyback_policies([item], [outcome(item, **updates)], {})


def test_nonfinite_derived_abnormal_reward_and_bound_fail():
    item = decision()
    model = CostModel(commission_per_side=4e307)
    for state in ("COMPLETED", "OUTCOME_UNRESOLVED"):
        with pytest.raises(ValueError, match="finite"):
            evaluate_buyback_policies(
                [item],
                [
                    outcome(
                        item,
                        state,
                        benchmark_entry_close=1,
                        benchmark_exit_close=1.5e308,
                    )
                ],
                {},
                cost_model=model,
            )


def test_finite_row_rewards_cannot_overflow_aggregate():
    items = [decision("a"), decision("b")]
    with pytest.raises(ValueError, match="finite"):
        evaluate_buyback_policies(
            items,
            [
                outcome(item, stock_entry_close=1, stock_exit_close=1e308)
                for item in items
            ],
            {},
        )


def test_finite_policy_totals_cannot_overflow_paired_lift():
    a, b = decision("a"), decision("b")
    with pytest.raises(ValueError, match="finite"):
        evaluate_buyback_policies(
            [a, b],
            [
                outcome(a, stock_entry_close=1, stock_exit_close=1e308),
                outcome(
                    b,
                    stock_exit_close=100,
                    benchmark_entry_close=1,
                    benchmark_exit_close=1e308,
                ),
            ],
            {
                "PositiveOnly": {"a": True, "b": False},
                "NegativeOnly": {"a": False, "b": True},
            },
        )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, True, "0.1"])
def test_cost_assumptions_must_be_finite_nonnegative_numeric_even_if_bypassed(value):
    item = decision()
    with pytest.raises(ValueError, match="cost assumptions"):
        evaluate_buyback_policies(
            [item],
            [outcome(item)],
            {},
            cost_model=CostModel().model_copy(update={"commission_per_side": value}),
        )


def test_nonfinite_computed_cost_fails_even_for_no_fill():
    item = decision()
    with pytest.raises(ValueError, match="finite"):
        evaluate_buyback_policies(
            [item],
            [outcome(item, "NO_FILL")],
            {},
            cost_model=CostModel(commission_per_side=1e308),
        )
    with pytest.raises(TypeError, match="CostModel"):
        evaluate_buyback_policies([item], [outcome(item)], {}, cost_model={})


def test_permutations_are_identical_and_returned_report_has_no_caller_references():
    a, b = decision("a"), decision("b")
    decisions, outcomes = [b, a], [outcome(a), outcome(b, "OUTCOME_UNRESOLVED")]
    masks = {"Zed": {"b": False, "a": True}, "Alpha": {"a": False, "b": True}}
    original = copy.deepcopy((decisions, outcomes, masks))
    result = evaluate_buyback_policies(decisions, outcomes, masks)
    assert (decisions, outcomes, masks) == original
    permuted = evaluate_buyback_policies(
        list(reversed(decisions)),
        list(reversed(outcomes)),
        {key: dict(reversed(list(masks[key].items()))) for key in reversed(masks)},
    )
    assert json.dumps(result, allow_nan=False) == json.dumps(permuted, allow_nan=False)
    saved = copy.deepcopy(result)
    decisions.clear()
    outcomes.clear()
    masks["Alpha"]["a"] = True
    masks.clear()
    assert result == saved
    assert list(result["policies"]) == sorted(result["policies"])
    assert result["decision_ids"] == ["a", "b"]
