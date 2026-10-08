from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Callable

from kdtb.experiments.ledger import ForwardDecisionLedger
from kdtb.experiments.outcomes import ForwardOutcomeLedger
from kdtb.experiments.registry import ExperimentRegistry
from kdtb.schemas.experiment import require_utc
from kdtb.schemas.forward_report import (
    ForwardExperimentReport,
    ForwardReportDecisionRecord,
    ForwardReportOutcomeRecord,
    MeanUncertainty,
    ReturnSampleMetrics,
)


class ForwardReportError(ValueError):
    """Raised when an auditable M2.4 report cannot be produced."""


class ForwardReportService:
    """Build a deterministic report from immutable registry and ledger facts."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        registry: ExperimentRegistry | None = None,
        decision_ledger: ForwardDecisionLedger | None = None,
        outcome_ledger: ForwardOutcomeLedger | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.conn = conn
        self.registry = registry or ExperimentRegistry(conn)
        self.decision_ledger = decision_ledger or ForwardDecisionLedger(
            conn,
            registry=self.registry,
        )
        self.outcome_ledger = outcome_ledger or ForwardOutcomeLedger(
            conn,
            decision_ledger=self.decision_ledger,
        )
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        if any(
            item.conn is not conn
            for item in (
                self.registry,
                self.decision_ledger,
                self.outcome_ledger,
            )
        ):
            raise ValueError("report registry and ledgers must share one connection")

    def _now(self) -> datetime:
        return require_utc(self.clock(), field="report service clock")

    def build(
        self,
        experiment_id: str,
        version: int,
        *,
        as_of: datetime,
    ) -> ForwardExperimentReport:
        cutoff = require_utc(as_of, field="report as_of")
        generated_at = self._now()
        if cutoff > generated_at:
            raise ForwardReportError(
                "report cutoff cannot follow trusted generation time"
            )
        try:
            registration = self.registry.get(experiment_id, version)
            if registration is None:
                raise ForwardReportError(
                    f"unknown experiment version: {experiment_id} v{version}"
                )
            if registration.activated_at is None:
                raise ForwardReportError(
                    f"experiment is not activated: {experiment_id} v{version}"
                )
            decisions = tuple(
                ForwardReportDecisionRecord(
                    decision=item.decision,
                    recorded_at=item.recorded_at,
                )
                for item in self.decision_ledger.list_for_experiment(experiment_id)
                if item.decision.experiment.version == version
                and item.recorded_at <= cutoff
            )
            outcomes = tuple(
                ForwardReportOutcomeRecord(
                    outcome=item.outcome,
                    recorded_at=item.recorded_at,
                )
                for item in self.outcome_ledger.list_for_experiment(experiment_id)
                if item.outcome.experiment_version == version
                and item.recorded_at <= cutoff
            )
            return ForwardExperimentReport.from_records(
                experiment=registration.specification,
                experiment_activated_at=registration.activated_at,
                as_of=cutoff,
                generated_at=generated_at,
                decision_records=decisions,
                outcome_records=outcomes,
            )
        except ForwardReportError:
            raise
        except Exception as error:
            raise ForwardReportError(
                f"could not build report for {experiment_id} v{version}: {error}"
            ) from error


def _percentage(value: float | None) -> str:
    return "not available" if value is None else f"{value * 100:.4f}%"


def _metric_lines(metrics: ReturnSampleMetrics | None) -> list[str]:
    if metrics is None:
        return ["- return metrics: not available (no observed outcomes)"]
    return [
        f"- issuers: {metrics.issuer_count}",
        "- mean abnormal net return: "
        f"{_percentage(metrics.mean_abnormal_net_return)}",
        f"- mean raw net return: {_percentage(metrics.mean_raw_net_return)}",
        f"- positive fraction: {_percentage(metrics.positive_fraction)}",
    ]


def _uncertainty_line(uncertainty: MeanUncertainty) -> str:
    if uncertainty.status == "available":
        return (
            "- issuer-clustered 95% interval: "
            f"[{_percentage(uncertainty.ci_lower)}, "
            f"{_percentage(uncertainty.ci_upper)}]"
        )
    if uncertainty.status == "insufficient_issuers":
        return "- issuer-clustered 95% interval: unavailable (fewer than 2 issuers)"
    return "- issuer-clustered 95% interval: unavailable (no outcomes)"


def render_forward_report(report: ForwardExperimentReport) -> str:
    """Render explicit, non-blended historical and prospective sections."""

    historical = report.historical
    forward = report.forward
    evaluation = report.evaluation
    lines = [
        f"Forward experiment report: {report.experiment.experiment_id} "
        f"v{report.experiment.version}",
        f"As of: {report.as_of.isoformat()}",
        f"Generated at: {report.generated_at.isoformat()}",
        "Historical and forward samples combined: NO",
        "",
        historical.sample_label,
        f"- status: {historical.status}",
        f"- frozen historical cutoff date: {historical.historical_cutoff_date}",
        f"- outcome basis: {historical.outcome_basis_status}",
        f"- decision contexts: {historical.decision_contexts}",
        f"- context coverage: {_percentage(historical.context_coverage_fraction)}",
        f"- sample size: {historical.sample_size}",
        *_metric_lines(historical.metrics),
        _uncertainty_line(historical.uncertainty),
        "",
        forward.sample_label,
        f"- status: {forward.status}",
        f"- considered decisions: {forward.considered_decisions}",
        f"- eligible decisions: {forward.eligible_decisions}",
        f"- rejected decisions: {forward.rejected_decisions}",
        f"- realized outcomes: {forward.outcome_count}",
        f"- outcome coverage: {_percentage(forward.outcome_coverage_fraction)}",
        f"- sample size: {forward.sample_size}",
    ]
    if forward.rejection_summaries:
        lines.append("- rejection reasons (overlapping counts):")
        lines.extend(
            f"  - {item.code}: {item.decision_count}"
            for item in forward.rejection_summaries
        )
    else:
        lines.append("- rejection reasons: none")
    lines.extend(_metric_lines(forward.metrics))
    if evaluation.criteria:
        criteria_lines = ["- frozen criteria:"]
        criteria_lines.extend(
            "  - "
            f"{item.side}/{item.name}: {item.metric} {item.operator} "
            f"{item.threshold} (observed={item.observed_value}, "
            f"matched={item.matched})"
            for item in evaluation.criteria
        )
    else:
        criteria_lines = []
    lines.extend(
        (
            _uncertainty_line(forward.uncertainty),
            "",
            "FROZEN FORWARD EVALUATION",
            f"- status: {evaluation.status}",
            "- classification final: "
            f"{str(evaluation.classification_is_final).lower()}",
            f"- frozen cohort policy: {evaluation.cohort_policy}",
            f"- cohort decisions: {evaluation.cohort_decisions}",
            f"- cohort eligible decisions: {evaluation.cohort_eligible_decisions}",
            f"- cohort rejected decisions: {evaluation.cohort_rejected_decisions}",
            f"- cohort outcomes: {evaluation.cohort_outcome_count}",
            "- cohort eligible without outcome: "
            f"{evaluation.cohort_eligible_without_outcome}",
            "- post-window decisions excluded from evaluation: "
            f"{evaluation.post_window_decisions_excluded}",
            f"- primary metric: {evaluation.primary_metric}",
            "- primary metric value: "
            + (
                "not available"
                if evaluation.primary_metric_value is None
                else str(evaluation.primary_metric_value)
            ),
            f"- minimum events: {evaluation.minimum_events} "
            f"(met={str(evaluation.minimum_events_met).lower()})",
            f"- minimum issuers: {evaluation.minimum_issuers} "
            f"(met={str(evaluation.minimum_issuers_met).lower()})",
            *criteria_lines,
            f"- detail: {evaluation.detail}",
            "- action: NO_TRADE (reporting only)",
        )
    )
    return "\n".join(lines)
