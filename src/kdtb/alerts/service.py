from __future__ import annotations

from datetime import datetime
from typing import Callable

from kdtb.context import (
    EVENT_TYPE_CATEGORIES,
    HistoricalContextError,
    HistoricalContextService,
)
from kdtb.data.benchmarks import BenchmarkDataError
from kdtb.events.chronology import latest_source_timestamp
from kdtb.live.dart_watcher import EventEnvelope
from kdtb.schemas.alert import (
    AlertHistoricalContext,
    AlertImportantField,
    AlertStrategyDisposition,
    ResearchAlert,
)
from kdtb.schemas.economic_event import EconomicEvent
from kdtb.schemas.significance import EventSignificance
from kdtb.significance import SignificanceEngine

AlertSink = Callable[[ResearchAlert, str], None]
AssessmentTime = Callable[[EventEnvelope], datetime]


def _important_fields(
    significance: EventSignificance,
) -> tuple[AlertImportantField, ...]:
    fields: list[AlertImportantField] = []
    for measurement in significance.measurements:
        if measurement.numerator_krw is not None:
            fields.extend(
                (
                    AlertImportantField(
                        key="contract_value_krw",
                        value=measurement.numerator_krw,
                        unit="KRW",
                    ),
                    AlertImportantField(
                        key="prior_year_revenue_krw",
                        value=measurement.denominator_krw,
                        unit="KRW",
                    ),
                )
            )
        fields.append(
            AlertImportantField(
                key="contract_to_revenue_ratio",
                value=measurement.value,
                unit="ratio",
            )
        )
    return tuple(fields)


def _context_unavailability(event: EconomicEvent) -> AlertHistoricalContext | None:
    if event.event_type not in EVENT_TYPE_CATEGORIES:
        return AlertHistoricalContext(
            status="unavailable",
            unavailable_reason="unsupported_event_type",
            detail=f"No verified Phase 0 category exists for {event.event_type}.",
        )
    if event.market not in {"KOSPI", "KOSDAQ"}:
        return AlertHistoricalContext(
            status="unavailable",
            unavailable_reason="unsupported_market",
            detail=f"No verified benchmark/cost context exists for {event.market}.",
        )
    if event.lineage_status not in {"self_contained", "complete"}:
        return AlertHistoricalContext(
            status="unavailable",
            unavailable_reason="incomplete_lineage",
            detail=(
                "Historical context is withheld because current-event lineage is "
                f"{event.lineage_status}."
            ),
        )
    return None


class ResearchAlertBuilder:
    """Compose verified event intelligence without adding a trading strategy."""

    def __init__(
        self,
        *,
        historical_context_service: HistoricalContextService | None = None,
    ) -> None:
        self.significance_engine = SignificanceEngine()
        self.historical_context_service = (
            historical_context_service or HistoricalContextService()
        )

    def build(
        self,
        envelope: EventEnvelope,
        *,
        assessed_at: datetime | None = None,
    ) -> ResearchAlert:
        cutoff = assessed_at or envelope.normalized_at
        significance = self.significance_engine.assess(
            envelope.event,
            assessed_at=cutoff,
        )
        historical_context = _context_unavailability(envelope.event)
        if historical_context is None:
            try:
                result = self.historical_context_service.query(
                    envelope.event,
                    assessed_at=cutoff,
                )
            except (HistoricalContextError, BenchmarkDataError) as error:
                historical_context = AlertHistoricalContext(
                    status="unavailable",
                    unavailable_reason="context_query_failed",
                    detail=str(error),
                )
            else:
                historical_context = AlertHistoricalContext(
                    status=result.status,
                    result=result,
                )

        return ResearchAlert(
            delivery_id=envelope.delivery_id,
            trigger_receipt_no=envelope.trigger_receipt_no,
            processed_at=envelope.normalized_at,
            assessed_at=cutoff,
            event=envelope.event,
            important_fields=_important_fields(significance),
            missing_important_fields=significance.missing_inputs,
            significance=significance,
            historical_context=historical_context,
            strategy=AlertStrategyDisposition(),
        )


def _comparison_text(status: str, percentile: float | None, count: int) -> str:
    if percentile is not None:
        return f"{percentile:.1f}th percentile (n={count:,})"
    labels = {
        "no_comparable_dataset": "no comparable significance dataset",
        "insufficient_population": "insufficient comparable population",
    }
    return f"unranked — {labels[status]} (n={count:,})"


def render_alert(alert: ResearchAlert) -> str:
    """Render only the validated structured alert; no free-form generation."""

    event = alert.event
    stock = event.issuer.stock_code or "unlisted/unknown"
    lines = [
        "=== KDTB EVENT ALERT ===",
        (
            f"Issuer: {event.issuer.corp_name} "
            f"({stock}, {event.market}; corp {event.issuer.corp_code})"
        ),
        f"Event: {event.event_type} [{event.status}]",
        (
            f"Receipt: {alert.trigger_receipt_no} | Canonical: "
            f"{event.economic_event_id} | Lineage: {event.lineage_status}"
        ),
        f"Assessed at: {alert.assessed_at.isoformat()}",
        "",
        "Economically important fields:",
    ]
    labels = {
        "contract_value_krw": "Contract value",
        "prior_year_revenue_krw": "Prior-year revenue",
        "contract_to_revenue_ratio": "Contract / revenue",
    }
    if alert.important_fields:
        for field in alert.important_fields:
            rendered = (
                f"₩{field.value:,}" if field.unit == "KRW" else f"{field.value:.2%}"
            )
            lines.append(f"- {labels[field.key]}: {rendered}")
    elif alert.significance.status == "unsupported_event_type":
        lines.append("- No verified magnitude extractor for this event type.")
    else:
        missing = ", ".join(alert.missing_important_fields) or "verified inputs"
        lines.append(f"- Unavailable; missing: {missing}.")

    lines.extend(("", "Significance:"))
    if alert.significance.measurements:
        measurement = alert.significance.measurements[0]
        lines.extend(
            (
                f"- Magnitude: {measurement.value:.2%}",
                "- Prior same type/market: "
                + _comparison_text(
                    measurement.historical_comparison.status,
                    measurement.historical_comparison.percentile_rank,
                    measurement.historical_comparison.event_count,
                ),
                "- Issuer history: "
                + _comparison_text(
                    measurement.issuer_history_comparison.status,
                    measurement.issuer_history_comparison.percentile_rank,
                    measurement.issuer_history_comparison.event_count,
                ),
            )
        )
    else:
        lines.append(f"- {alert.significance.status}.")

    lines.extend(("", "Historical context (T+1 close to T+5 close):"))
    context = alert.historical_context
    if context.result is None:
        lines.append(f"- Unavailable [{context.unavailable_reason}]: {context.detail}")
    elif context.result.metrics is None:
        lines.append(
            "- Insufficient history: "
            f"n={context.result.selection.selected_rows:,}; no interval reported."
        )
    else:
        metrics = context.result.metrics
        lines.extend(
            (
                f"- Comparable sample: n={metrics.sample_size:,}, "
                f"issuers={metrics.issuer_count:,}",
                f"- Mean abnormal net: {metrics.mean_abnormal_net_return:+.2%}",
                f"- Median abnormal net: {metrics.median_abnormal_net_return:+.2%}",
                (
                    f"- {metrics.confidence_level:.0%} issuer-clustered CI: "
                    f"[{metrics.ci_lower:+.2%}, {metrics.ci_upper:+.2%}]"
                ),
                f"- Positive fraction: {metrics.positive_fraction:.1%}",
                (
                    f"- Dataset: {context.result.dataset.dataset_id} / "
                    f"{context.result.dataset.dataset_version}; outcomes before "
                    f"{context.result.dataset.outcomes_strictly_before.isoformat()}"
                ),
            )
        )

    lines.extend(
        (
            "",
            "Strategy / execution:",
            "- NO REGISTERED STRATEGY",
            "- NO TRADE",
            "- No active frozen experiment registry exists before M2.1.",
            "",
            "Research context only; no trading recommendation was evaluated.",
        )
    )
    return "\n".join(lines)


def historical_replay_assessment_time(envelope: EventEnvelope) -> datetime:
    """Use only source time for a historical replay's decision cutoff."""

    return latest_source_timestamp(envelope.event)


class ResearchAlertConsumer:
    """Watcher consumer shared by live delivery and historical replay."""

    def __init__(
        self,
        *,
        builder: ResearchAlertBuilder,
        sink: AlertSink,
        consumer_name: str = "research-alert-v1",
        assessment_time: AssessmentTime | None = None,
    ) -> None:
        if not consumer_name.strip():
            raise ValueError("consumer_name must not be empty")
        self.consumer_name = consumer_name
        self.builder = builder
        self.sink = sink
        self.assessment_time = assessment_time

    def consume(self, envelope: EventEnvelope) -> None:
        assessed_at = (
            self.assessment_time(envelope)
            if self.assessment_time is not None
            else envelope.normalized_at
        )
        alert = self.builder.build(envelope, assessed_at=assessed_at)
        self.sink(alert, render_alert(alert))
