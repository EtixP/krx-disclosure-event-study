"""Pure accounting for supplied buyback cohorts, masks and potential outcomes.

This module proves no source admission, decision-time availability, frozen-mask
provenance, fill, predictive edge or production readiness. Dates are declared
research windows, not an inferred session calendar. Inputs must be typed records
and Python boolean selection maps; no loader or learner is involved.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from typing import Literal, get_args
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from kdtb.backtest.cost_model import CostModel, SUPPORTED_MARKETS

OutcomeState = Literal[
    "COMPLETED",
    "CANCELED_BEFORE_ENTRY",
    "NO_FILL",
    "ENTRY_UNVERIFIED",
    "CANCEL_STATUS_UNVERIFIED",
    "OUTCOME_UNRESOLVED",
]
OUTCOME_STATES = tuple(sorted(get_args(OutcomeState)))
_UNKNOWN_EXECUTION = {"ENTRY_UNVERIFIED", "CANCEL_STATUS_UNVERIFIED"}
_ENTERED = {"COMPLETED", "OUTCOME_UNRESOLVED"}
_KST = ZoneInfo("Asia/Seoul")


def _identifier(value: str) -> str:
    if not value or value != value.strip():
        raise ValueError("identifiers and policy names must be nonempty and trimmed")
    return value


def _finite(value: float) -> float:
    if not math.isfinite(value):
        raise ValueError("derived ratios, rewards, costs and aggregates must be finite")
    return value


def _sum(values: Sequence[float]) -> float:
    try:
        return _finite(math.fsum(values))
    except OverflowError as error:
        raise ValueError("reward aggregate must be finite") from error


class EligibleDecision(BaseModel):
    """Caller-declared eligibility; evidence and calendar admission are external."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    decision_id: str
    issuer_id: str
    decision_at: datetime
    market: str
    entry_date: date
    exit_date: date

    @field_validator("decision_id", "issuer_id")
    @classmethod
    def identifiers(cls, value: str) -> str:
        return _identifier(value)

    @field_validator("decision_at")
    @classmethod
    def aware_timestamp(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("decision_at must be timezone-aware")
        return value.astimezone(timezone.utc)

    @field_validator("market")
    @classmethod
    def supported_market(cls, value: str) -> str:
        if value not in SUPPORTED_MARKETS:
            raise ValueError("unsupported market")
        return value

    @model_validator(mode="after")
    def declared_chronology(self) -> EligibleDecision:
        if self.decision_at.astimezone(_KST).date() > self.entry_date:
            raise ValueError("Korean decision date must be no later than entry_date")
        if self.entry_date >= self.exit_date:
            raise ValueError("entry_date must be strictly before exit_date")
        return self


class BuybackOutcome(BaseModel):
    """Separate declared potential outcome, including explicit unknown states."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    decision_id: str
    entry_date: date
    exit_date: date
    state: OutcomeState
    stock_entry_close: float | None = None
    stock_exit_close: float | None = None
    benchmark_entry_close: float | None = None
    benchmark_exit_close: float | None = None

    @field_validator("decision_id")
    @classmethod
    def identifier(cls, value: str) -> str:
        return _identifier(value)

    @field_validator(
        "stock_entry_close",
        "stock_exit_close",
        "benchmark_entry_close",
        "benchmark_exit_close",
        mode="before",
    )
    @classmethod
    def positive_close(cls, value):
        if value is None:
            return None
        if type(value) not in (int, float):
            raise ValueError("closes must be numeric Python int/float, never boolean")
        try:
            close = float(value)
        except OverflowError as error:
            raise ValueError("closes must be finite and positive") from error
        if not math.isfinite(close) or close <= 0:
            raise ValueError("closes must be finite and positive")
        return close

    @model_validator(mode="after")
    def payload_matches_state(self) -> BuybackOutcome:
        if self.entry_date >= self.exit_date:
            raise ValueError("outcome entry_date must be strictly before exit_date")
        if self.state in _ENTERED:
            if self.stock_entry_close is None:
                raise ValueError("entered state requires stock_entry_close")
            if (self.state == "COMPLETED") != (self.stock_exit_close is not None):
                raise ValueError("stock_exit_close contradicts outcome state")
        elif any(
            value is not None
            for value in (
                self.stock_entry_close,
                self.stock_exit_close,
                self.benchmark_entry_close,
                self.benchmark_exit_close,
            )
        ):
            raise ValueError("nonentered/unknown-execution state forbids price payload")
        return self


def _snapshot_records(records: Sequence, record_type: type[BaseModel]) -> dict:
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
        raise TypeError("records must be a finite sequence of typed records")
    snapshot = {}
    for record in records:
        if type(record) is not record_type:
            raise TypeError(f"records must contain {record_type.__name__} instances")
        # Revalidate copies as frozen models can still be bypassed with model_copy
        # or model_construct. No caller-owned object enters the returned report.
        copied = record_type.model_validate(record.model_dump())
        if copied.decision_id in snapshot:
            raise ValueError("duplicate decision_id")
        snapshot[copied.decision_id] = copied
    return {key: snapshot[key] for key in sorted(snapshot)}


def _snapshot_mapping(value: Mapping) -> dict:
    if not isinstance(value, Mapping):
        raise TypeError("policies and selection maps must be mappings")
    snapshot = {}
    for key, item in value.items():
        if type(key) is not str:
            raise ValueError("mapping identifiers must be strings")
        _identifier(key)
        if key in snapshot:
            raise ValueError("duplicate mapping identifier")
        snapshot[key] = item
    return snapshot


def _potential_reward(outcome: BuybackOutcome, cost: float) -> dict:
    if outcome.state in {"CANCELED_BEFORE_ENTRY", "NO_FILL"}:
        return {"raw": (0.0, 0.0), "abnormal": (0.0, 0.0)}
    if outcome.state in _UNKNOWN_EXECUTION:
        return {"raw": (None, None), "abnormal": (None, None)}

    benchmark = None
    if (
        outcome.benchmark_entry_close is not None
        and outcome.benchmark_exit_close is not None
    ):
        benchmark = _finite(
            _finite(outcome.benchmark_exit_close / outcome.benchmark_entry_close) - 1
        )
    raw = None
    if outcome.state == "COMPLETED":
        raw = _finite(
            _finite(outcome.stock_exit_close / outcome.stock_entry_close) - 1 - cost
        )
    raw_lower = raw if raw is not None else _finite(-1 - cost)
    abnormal = None if raw is None or benchmark is None else _finite(raw - benchmark)
    abnormal_lower = None if benchmark is None else _finite(raw_lower - benchmark)
    return {"raw": (raw, raw_lower), "abnormal": (abnormal, abnormal_lower)}


def _reward_summary(
    points: list[float | None],
    bounds: list[float | None],
    entered_count: int | None,
) -> dict:
    known_sum = _sum([point for point in points if point is not None])
    unknown_count = points.count(None)
    total = None if unknown_count else known_sum
    lower = None if None in bounds else _sum(bounds)
    n = len(points)
    return {
        "known_reward_count": n - unknown_count,
        "unknown_reward_count": unknown_count,
        "known_reward_sum": known_sum,
        "point_total": total,
        "point_mean_per_eligible": None if not n or total is None else total / n,
        "point_mean_per_entered": (
            None if not entered_count or total is None else total / entered_count
        ),
        "lower_bound_total": lower,
        "lower_bound_mean_per_eligible": None if not n or lower is None else lower / n,
        "lower_bound_mean_per_entered": (
            None if not entered_count or lower is None else lower / entered_count
        ),
    }


def evaluate_buyback_policies(
    decisions: Sequence[EligibleDecision],
    outcomes: Sequence[BuybackOutcome],
    selections: Mapping[str, Mapping[str, bool]],
    *,
    cost_model: CostModel | None = None,
) -> dict:
    """Calculate same-cohort accounting for supplied masks, without fitting them.

    Numeric rewards are return fractions on unit same-notional positions, not
    compounded portfolio PnL. ``None`` is unavailable; known partial sums never
    substitute for complete point totals. Paired lifts have no bound estimate.
    """
    cohort = _snapshot_records(decisions, EligibleDecision)
    observed = _snapshot_records(outcomes, BuybackOutcome)
    ids = list(cohort)
    if set(observed) != set(cohort):
        raise ValueError("outcome IDs must exactly match the eligible cohort")

    policies = _snapshot_mapping(selections)
    if {"AlwaysTrade", "NeverTrade"} & policies.keys():
        raise ValueError("AlwaysTrade and NeverTrade policy names are reserved")
    for name, supplied in policies.items():
        mask = _snapshot_mapping(supplied)
        if set(mask) != set(cohort):
            raise ValueError("selection IDs must exactly match the eligible cohort")
        if any(type(selected) is not bool for selected in mask.values()):
            raise ValueError("selection values must be Python booleans")
        policies[name] = {key: mask[key] for key in ids}
    policies["AlwaysTrade"] = dict.fromkeys(ids, True)
    policies["NeverTrade"] = dict.fromkeys(ids, False)
    policies = {name: policies[name] for name in sorted(policies)}

    model = CostModel() if cost_model is None else cost_model
    if type(model) is not CostModel:
        raise TypeError("cost_model must be a CostModel")
    assumptions = {name: getattr(model, name) for name in CostModel.model_fields}
    for value in assumptions.values():
        if type(value) not in (int, float):
            raise ValueError("cost assumptions must be finite nonnegative numbers")
        try:
            valid = math.isfinite(value) and value >= 0
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError("cost assumptions must be finite nonnegative numbers")
    model = CostModel(**assumptions)

    rewards, rows = {}, []
    for key in ids:
        decision, outcome = cohort[key], observed[key]
        if (decision.entry_date, decision.exit_date) != (
            outcome.entry_date,
            outcome.exit_date,
        ):
            raise ValueError("outcome dates must exactly match declared decision dates")
        cost = _finite(
            model.roundtrip_cost(
                1,
                buy_date=decision.entry_date,
                sell_date=decision.exit_date,
                market=decision.market,
            )
        )
        rewards[key] = _potential_reward(outcome, cost)
        rows.append(
            {
                "decision": decision.model_dump(mode="json"),
                "outcome": outcome.model_dump(mode="json"),
                "cost_fraction_if_entered": cost,
                "potential_rewards": {
                    basis: {"point": point, "lower_bound": lower}
                    for basis, (point, lower) in rewards[key].items()
                },
            }
        )

    summaries = {}
    for name, mask in policies.items():
        selected_ids = [key for key in ids if mask[key]]
        states = [observed[key].state for key in selected_ids]
        known_entered = sum(state in _ENTERED for state in states)
        unknown_execution = sum(state in _UNKNOWN_EXECUTION for state in states)
        entered = None if unknown_execution else known_entered
        summaries[name] = {
            "eligible_count": len(ids),
            "selected_instruction_count": len(selected_ids),
            "selected_decision_ids": selected_ids,
            "selection_fraction": len(selected_ids) / len(ids) if ids else None,
            "abstained_count": len(ids) - len(selected_ids),
            "known_entered_position_count": known_entered,
            "unknown_execution_count": unknown_execution,
            "entered_position_count": entered,
            "selected_state_counts": {
                state: states.count(state) for state in OUTCOME_STATES
            },
            "rewards": {
                basis: _reward_summary(
                    [rewards[key][basis][0] if mask[key] else 0.0 for key in ids],
                    [rewards[key][basis][1] if mask[key] else 0.0 for key in ids],
                    entered,
                )
                for basis in ("raw", "abnormal")
            },
        }

    comparisons = {}
    for name, mask in policies.items():
        comparisons[name] = {}
        for reference, baseline in policies.items():
            if name == reference:
                continue
            paired = {}
            for basis in ("raw", "abnormal"):
                differences = []
                for key in ids:
                    difference = int(mask[key]) - int(baseline[key])
                    potential = rewards[key][basis][0]
                    differences.append(
                        0.0
                        if difference == 0
                        else (
                            None
                            if potential is None
                            else _finite(difference * potential)
                        )
                    )
                unknown = differences.count(None)
                known = _sum([item for item in differences if item is not None])
                paired[basis] = {
                    "unknown_difference_count": unknown,
                    "known_difference_sum": known,
                    "point_total": None if unknown else known,
                    "point_mean_per_eligible": (
                        None if unknown or not ids else known / len(ids)
                    ),
                }
            comparisons[name][reference] = {
                "eligible_count": len(ids),
                "selection_difference_count": sum(
                    mask[key] != baseline[key] for key in ids
                ),
                "rewards": paired,
            }

    return {
        "scope": "OFFLINE_ACCOUNTING_ONLY_NO_SOURCE_OR_PERFORMANCE_CLAIM",
        "status": "empty" if not ids else "nonempty",
        "reward_unit": "return_fraction_on_unit_same_notional",
        "eligible_count": len(ids),
        "issuer_count": len({decision.issuer_id for decision in cohort.values()}),
        "decision_ids": ids,
        "state_counts": {
            state: sum(outcome.state == state for outcome in observed.values())
            for state in OUTCOME_STATES
        },
        "cost_assumptions": {"model": "CostModel", "notional": 1, **assumptions},
        "cohort": rows,
        "policies": summaries,
        "paired_comparisons": comparisons,
    }
