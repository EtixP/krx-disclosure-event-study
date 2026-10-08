from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from kdtb.backtest.cost_model import (
    DEFAULT_COMMISSION_PER_SIDE,
    DEFAULT_SLIPPAGE_BPS_PER_SIDE,
    DEFAULT_VAT_ON_COMMISSION,
)
from kdtb.backtest.metrics import compute
from kdtb.research.inference import issuer_clustered_mean_ci
from kdtb.schemas.experiment import (
    ExperimentSpecification,
    OutcomeCriterion,
    require_utc,
)
from kdtb.schemas.forward_decision import ForwardDecision
from kdtb.schemas.forward_outcome import ForwardOutcome
from kdtb.schemas.historical_context import (
    HistoricalComparableOutcome,
    HistoricalContextDataset,
)

SEOUL = ZoneInfo("Asia/Seoul")
CONFIDENCE_LEVEL = 0.95
N_RESAMPLES = 10_000
RANDOM_STATE = 0


def _canonical_json(value: BaseModel) -> str:
    return json.dumps(
        value.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _finite(value: float, *, field: str) -> float:
    if not math.isfinite(value):
        raise ValueError(f"{field} must be finite")
    return value


class ForwardReportDecisionRecord(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    decision: ForwardDecision
    recorded_at: datetime

    @field_validator("recorded_at")
    @classmethod
    def utc_recorded_at(cls, value: datetime) -> datetime:
        return require_utc(value, field="decision recorded_at")

    @model_validator(mode="after")
    def chronology_is_consistent(self) -> "ForwardReportDecisionRecord":
        if self.recorded_at < self.decision.inputs.assessed_at:
            raise ValueError("decision report record predates its assessment")
        return self


class ForwardReportOutcomeRecord(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    outcome: ForwardOutcome
    recorded_at: datetime

    @field_validator("recorded_at")
    @classmethod
    def utc_recorded_at(cls, value: datetime) -> datetime:
        return require_utc(value, field="outcome recorded_at")

    @model_validator(mode="after")
    def chronology_is_consistent(self) -> "ForwardReportOutcomeRecord":
        if self.recorded_at < self.outcome.evaluated_at:
            raise ValueError("outcome report record predates its evaluation")
        return self


class ReturnSampleMetrics(BaseModel):
    """Common descriptive metrics; the sample label lives in its section."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    sample_size: int = Field(ge=1)
    issuer_count: int = Field(ge=1)
    mean_stock_gross_return: float
    mean_benchmark_gross_return: float
    mean_cost_fraction: float = Field(ge=0)
    mean_raw_net_return: float
    mean_abnormal_net_return: float
    median_abnormal_net_return: float
    std_abnormal_net_return: float = Field(ge=0)
    positive_fraction: float = Field(ge=0, le=1)
    profit_factor: float | None = Field(default=None, ge=0)

    @field_validator(
        "mean_stock_gross_return",
        "mean_benchmark_gross_return",
        "mean_cost_fraction",
        "mean_raw_net_return",
        "mean_abnormal_net_return",
        "median_abnormal_net_return",
        "std_abnormal_net_return",
        "positive_fraction",
    )
    @classmethod
    def finite_metrics(cls, value: float, info) -> float:
        return _finite(value, field=info.field_name)

    @field_validator("profit_factor")
    @classmethod
    def finite_profit_factor(cls, value: float | None) -> float | None:
        return value if value is None else _finite(value, field="profit_factor")

    @model_validator(mode="after")
    def counts_are_consistent(self) -> "ReturnSampleMetrics":
        if self.issuer_count > self.sample_size:
            raise ValueError("metric issuer count cannot exceed sample size")
        return self


class MeanUncertainty(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    target_metric: Literal["mean_abnormal_net_return"] = "mean_abnormal_net_return"
    status: Literal["available", "no_observations", "insufficient_issuers"]
    ci_lower: float | None = None
    ci_upper: float | None = None
    bootstrap_standard_error: float | None = Field(default=None, ge=0)
    confidence_level: Literal[0.95] = CONFIDENCE_LEVEL
    n_resamples: Literal[10000] = N_RESAMPLES
    random_state: Literal[0] = RANDOM_STATE
    resampling_unit: Literal["issuer"] = "issuer"
    estimand: Literal["event_weighted_mean"] = "event_weighted_mean"
    interval: Literal["percentile_cluster_bootstrap"] = "percentile_cluster_bootstrap"

    @field_validator("ci_lower", "ci_upper", "bootstrap_standard_error")
    @classmethod
    def finite_optional(cls, value: float | None, info) -> float | None:
        return value if value is None else _finite(value, field=info.field_name)

    @model_validator(mode="after")
    def status_is_consistent(self) -> "MeanUncertainty":
        values = (self.ci_lower, self.ci_upper, self.bootstrap_standard_error)
        if self.status == "available":
            if any(value is None for value in values):
                raise ValueError("available uncertainty requires interval values")
            if self.ci_lower > self.ci_upper:
                raise ValueError("uncertainty interval bounds are reversed")
        elif any(value is not None for value in values):
            raise ValueError("unavailable uncertainty cannot contain estimates")
        return self


class RejectionSummary(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")
    decision_count: int = Field(ge=1)
    trigger_receipt_nos: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def receipts_are_consistent(self) -> "RejectionSummary":
        if (
            self.trigger_receipt_nos != tuple(sorted(self.trigger_receipt_nos))
            or len(self.trigger_receipt_nos) != len(set(self.trigger_receipt_nos))
            or any(
                len(value) != 14 or not value.isdigit()
                for value in self.trigger_receipt_nos
            )
        ):
            raise ValueError("rejection receipt numbers must be sorted and unique")
        if self.decision_count != len(self.trigger_receipt_nos):
            raise ValueError("rejection count must match its unique decisions")
        return self


class HistoricalReportSection(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    sample_kind: Literal["historical_pre_specification_context"] = (
        "historical_pre_specification_context"
    )
    sample_label: Literal["HISTORICAL — NOT FORWARD"] = "HISTORICAL — NOT FORWARD"
    is_historical: Literal[True] = True
    is_forward: Literal[False] = False
    status: Literal[
        "available",
        "no_decisions",
        "no_context",
        "no_pre_cutoff_outcomes",
    ]
    historical_cutoff_date: date
    cutoff_policy: Literal["exit_date_strictly_before_frozen_historical_cutoff"] = (
        "exit_date_strictly_before_frozen_historical_cutoff"
    )
    selection_basis: Literal[
        "deduplicated_decision_contexts_not_historical_strategy_replay"
    ] = "deduplicated_decision_contexts_not_historical_strategy_replay"
    outcome_basis_status: Literal[
        "aligned_to_frozen_outcome_basis",
        "reference_only_outcome_basis_mismatch",
    ]
    decision_contexts: int = Field(ge=0)
    context_results: int = Field(ge=0)
    context_coverage_fraction: float | None = Field(default=None, ge=0, le=1)
    sample_size: int = Field(ge=0)
    issuer_count: int = Field(ge=0)
    sources: tuple[HistoricalContextDataset, ...]
    observations: tuple[HistoricalComparableOutcome, ...]
    metrics: ReturnSampleMetrics | None = None
    uncertainty: MeanUncertainty
    limitations: tuple[str, ...] = Field(min_length=1)


class ForwardReportSection(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    sample_kind: Literal["prospective_forward"] = "prospective_forward"
    sample_label: Literal["FORWARD — PROSPECTIVE ONLY"] = "FORWARD — PROSPECTIVE ONLY"
    is_historical: Literal[False] = False
    is_forward: Literal[True] = True
    status: Literal[
        "no_decisions",
        "no_eligible_decisions",
        "no_outcomes",
        "partial_outcomes",
        "observed_outcomes_complete",
    ]
    considered_decisions: int = Field(ge=0)
    eligible_decisions: int = Field(ge=0)
    rejected_decisions: int = Field(ge=0)
    outcome_count: int = Field(ge=0)
    eligible_without_outcome: int = Field(ge=0)
    outcome_coverage_fraction: float | None = Field(default=None, ge=0, le=1)
    eligible_without_outcome_receipt_nos: tuple[str, ...]
    rejection_summaries: tuple[RejectionSummary, ...]
    sample_size: int = Field(ge=0)
    issuer_count: int = Field(ge=0)
    metrics: ReturnSampleMetrics | None = None
    uncertainty: MeanUncertainty
    limitations: tuple[str, ...] = Field(min_length=1)


class CriterionResult(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    side: Literal["success", "failure"]
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    metric: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    operator: Literal["gt", "gte", "lt", "lte"]
    threshold: float
    observed_value: int | float | None
    matched: bool | None

    @field_validator("threshold")
    @classmethod
    def finite_threshold(cls, value: float) -> float:
        return _finite(value, field="criterion threshold")

    @field_validator("observed_value")
    @classmethod
    def finite_observed(cls, value: int | float | None) -> int | float | None:
        if value is not None:
            _finite(float(value), field="criterion observed value")
        return value

    @model_validator(mode="after")
    def match_is_consistent(self) -> "CriterionResult":
        if (self.observed_value is None) != (self.matched is None):
            raise ValueError("criterion match availability is inconsistent")
        return self


class ForwardEvaluationResult(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    status: Literal[
        "pre_start",
        "collecting",
        "awaiting_outcomes",
        "insufficient_data",
        "metric_unavailable",
        "success",
        "failure",
        "inconclusive",
    ]
    classification_is_final: bool
    evaluation_end: datetime
    cohort_policy: Literal["decision_recorded_at_on_or_before_evaluation_end"] = (
        "decision_recorded_at_on_or_before_evaluation_end"
    )
    cohort_decisions: int = Field(ge=0)
    cohort_eligible_decisions: int = Field(ge=0)
    cohort_rejected_decisions: int = Field(ge=0)
    cohort_outcome_count: int = Field(ge=0)
    cohort_issuer_count: int = Field(ge=0)
    cohort_eligible_without_outcome: int = Field(ge=0)
    post_window_decisions_excluded: int = Field(ge=0)
    post_window_trigger_receipt_nos: tuple[str, ...]
    minimum_events: int = Field(ge=1)
    minimum_issuers: int = Field(ge=1)
    minimum_events_met: bool
    minimum_issuers_met: bool
    primary_metric: str
    primary_metric_value: int | float | None
    success_criteria_met: bool | None
    failure_criteria_met: bool | None
    criteria: tuple[CriterionResult, ...]
    detail: str = Field(min_length=1)

    @field_validator("evaluation_end")
    @classmethod
    def utc_evaluation_end(cls, value: datetime) -> datetime:
        return require_utc(value, field="report evaluation_end")

    @field_validator("primary_metric_value")
    @classmethod
    def finite_primary(cls, value: int | float | None) -> int | float | None:
        if value is not None:
            _finite(float(value), field="primary metric")
        return value

    @model_validator(mode="after")
    def cohort_is_consistent(self) -> "ForwardEvaluationResult":
        receipts = self.post_window_trigger_receipt_nos
        if (
            receipts != tuple(sorted(receipts))
            or len(receipts) != len(set(receipts))
            or any(len(value) != 14 or not value.isdigit() for value in receipts)
        ):
            raise ValueError("post-window receipt numbers must be sorted and unique")
        if self.post_window_decisions_excluded != len(receipts):
            raise ValueError("post-window decision count does not match receipts")
        if (
            self.cohort_eligible_decisions + self.cohort_rejected_decisions
            != self.cohort_decisions
        ):
            raise ValueError("evaluation cohort decisions do not reconcile")
        if (
            self.cohort_outcome_count + self.cohort_eligible_without_outcome
            != self.cohort_eligible_decisions
        ):
            raise ValueError("evaluation cohort outcomes do not reconcile")
        if self.cohort_issuer_count > self.cohort_outcome_count:
            raise ValueError("evaluation cohort issuer count exceeds outcomes")
        if self.minimum_events_met != (
            self.cohort_outcome_count >= self.minimum_events
        ) or self.minimum_issuers_met != (
            self.cohort_issuer_count >= self.minimum_issuers
        ):
            raise ValueError("evaluation minimum-data flags do not reconcile")
        terminal = self.status in {
            "insufficient_data",
            "metric_unavailable",
            "success",
            "failure",
            "inconclusive",
        }
        if self.classification_is_final != terminal:
            raise ValueError("evaluation finality does not match its status")
        if self.status == "awaiting_outcomes" and not (
            self.cohort_eligible_without_outcome > 0
        ):
            raise ValueError("awaiting-outcomes status requires unresolved outcomes")
        return self


def _metrics(
    *,
    stock: tuple[float, ...],
    benchmark: tuple[float, ...],
    costs: tuple[float, ...],
    raw: tuple[float, ...],
    abnormal: tuple[float, ...],
    issuers: tuple[str, ...],
) -> ReturnSampleMetrics | None:
    if not abnormal:
        return None
    trade_metrics = compute(abnormal)
    return ReturnSampleMetrics(
        sample_size=len(abnormal),
        issuer_count=len(set(issuers)),
        mean_stock_gross_return=sum(stock) / len(stock),
        mean_benchmark_gross_return=sum(benchmark) / len(benchmark),
        mean_cost_fraction=sum(costs) / len(costs),
        mean_raw_net_return=sum(raw) / len(raw),
        mean_abnormal_net_return=trade_metrics.mean_return,
        median_abnormal_net_return=trade_metrics.median_return,
        std_abnormal_net_return=trade_metrics.std_return,
        positive_fraction=trade_metrics.win_rate,
        profit_factor=trade_metrics.profit_factor,
    )


def _uncertainty(
    abnormal: tuple[float, ...],
    issuers: tuple[str, ...],
) -> MeanUncertainty:
    if not abnormal:
        return MeanUncertainty(status="no_observations")
    if len(set(issuers)) < 2:
        return MeanUncertainty(status="insufficient_issuers")
    result = issuer_clustered_mean_ci(
        abnormal,
        issuers,
        confidence_level=CONFIDENCE_LEVEL,
        n_resamples=N_RESAMPLES,
        random_state=RANDOM_STATE,
    )
    return MeanUncertainty(
        status="available",
        ci_lower=result["ci_lower"],
        ci_upper=result["ci_upper"],
        bootstrap_standard_error=result["bootstrap_standard_error"],
    )


def _rule_parameters(specification: ExperimentSpecification, rule: str) -> dict:
    target = specification.entry_rule if rule == "entry" else specification.exit_rule
    return {item.name: item.value for item in target.parameters}


def _historical_outcome_basis(specification: ExperimentSpecification) -> str:
    mappings = {
        item.market: item.symbol for item in specification.benchmark.market_mappings
    }
    aligned = (
        specification.entry_rule.rule_id == "next_trading_day_close"
        and specification.entry_rule.rule_version == "m2.1-v1"
        and _rule_parameters(specification, "entry") == {"session_offset": 1}
        and specification.exit_rule.rule_id == "trading_session_close"
        and specification.exit_rule.rule_version == "m2.1-v1"
        and _rule_parameters(specification, "exit") == {"holding_sessions": 4}
        and specification.benchmark.source == "NAVER_FINANCE_DOMESTIC_INDEX_DAILY"
        and specification.benchmark.source_version == "m0.3-v1"
        and specification.benchmark.alignment == "exact_trading_date"
        and specification.benchmark.return_type == "price_return"
        and specification.benchmark.missing_data_policy == "fail"
        and all(mappings.get(market) == market for market in mappings)
        and specification.costs.model_id == "korean_equity_roundtrip"
        and specification.costs.model_version == "m0.2-v1"
        and specification.costs.commission_per_side == DEFAULT_COMMISSION_PER_SIDE
        and specification.costs.vat_on_commission == DEFAULT_VAT_ON_COMMISSION
        and specification.costs.slippage_bps_per_side == DEFAULT_SLIPPAGE_BPS_PER_SIDE
        and specification.costs.tax_policy_id == "korean_equity_transaction_tax"
        and specification.costs.tax_policy_version == "verified_2021_2026"
    )
    return (
        "aligned_to_frozen_outcome_basis"
        if aligned
        else "reference_only_outcome_basis_mismatch"
    )


def _dataset_key(value: HistoricalContextDataset) -> str:
    return json.dumps(
        value.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _historical_section(
    specification: ExperimentSpecification,
    decisions: tuple[ForwardReportDecisionRecord, ...],
) -> HistoricalReportSection:
    cutoff_date = specification.historical_cutoff.astimezone(SEOUL).date()
    contexts = tuple(
        item.decision.inputs.historical_context.result for item in decisions
    )
    results = tuple(item for item in contexts if item is not None)

    sources_by_key = {_dataset_key(item.dataset): item.dataset for item in results}
    sources = tuple(sources_by_key[key] for key in sorted(sources_by_key))
    observations_by_receipt: dict[str, HistoricalComparableOutcome] = {}
    for result in results:
        for observation in result.comparables:
            if observation.exit_date >= cutoff_date:
                continue
            existing = observations_by_receipt.get(observation.receipt_no)
            if existing is not None and existing != observation:
                raise ValueError(
                    "historical contexts disagree on one receipt's outcome"
                )
            observations_by_receipt[observation.receipt_no] = observation
    observations = tuple(
        observations_by_receipt[key] for key in sorted(observations_by_receipt)
    )
    issuers = tuple(item.stock_code for item in observations)
    stock = tuple(item.stock_gross_return for item in observations)
    benchmark = tuple(item.benchmark_gross_return for item in observations)
    costs = tuple(item.modeled_cost_fraction for item in observations)
    raw = tuple(
        item.stock_gross_return - item.modeled_cost_fraction for item in observations
    )
    abnormal = tuple(item.abnormal_net_return for item in observations)
    metrics = _metrics(
        stock=stock,
        benchmark=benchmark,
        costs=costs,
        raw=raw,
        abnormal=abnormal,
        issuers=issuers,
    )
    if not decisions:
        status = "no_decisions"
    elif not results:
        status = "no_context"
    elif not observations:
        status = "no_pre_cutoff_outcomes"
    else:
        status = "available"
    coverage = len(results) / len(decisions) if decisions else None
    outcome_basis = _historical_outcome_basis(specification)
    limitations = [
        "Historical rows are deduplicated decision-embedded M1.4 context, not prospective outcomes.",
        "Historical selection does not replay the frozen experiment eligibility rules.",
        "Only outcomes strictly before the frozen historical-cutoff Seoul date are included.",
    ]
    if outcome_basis != "aligned_to_frozen_outcome_basis":
        limitations.append(
            "The historical context outcome basis differs from the frozen experiment and is reference-only."
        )
    return HistoricalReportSection(
        status=status,
        historical_cutoff_date=cutoff_date,
        outcome_basis_status=outcome_basis,
        decision_contexts=len(decisions),
        context_results=len(results),
        context_coverage_fraction=coverage,
        sample_size=len(observations),
        issuer_count=len(set(issuers)),
        sources=sources,
        observations=observations,
        metrics=metrics,
        uncertainty=_uncertainty(abnormal, issuers),
        limitations=tuple(limitations),
    )


def _forward_section(
    decisions: tuple[ForwardReportDecisionRecord, ...],
    outcomes: tuple[ForwardReportOutcomeRecord, ...],
) -> ForwardReportSection:
    eligible = tuple(
        item for item in decisions if item.decision.disposition == "eligible"
    )
    rejected = tuple(
        item for item in decisions if item.decision.disposition == "rejected"
    )
    outcome_receipts = {item.outcome.trigger_receipt_no for item in outcomes}
    missing_receipts = tuple(
        sorted(
            item.decision.inputs.trigger_receipt_no
            for item in eligible
            if item.decision.inputs.trigger_receipt_no not in outcome_receipts
        )
    )
    reason_receipts: defaultdict[str, set[str]] = defaultdict(set)
    for item in rejected:
        receipt_no = item.decision.inputs.trigger_receipt_no
        for reason in item.decision.rejection_reasons:
            reason_receipts[reason.code].add(receipt_no)
    rejection_summaries = tuple(
        RejectionSummary(
            code=code,
            decision_count=len(reason_receipts[code]),
            trigger_receipt_nos=tuple(sorted(reason_receipts[code])),
        )
        for code in sorted(reason_receipts)
    )
    if not decisions:
        status = "no_decisions"
    elif not eligible:
        status = "no_eligible_decisions"
    elif not outcomes:
        status = "no_outcomes"
    elif missing_receipts:
        status = "partial_outcomes"
    else:
        status = "observed_outcomes_complete"

    issuers = tuple(item.outcome.issuer_stock_code for item in outcomes)
    stock = tuple(item.outcome.stock_gross_return for item in outcomes)
    benchmark = tuple(item.outcome.benchmark_gross_return for item in outcomes)
    costs = tuple(item.outcome.roundtrip_cost_fraction for item in outcomes)
    raw = tuple(item.outcome.raw_net_return for item in outcomes)
    abnormal = tuple(item.outcome.abnormal_net_return for item in outcomes)
    return ForwardReportSection(
        status=status,
        considered_decisions=len(decisions),
        eligible_decisions=len(eligible),
        rejected_decisions=len(rejected),
        outcome_count=len(outcomes),
        eligible_without_outcome=len(missing_receipts),
        outcome_coverage_fraction=(len(outcomes) / len(eligible) if eligible else None),
        eligible_without_outcome_receipt_nos=missing_receipts,
        rejection_summaries=rejection_summaries,
        sample_size=len(outcomes),
        issuer_count=len(set(issuers)),
        metrics=_metrics(
            stock=stock,
            benchmark=benchmark,
            costs=costs,
            raw=raw,
            abnormal=abnormal,
            issuers=issuers,
        ),
        uncertainty=_uncertainty(abnormal, issuers),
        limitations=(
            "Outcome coverage is measured among ledger-recorded eligible decisions, not the external disclosure universe.",
            "Eligible decisions without outcomes may be immature or missing required evidence; this report does not impute them.",
            "Prospective outcomes are research observations and do not authorize execution.",
        ),
    )


def _metric_values(section: ForwardReportSection) -> dict[str, int | float | None]:
    metrics = section.metrics
    values: dict[str, int | float | None] = {
        "sample_size": section.sample_size,
        "issuer_count": section.issuer_count,
        "considered_decisions": section.considered_decisions,
        "eligible_decisions": section.eligible_decisions,
        "rejected_decisions": section.rejected_decisions,
        "outcome_coverage_fraction": section.outcome_coverage_fraction,
        "mean_stock_gross_return": None,
        "mean_benchmark_gross_return": None,
        "mean_cost_fraction": None,
        "mean_raw_net_return": None,
        "mean_abnormal_net_return": None,
        "median_abnormal_net_return": None,
        "std_abnormal_net_return": None,
        "positive_fraction": None,
        "profit_factor": None,
    }
    if metrics is not None:
        values.update(
            {
                name: getattr(metrics, name)
                for name in (
                    "mean_stock_gross_return",
                    "mean_benchmark_gross_return",
                    "mean_cost_fraction",
                    "mean_raw_net_return",
                    "mean_abnormal_net_return",
                    "median_abnormal_net_return",
                    "std_abnormal_net_return",
                    "positive_fraction",
                    "profit_factor",
                )
            }
        )
    return values


def _criterion_result(
    criterion: OutcomeCriterion,
    *,
    side: Literal["success", "failure"],
    values: dict[str, int | float | None],
) -> CriterionResult:
    if criterion.metric not in values:
        raise ValueError(f"unsupported frozen evaluation metric: {criterion.metric}")
    observed = values[criterion.metric]
    if observed is None:
        matched = None
    elif criterion.operator == "gt":
        matched = observed > criterion.threshold
    elif criterion.operator == "gte":
        matched = observed >= criterion.threshold
    elif criterion.operator == "lt":
        matched = observed < criterion.threshold
    else:
        matched = observed <= criterion.threshold
    return CriterionResult(
        side=side,
        name=criterion.name,
        metric=criterion.metric,
        operator=criterion.operator,
        threshold=criterion.threshold,
        observed_value=observed,
        matched=matched,
    )


def _aggregate_matches(
    values: tuple[bool | None, ...],
    *,
    policy: Literal["all", "any"],
) -> bool | None:
    if policy == "all":
        if any(value is False for value in values):
            return False
        return None if any(value is None for value in values) else True
    if any(value is True for value in values):
        return True
    return None if any(value is None for value in values) else False


def _evaluation(
    specification: ExperimentSpecification,
    *,
    as_of: datetime,
    decisions: tuple[ForwardReportDecisionRecord, ...],
    outcomes: tuple[ForwardReportOutcomeRecord, ...],
) -> ForwardEvaluationResult:
    plan = specification.evaluation
    cohort_decisions = tuple(
        item for item in decisions if item.recorded_at <= plan.evaluation_end
    )
    cohort_decision_ids = {item.decision.decision_id for item in cohort_decisions}
    cohort_outcomes = tuple(
        item for item in outcomes if item.outcome.decision_id in cohort_decision_ids
    )
    post_window_decisions = tuple(
        item for item in decisions if item.recorded_at > plan.evaluation_end
    )
    forward = _forward_section(cohort_decisions, cohort_outcomes)
    values = _metric_values(forward)
    if plan.primary_metric not in values:
        raise ValueError(f"unsupported frozen evaluation metric: {plan.primary_metric}")
    criteria = tuple(
        _criterion_result(criterion, side="success", values=values)
        for criterion in plan.success_criteria
    ) + tuple(
        _criterion_result(criterion, side="failure", values=values)
        for criterion in plan.failure_criteria
    )
    success_met = _aggregate_matches(
        tuple(item.matched for item in criteria if item.side == "success"),
        policy=plan.success_criteria_policy,
    )
    failure_met = _aggregate_matches(
        tuple(item.matched for item in criteria if item.side == "failure"),
        policy=plan.failure_criteria_policy,
    )
    events_met = forward.sample_size >= plan.minimum_events
    issuers_met = forward.issuer_count >= plan.minimum_issuers
    if as_of < specification.forward_test_start:
        status = "pre_start"
        detail = "Forward testing has not started; no evaluation is claimed."
    elif as_of < plan.evaluation_end:
        status = "collecting"
        detail = "The frozen evaluation window is still collecting prospective data."
    elif forward.eligible_without_outcome:
        status = "awaiting_outcomes"
        detail = (
            "The evaluation window ended, but eligible decisions still lack "
            "outcomes; no terminal-missing policy is frozen."
        )
    elif not events_met or not issuers_met:
        status = "insufficient_data"
        detail = "The frozen minimum event or issuer requirement was not met."
    elif success_met is None or failure_met is None:
        status = "metric_unavailable"
        detail = "At least one frozen criterion metric is unavailable."
    elif failure_met:
        status = "failure"
        detail = "At least one failure criterion matched; failure has precedence."
    elif success_met:
        status = "success"
        detail = "All frozen success criteria matched and no failure criterion matched."
    else:
        status = "inconclusive"
        detail = "Neither the frozen success nor failure classification matched."
    return ForwardEvaluationResult(
        status=status,
        classification_is_final=(
            as_of >= plan.evaluation_end and forward.eligible_without_outcome == 0
        ),
        evaluation_end=plan.evaluation_end,
        cohort_decisions=forward.considered_decisions,
        cohort_eligible_decisions=forward.eligible_decisions,
        cohort_rejected_decisions=forward.rejected_decisions,
        cohort_outcome_count=forward.outcome_count,
        cohort_issuer_count=forward.issuer_count,
        cohort_eligible_without_outcome=forward.eligible_without_outcome,
        post_window_decisions_excluded=len(post_window_decisions),
        post_window_trigger_receipt_nos=tuple(
            sorted(
                item.decision.inputs.trigger_receipt_no
                for item in post_window_decisions
            )
        ),
        minimum_events=plan.minimum_events,
        minimum_issuers=plan.minimum_issuers,
        minimum_events_met=events_met,
        minimum_issuers_met=issuers_met,
        primary_metric=plan.primary_metric,
        primary_metric_value=values[plan.primary_metric],
        success_criteria_met=success_met,
        failure_criteria_met=failure_met,
        criteria=criteria,
        detail=detail,
    )


class ForwardExperimentReport(BaseModel):
    """Auditable side-by-side report; historical and forward remain separate."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    report_schema_version: Literal["m2.4-v1"] = "m2.4-v1"
    experiment: ExperimentSpecification
    experiment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    experiment_activated_at: datetime
    as_of: datetime
    generated_at: datetime
    decision_records: tuple[ForwardReportDecisionRecord, ...]
    outcome_records: tuple[ForwardReportOutcomeRecord, ...]
    historical: HistoricalReportSection
    forward: ForwardReportSection
    evaluation: ForwardEvaluationResult
    historical_and_forward_are_combined: Literal[False] = False
    is_trade_recommendation: Literal[False] = False
    trading_recommendation: None = None
    authorizes_execution: Literal[False] = False

    @field_validator("experiment_activated_at", "as_of", "generated_at")
    @classmethod
    def utc_timestamps(cls, value: datetime, info) -> datetime:
        return require_utc(value, field=info.field_name)

    @model_validator(mode="after")
    def report_is_consistent(self) -> "ForwardExperimentReport":
        if self.experiment_sha256 != self.experiment.sha256():
            raise ValueError("report experiment hash does not match its definition")
        if self.experiment_activated_at > self.experiment.forward_test_start:
            raise ValueError("report experiment was activated after its forward start")
        if self.as_of < self.experiment_activated_at:
            raise ValueError("report cutoff cannot precede experiment activation")
        if self.as_of > self.generated_at:
            raise ValueError("report cutoff cannot follow report generation")

        decision_keys = tuple(
            (
                item.recorded_at,
                item.decision.inputs.assessed_at,
                item.decision.inputs.trigger_receipt_no,
                item.decision.decision_id,
            )
            for item in self.decision_records
        )
        if decision_keys != tuple(sorted(decision_keys)):
            raise ValueError("report decisions must use deterministic order")
        decision_ids = tuple(
            item.decision.decision_id for item in self.decision_records
        )
        decision_receipts = tuple(
            item.decision.inputs.trigger_receipt_no for item in self.decision_records
        )
        if len(decision_ids) != len(set(decision_ids)) or len(decision_receipts) != len(
            set(decision_receipts)
        ):
            raise ValueError("report decisions must be unique")
        if any(item.recorded_at > self.as_of for item in self.decision_records):
            raise ValueError("report contains a decision recorded after its cutoff")
        if any(
            item.decision.experiment != self.experiment
            or item.decision.experiment_sha256 != self.experiment_sha256
            or item.decision.experiment_activated_at != self.experiment_activated_at
            for item in self.decision_records
        ):
            raise ValueError("report decision does not match the experiment")

        outcome_keys = tuple(
            (
                item.recorded_at,
                item.outcome.evaluated_at,
                item.outcome.trigger_receipt_no,
                item.outcome.outcome_id,
            )
            for item in self.outcome_records
        )
        if outcome_keys != tuple(sorted(outcome_keys)):
            raise ValueError("report outcomes must use deterministic order")
        outcome_ids = tuple(item.outcome.outcome_id for item in self.outcome_records)
        outcome_receipts = tuple(
            item.outcome.trigger_receipt_no for item in self.outcome_records
        )
        if len(outcome_ids) != len(set(outcome_ids)) or len(outcome_receipts) != len(
            set(outcome_receipts)
        ):
            raise ValueError("report outcomes must be unique")
        if any(item.recorded_at > self.as_of for item in self.outcome_records):
            raise ValueError("report contains an outcome recorded after its cutoff")

        decisions_by_id = {
            item.decision.decision_id: item for item in self.decision_records
        }
        for item in self.outcome_records:
            outcome = item.outcome
            decision_record = decisions_by_id.get(outcome.decision_id)
            if decision_record is None:
                raise ValueError("report outcome has no included decision")
            decision = decision_record.decision
            event = decision.inputs.event
            expected = (
                decision.sha256(),
                decision.inputs.assessed_at,
                decision.experiment.experiment_id,
                decision.experiment.version,
                decision.experiment_sha256,
                decision.inputs.trigger_receipt_no,
                event.issuer.stock_code,
                event.market,
                decision.experiment.entry_rule,
                decision.experiment.exit_rule,
                decision.experiment.benchmark,
                decision.experiment.costs,
                "eligible",
            )
            actual = (
                outcome.decision_sha256,
                outcome.decision_assessed_at,
                outcome.experiment_id,
                outcome.experiment_version,
                outcome.experiment_sha256,
                outcome.trigger_receipt_no,
                outcome.issuer_stock_code,
                outcome.market,
                outcome.entry_rule,
                outcome.exit_rule,
                outcome.benchmark,
                outcome.costs,
                decision.disposition,
            )
            if actual != expected:
                raise ValueError("report outcome does not match its eligible decision")
            if item.recorded_at < decision_record.recorded_at:
                raise ValueError("report outcome was recorded before its decision")

        expected_historical = _historical_section(
            self.experiment,
            self.decision_records,
        )
        expected_forward = _forward_section(
            self.decision_records,
            self.outcome_records,
        )
        expected_evaluation = _evaluation(
            self.experiment,
            as_of=self.as_of,
            decisions=self.decision_records,
            outcomes=self.outcome_records,
        )
        if self.historical != expected_historical:
            raise ValueError("historical report section does not match source records")
        if self.forward != expected_forward:
            raise ValueError("forward report section does not match source records")
        if self.evaluation != expected_evaluation:
            raise ValueError("forward evaluation does not match frozen criteria")
        return self

    @classmethod
    def from_records(
        cls,
        *,
        experiment: ExperimentSpecification,
        experiment_activated_at: datetime,
        as_of: datetime,
        generated_at: datetime,
        decision_records: tuple[ForwardReportDecisionRecord, ...],
        outcome_records: tuple[ForwardReportOutcomeRecord, ...],
    ) -> "ForwardExperimentReport":
        decisions = tuple(
            sorted(
                decision_records,
                key=lambda item: (
                    item.recorded_at,
                    item.decision.inputs.assessed_at,
                    item.decision.inputs.trigger_receipt_no,
                    item.decision.decision_id,
                ),
            )
        )
        outcomes = tuple(
            sorted(
                outcome_records,
                key=lambda item: (
                    item.recorded_at,
                    item.outcome.evaluated_at,
                    item.outcome.trigger_receipt_no,
                    item.outcome.outcome_id,
                ),
            )
        )
        historical = _historical_section(experiment, decisions)
        forward = _forward_section(decisions, outcomes)
        return cls(
            experiment=experiment,
            experiment_sha256=experiment.sha256(),
            experiment_activated_at=experiment_activated_at,
            as_of=as_of,
            generated_at=generated_at,
            decision_records=decisions,
            outcome_records=outcomes,
            historical=historical,
            forward=forward,
            evaluation=_evaluation(
                experiment,
                as_of=as_of,
                decisions=decisions,
                outcomes=outcomes,
            ),
        )

    def canonical_json(self) -> str:
        return _canonical_json(self)

    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()
