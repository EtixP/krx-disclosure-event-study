from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from kdtb.alerts import ResearchAlertBuilder
from kdtb.cli import build_parser
from kdtb.context import HistoricalContextPolicy, HistoricalContextService
from kdtb.experiments import (
    EvaluationPlan,
    ExperimentRegistry,
    ExperimentSpecification,
    ForwardDecisionConsumer,
    ForwardDecisionLedger,
    ForwardExperimentReport,
    ForwardOutcomeLedger,
    ForwardReportError,
    ForwardReportService,
    OutcomeCriterion,
    ProspectiveOutcomeEvaluator,
    render_forward_report,
)
from kdtb.schemas.economic_event import EconomicEvent, EventIssuer
from kdtb.storage.db import init_db, open_readonly_db
from tests.test_historical_context import _fixture_rows, _write_supply_contracts
from tests.test_prospective_outcomes import (
    ManualClock,
    _event,
    _market_observations,
    _observation,
    _save_envelope,
    _session_calendar,
    _specification,
    _time,
)

UTC = timezone.utc
EVALUATION_END = datetime(2026, 12, 31, tzinfo=UTC)
LATE_OUTCOME_TIME = datetime(2027, 1, 2, 10, tzinfo=UTC)
FINAL_REPORT_TIME = datetime(2027, 1, 2, 11, tzinfo=UTC)
LATE_DECISION_TIME = datetime(2027, 1, 3, 10, tzinfo=UTC)
POST_WINDOW_REPORT_TIME = datetime(2027, 1, 3, 11, tzinfo=UTC)


def _report_service(
    conn: sqlite3.Connection,
    *,
    generated_at: datetime = FINAL_REPORT_TIME,
) -> ForwardReportService:
    return ForwardReportService(conn, clock=ManualClock(generated_at))


def _evaluation(*, metric: str = "mean_abnormal_net_return") -> EvaluationPlan:
    return EvaluationPlan(
        evaluation_end=EVALUATION_END,
        primary_metric=metric,
        minimum_events=2,
        minimum_issuers=2,
        success_criteria=(
            OutcomeCriterion(
                name="positive_primary",
                metric=metric,
                operator="gt",
                threshold=0.0,
            ),
        ),
        failure_criteria=(
            OutcomeCriterion(
                name="nonpositive_primary",
                metric=metric,
                operator="lte",
                threshold=0.0,
            ),
        ),
    )


def _report_specification(
    experiment_id: str = "forward-report",
    *,
    evaluation_metric: str = "mean_abnormal_net_return",
) -> ExperimentSpecification:
    base = _specification(experiment_id)
    values = base.model_dump(mode="python")
    values["event_definition"] = base.event_definition.model_copy(
        update={"event_types": ("major_supply_contract",)}
    )
    values["evaluation"] = _evaluation(metric=evaluation_metric)
    return ExperimentSpecification.model_validate(values)


def _report_event(
    receipt_no: str,
    *,
    score: float,
    corp_code: str,
    stock_code: str,
) -> EconomicEvent:
    base = _event(score=score, receipt_no=receipt_no)
    values = base.model_dump(mode="python")
    values["event_type"] = "major_supply_contract"
    values["issuer"] = EventIssuer(
        corp_code=corp_code,
        corp_name=f"forward issuer {corp_code}",
        stock_code=stock_code,
    )
    return EconomicEvent.model_validate(values)


@pytest.fixture
def report_context(tmp_path):
    database = tmp_path / "forward-report.db"
    conn = init_db(database)
    data_dir, _ = _write_supply_contracts(tmp_path, _fixture_rows())
    specification = _report_specification()
    registry_clock = ManualClock(_time(10, 13))
    registry = ExperimentRegistry(conn, clock=registry_clock)
    registry.register(specification)
    registry_clock.value = _time(14)
    registry.activate(specification.experiment_id, specification.version)

    decision_ledger = ForwardDecisionLedger(
        conn,
        registry=registry,
        clock=ManualClock(_time(16, 3)),
    )
    consumer = ForwardDecisionConsumer(
        registry=registry,
        ledger=decision_ledger,
        alert_builder=ResearchAlertBuilder(
            historical_context_service=HistoricalContextService(
                data_dir=data_dir,
                policy=HistoricalContextPolicy(n_resamples=10, random_state=17),
            )
        ),
    )
    events = (
        _report_event(
            "20260916000001",
            score=0.75,
            corp_code="00000001",
            stock_code="005930",
        ),
        _report_event(
            "20260916000002",
            score=0.80,
            corp_code="00000002",
            stock_code="000002",
        ),
        _report_event(
            "20260916000003",
            score=0.10,
            corp_code="00000003",
            stock_code="000003",
        ),
    )
    for event in events:
        consumer.consume(_save_envelope(conn, event))

    decisions = decision_ledger.list_for_experiment(specification.experiment_id)
    eligible = tuple(
        item for item in decisions if item.decision.disposition == "eligible"
    )
    stock, benchmark = _market_observations()
    second_stock = tuple(
        _observation(
            "stock",
            "000002",
            observation.trading_date,
            observation.close * 2,
        )
        for observation in stock
    )
    evaluator = ProspectiveOutcomeEvaluator(clock=ManualClock(_time(23, 9)))
    outcome_clock = ManualClock(_time(23, 10))
    outcome_ledger = ForwardOutcomeLedger(
        conn,
        decision_ledger=decision_ledger,
        clock=outcome_clock,
    )
    for index, (stored, stock_observations) in enumerate(
        zip(eligible, (stock, second_stock), strict=True)
    ):
        if index == 1:
            outcome_clock.value = LATE_OUTCOME_TIME
        outcome_ledger.record(
            evaluator.evaluate(
                stored.decision,
                stock_observations=stock_observations,
                benchmark_observations=benchmark,
                session_calendar=_session_calendar(),
            )
        )
    try:
        yield database, conn, specification, decision_ledger, consumer
    finally:
        conn.close()


def test_report_keeps_historical_and_forward_samples_explicit_and_separate(
    report_context,
):
    _, conn, specification, _, _ = report_context
    service = _report_service(conn)
    report = service.build(
        specification.experiment_id,
        specification.version,
        as_of=FINAL_REPORT_TIME,
    )

    assert report.historical_and_forward_are_combined is False
    assert report.historical.sample_label == "HISTORICAL — NOT FORWARD"
    assert report.historical.status == "available"
    assert report.historical.decision_contexts == 3
    assert report.historical.context_results == 3
    assert report.historical.context_coverage_fraction == 1.0
    assert report.historical.sample_size == 5
    assert report.historical.metrics is not None
    assert report.historical.metrics.sample_size == 5
    assert all(
        item.exit_date < report.historical.historical_cutoff_date
        for item in report.historical.observations
    )

    assert report.forward.sample_label == "FORWARD — PROSPECTIVE ONLY"
    assert report.forward.status == "observed_outcomes_complete"
    assert report.forward.considered_decisions == 3
    assert report.forward.eligible_decisions == 2
    assert report.forward.rejected_decisions == 1
    assert report.forward.outcome_count == 2
    assert report.forward.outcome_coverage_fraction == 1.0
    assert report.forward.sample_size == 2
    assert report.forward.issuer_count == 2
    assert report.forward.rejection_summaries[0].code == ("ELIGIBILITY_RULE_NOT_MET")
    assert report.forward.uncertainty.status == "available"
    assert report.evaluation.status == "success"
    assert report.evaluation.classification_is_final is True
    assert report.generated_at == FINAL_REPORT_TIME
    assert report.authorizes_execution is False
    assert report.trading_recommendation is None

    replayed = ForwardExperimentReport.model_validate_json(report.canonical_json())
    assert replayed == report
    assert replayed.sha256() == report.sha256()
    assert (
        service.build(
            specification.experiment_id,
            specification.version,
            as_of=FINAL_REPORT_TIME,
        ).canonical_json()
        == report.canonical_json()
    )
    rendered = render_forward_report(report)
    assert "Historical and forward samples combined: NO" in rendered
    assert "HISTORICAL — NOT FORWARD" in rendered
    assert "FORWARD — PROSPECTIVE ONLY" in rendered
    assert "ELIGIBILITY_RULE_NOT_MET: 1" in rendered
    assert "action: NO_TRADE" in rendered


def test_report_cutoff_excludes_later_outcomes_and_does_not_impute_them(
    report_context,
):
    _, conn, specification, _, _ = report_context
    report = _report_service(conn).build(
        specification.experiment_id,
        specification.version,
        as_of=_time(20),
    )

    assert report.forward.status == "no_outcomes"
    assert report.forward.sample_size == 0
    assert report.forward.metrics is None
    assert report.forward.uncertainty.status == "no_observations"
    assert report.forward.eligible_without_outcome == 2
    assert report.forward.outcome_coverage_fraction == 0.0
    assert report.evaluation.status == "collecting"
    assert report.evaluation.classification_is_final is False


def test_partial_forward_sample_reports_missing_coverage_and_uncertainty(
    report_context,
):
    _, conn, specification, _, _ = report_context
    report = _report_service(conn).build(
        specification.experiment_id,
        specification.version,
        as_of=_time(24, 9),
    )

    assert report.forward.status == "partial_outcomes"
    assert report.forward.sample_size == 1
    assert report.forward.outcome_coverage_fraction == 0.5
    assert report.forward.eligible_without_outcome == 1
    assert report.forward.eligible_without_outcome_receipt_nos == ("20260916000002",)
    assert report.forward.uncertainty.status == "insufficient_issuers"
    assert report.evaluation.status == "collecting"
    assert report.evaluation.classification_is_final is False


def test_evaluation_waits_for_late_outcomes_before_becoming_final(report_context):
    _, conn, specification, _, _ = report_context
    service = _report_service(conn)

    at_evaluation_end = service.build(
        specification.experiment_id,
        specification.version,
        as_of=specification.evaluation.evaluation_end,
    )
    assert at_evaluation_end.forward.outcome_count == 1
    assert at_evaluation_end.forward.eligible_without_outcome == 1
    assert at_evaluation_end.evaluation.status == "awaiting_outcomes"
    assert at_evaluation_end.evaluation.classification_is_final is False

    after_resolution = service.build(
        specification.experiment_id,
        specification.version,
        as_of=FINAL_REPORT_TIME,
    )
    assert after_resolution.forward.outcome_count == 2
    assert after_resolution.forward.eligible_without_outcome == 0
    assert after_resolution.evaluation.status == "success"
    assert after_resolution.evaluation.classification_is_final is True


def test_final_evaluation_cohort_ignores_but_reports_late_decisions(report_context):
    _, conn, specification, decision_ledger, consumer = report_context
    baseline = _report_service(conn).build(
        specification.experiment_id,
        specification.version,
        as_of=FINAL_REPORT_TIME,
    )
    assert baseline.evaluation.status == "success"
    assert baseline.evaluation.classification_is_final is True

    decision_ledger.clock.value = LATE_DECISION_TIME
    consumer.consume(
        _save_envelope(
            conn,
            _report_event(
                "20260916000004",
                score=0.90,
                corp_code="00000004",
                stock_code="000004",
            ),
        )
    )
    later = _report_service(
        conn,
        generated_at=POST_WINDOW_REPORT_TIME,
    ).build(
        specification.experiment_id,
        specification.version,
        as_of=POST_WINDOW_REPORT_TIME,
    )

    assert later.forward.considered_decisions == 4
    assert later.forward.eligible_without_outcome == 1
    assert later.evaluation.status == baseline.evaluation.status
    assert later.evaluation.classification_is_final is True
    assert later.evaluation.primary_metric_value == (
        baseline.evaluation.primary_metric_value
    )
    assert later.evaluation.criteria == baseline.evaluation.criteria
    assert later.evaluation.cohort_decisions == 3
    assert later.evaluation.cohort_outcome_count == 2
    assert later.evaluation.cohort_eligible_without_outcome == 0
    assert later.evaluation.post_window_decisions_excluded == 1
    assert later.evaluation.post_window_trigger_receipt_nos == ("20260916000004",)


def test_empty_forward_sample_is_reported_as_unavailable_not_zero(tmp_path):
    conn = init_db(tmp_path / "empty-report.db")
    specification = _report_specification("empty-forward-report")
    clock = ManualClock(_time(10, 13))
    registry = ExperimentRegistry(conn, clock=clock)
    registry.register(specification)
    clock.value = _time(14)
    registry.activate(specification.experiment_id, specification.version)
    try:
        report_clock = ManualClock(_time(17, 12))
        service = ForwardReportService(conn, clock=report_clock)
        with pytest.raises(ForwardReportError, match="trusted generation time"):
            service.build(
                specification.experiment_id,
                specification.version,
                as_of=specification.evaluation.evaluation_end,
            )
        report_clock.value = specification.evaluation.evaluation_end
        report = service.build(
            specification.experiment_id,
            specification.version,
            as_of=specification.evaluation.evaluation_end,
        )
    finally:
        conn.close()

    assert report.forward.status == "no_decisions"
    assert report.forward.metrics is None
    assert report.forward.uncertainty.status == "no_observations"
    assert report.forward.outcome_coverage_fraction is None
    assert report.evaluation.status == "insufficient_data"
    assert report.evaluation.classification_is_final is True
    assert report.evaluation.primary_metric_value is None
    assert "not available (no observed outcomes)" in render_forward_report(report)


def test_report_schema_rederives_aggregates_from_embedded_records(report_context):
    _, conn, specification, _, _ = report_context
    report = _report_service(conn).build(
        specification.experiment_id,
        specification.version,
        as_of=specification.evaluation.evaluation_end,
    )
    payload = report.model_dump(mode="python")
    payload["forward"] = report.forward.model_copy(update={"sample_size": 200})

    with pytest.raises(ValidationError, match="forward report section"):
        ForwardExperimentReport.model_validate(payload)

    payload = report.model_dump(mode="python")
    payload["generated_at"] = _time(20)
    with pytest.raises(ValidationError, match="cutoff cannot follow report generation"):
        ForwardExperimentReport.model_validate(payload)


def test_reporting_connection_is_read_only_and_does_not_change_database(
    report_context,
):
    database, conn, specification, _, _ = report_context
    conn.close()
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    readonly = open_readonly_db(database)
    try:
        assert readonly.execute("PRAGMA query_only").fetchone() == (1,)
        report = _report_service(readonly).build(
            specification.experiment_id,
            specification.version,
            as_of=FINAL_REPORT_TIME,
        )
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            readonly.execute(
                "INSERT INTO disclosures (receipt_no) VALUES ('20990101000001')"
            )
    finally:
        readonly.close()

    assert report.forward.sample_size == 2
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


def test_unknown_frozen_metric_fails_explicitly(tmp_path):
    conn = init_db(tmp_path / "unknown-metric.db")
    specification = _report_specification(
        "unknown-report-metric",
        evaluation_metric="unimplemented_metric",
    )
    clock = ManualClock(_time(10, 13))
    registry = ExperimentRegistry(conn, clock=clock)
    registry.register(specification)
    clock.value = _time(14)
    registry.activate(specification.experiment_id, specification.version)
    try:
        with pytest.raises(ForwardReportError, match="unsupported frozen"):
            _report_service(
                conn,
                generated_at=specification.evaluation.evaluation_end,
            ).build(
                specification.experiment_id,
                specification.version,
                as_of=specification.evaluation.evaluation_end,
            )
    finally:
        conn.close()


def test_forward_report_cli_requires_explicit_version_and_utc_cutoff():
    parser = build_parser()
    args = parser.parse_args(
        (
            "forward-report",
            "forward-report",
            "--version",
            "1",
            "--as-of",
            "2026-12-31T00:00:00Z",
            "--json",
        )
    )
    assert args.command == "forward-report"
    assert args.version == 1
    assert args.as_of == datetime(2026, 12, 31, tzinfo=UTC)
    assert args.json is True

    with pytest.raises(SystemExit):
        parser.parse_args(
            (
                "forward-report",
                "forward-report",
                "--version",
                "1",
                "--as-of",
                "2026-12-31T09:00:00+09:00",
            )
        )
