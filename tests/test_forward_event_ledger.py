from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from kdtb.alerts import ResearchAlertBuilder
from kdtb.event_identity import (
    canonical_event_snapshot,
    delivery_id_for_event,
    event_from_canonical_snapshot,
)
from kdtb.experiments import (
    BenchmarkDefinition,
    BenchmarkMapping,
    CostAssumptions,
    EligibilityRule,
    EvaluationPlan,
    EventDefinition,
    ExecutionRule,
    ExperimentRegistry,
    ExperimentSpecification,
    FeatureDefinition,
    ForwardDecision,
    ForwardDecisionConsumer,
    ForwardDecisionLedger,
    ForwardDecisionLedgerError,
    OutcomeCriterion,
    Predicate,
    RuleParameter,
    build_decision_inputs,
    decision_id_for,
)
from kdtb.live import EventEnvelope
from kdtb.live.store import LiveEventStore
from kdtb.schemas.economic_event import (
    EconomicEvent,
    EventIssuer,
    EventSourceProvenance,
)
from kdtb.schemas.forward_decision import decision_rejection_reasons
from kdtb.storage.db import init_db

UTC = timezone.utc


def _time(day: int, hour: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, tzinfo=UTC)


class ManualClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _specification(
    experiment_id: str,
    *,
    minimum_score: float = 0.5,
    feature_path: str = "normalized_fields.score",
) -> ExperimentSpecification:
    return ExperimentSpecification(
        experiment_id=experiment_id,
        version=1,
        name="Frozen custom-event eligibility observation",
        objective="Record every custom event considered without authorizing a trade.",
        created_at=_time(10, 12),
        historical_cutoff=_time(9, 23),
        forward_test_start=_time(15),
        event_definition=EventDefinition(
            event_types=("custom_event",),
            markets=("KOSPI",),
            event_statuses=("active",),
            lineage_statuses=("self_contained",),
            normalization_version="m1.1-v1",
        ),
        features=(
            FeatureDefinition(
                name="score",
                source="economic_event",
                source_version="m1.1-v1",
                field_path=feature_path,
                value_type="number",
                missing_policy="exclude",
            ),
        ),
        benchmark=BenchmarkDefinition(
            benchmark_id="broad_market_price_index",
            source="NAVER_FINANCE_DOMESTIC_INDEX_DAILY",
            source_version="m0.3-v1",
            market_mappings=(BenchmarkMapping(market="KOSPI", symbol="KOSPI"),),
            alignment="exact_trading_date",
            return_type="price_return",
            missing_data_policy="fail",
        ),
        eligibility_rules=(
            EligibilityRule(
                rule_id="minimum_score",
                predicate=Predicate(
                    field_path="score",
                    operator="gte",
                    value=minimum_score,
                ),
                on_missing="exclude",
            ),
        ),
        entry_rule=ExecutionRule(
            rule_id="next_trading_day_close",
            rule_version="m2.1-v1",
            parameters=(RuleParameter(name="session_offset", value=1),),
        ),
        exit_rule=ExecutionRule(
            rule_id="trading_session_close",
            rule_version="m2.1-v1",
            parameters=(RuleParameter(name="holding_sessions", value=4),),
        ),
        costs=CostAssumptions(
            model_id="korean_equity_roundtrip",
            model_version="m0.2-v1",
            commission_per_side=0.00015,
            vat_on_commission=0.1,
            slippage_bps_per_side=5.0,
            tax_policy_id="korean_equity_transaction_tax",
            tax_policy_version="verified_2021_2026",
        ),
        evaluation=EvaluationPlan(
            evaluation_end=datetime(2026, 12, 31, tzinfo=UTC),
            primary_metric="mean_abnormal_net_return",
            minimum_events=10,
            minimum_issuers=5,
            success_criteria=(
                OutcomeCriterion(
                    name="positive_mean",
                    metric="mean_abnormal_net_return",
                    operator="gt",
                    threshold=0.0,
                ),
            ),
            failure_criteria=(
                OutcomeCriterion(
                    name="nonpositive_mean",
                    metric="mean_abnormal_net_return",
                    operator="lte",
                    threshold=0.0,
                ),
            ),
        ),
    )


def _event(
    receipt_no: str = "20260916000001",
    *,
    score: int | float | None = 0.75,
    normalized_fields: dict[str, object] | None = None,
    source_timestamp: datetime | None = None,
) -> EconomicEvent:
    source_timestamp = source_timestamp or _time(16)
    if normalized_fields is None:
        normalized_fields = {} if score is None else {"score": score}
    return EconomicEvent(
        economic_event_id=f"dart:{receipt_no}",
        event_type="custom_event",
        issuer=EventIssuer(
            corp_code="00126380",
            corp_name="forward-test issuer",
            stock_code="005930",
        ),
        market="KOSPI",
        primary_receipt_no=receipt_no,
        original_timestamp=source_timestamp,
        latest_update_timestamp=source_timestamp,
        status="active",
        normalized_fields=normalized_fields,
        source_provenance=(
            EventSourceProvenance(
                receipt_no=receipt_no,
                report_name="prospective custom event",
                receipt_timestamp=source_timestamp,
                source="DART",
                action="original",
                relationship="primary",
                raw_payload_sha256="a" * 64,
            ),
        ),
        lineage_status="self_contained",
    )


def _save_envelope(
    conn: sqlite3.Connection,
    event: EconomicEvent,
    *,
    normalized_at: datetime | None = None,
) -> EventEnvelope:
    normalized_at = normalized_at or _time(16, 1)
    source = event.source_provenance[0]
    with conn:
        conn.execute(
            """
            INSERT INTO disclosures (
                receipt_no, corp_code, corp_name, stock_code, report_name,
                receipt_datetime, market, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source.receipt_no,
                event.issuer.corp_code,
                event.issuer.corp_name,
                event.issuer.stock_code,
                source.report_name,
                source.receipt_timestamp.isoformat(),
                event.market,
                source.source,
            ),
        )
    snapshot = LiveEventStore(conn).save_snapshot(
        trigger_receipt_no=source.receipt_no,
        event=event,
        normalized_at=normalized_at,
    )
    return EventEnvelope(
        delivery_id=delivery_id_for_event(
            trigger_receipt_no=snapshot.trigger_receipt_no,
            event=snapshot.event,
        ),
        trigger_receipt_no=snapshot.trigger_receipt_no,
        normalized_at=snapshot.normalized_at,
        event=snapshot.event,
    )


def _activate(
    registry: ExperimentRegistry,
    clock: ManualClock,
    *specifications: ExperimentSpecification,
) -> None:
    for specification in specifications:
        registry.register(specification)
    clock.value = _time(14)
    for specification in specifications:
        registry.activate(specification.experiment_id, specification.version)


@pytest.fixture
def ledger_context(tmp_path):
    database = tmp_path / "forward-decisions.db"
    conn = init_db(database)
    registry_clock = ManualClock(_time(10, 13))
    ledger_clock = ManualClock(_time(16, 2))
    registry = ExperimentRegistry(conn, clock=registry_clock)
    ledger = ForwardDecisionLedger(
        conn,
        registry=registry,
        clock=ledger_clock,
    )
    try:
        yield database, conn, registry, registry_clock, ledger, ledger_clock
    finally:
        conn.close()


def test_consumer_logs_every_active_experiment_with_inputs_and_rejection(
    ledger_context,
):
    _, conn, registry, registry_clock, ledger, _ = ledger_context
    eligible_spec = _specification("eligible-forward", minimum_score=0.5)
    rejected_spec = _specification("rejected-forward", minimum_score=0.9)
    _activate(registry, registry_clock, eligible_spec, rejected_spec)
    envelope = _save_envelope(conn, _event())
    consumer = ForwardDecisionConsumer(
        registry=registry,
        ledger=ledger,
        alert_builder=ResearchAlertBuilder(),
    )

    consumer.consume(envelope)

    stored = ledger.list_for_event(envelope.trigger_receipt_no)
    assert tuple(item.decision.experiment.experiment_id for item in stored) == (
        "eligible-forward",
        "rejected-forward",
    )
    eligible, rejected = (item.decision for item in stored)
    assert eligible.disposition == "eligible"
    assert eligible.rejection_reasons == ()
    assert eligible.experiment.version == 1
    assert eligible.experiment_sha256 == eligible_spec.sha256()
    assert eligible.inputs.input_schema_version == "m2.2-v1"
    assert eligible.inputs.event_sha256 == canonical_event_snapshot(envelope.event)[1]
    assert eligible.inputs.normalized_at == _time(16, 1)
    assert eligible.inputs.features[0].value == 0.75
    assert eligible.inputs.features[0].actual_source_version == "m1.1-v1"
    assert eligible.inputs.historical_context.unavailable_reason == (
        "unsupported_event_type"
    )
    assert eligible.authorizes_execution is False
    assert eligible.is_outcome is False
    assert rejected.disposition == "rejected"
    assert tuple(reason.code for reason in rejected.rejection_reasons) == (
        "ELIGIBILITY_RULE_NOT_MET",
    )
    assert rejected.rejection_reasons[0].rule_id == "minimum_score"


def test_consumer_logs_exact_integer_too_large_for_float(ledger_context):
    _, conn, registry, registry_clock, ledger, _ = ledger_context
    specification = _specification("exact-integer-forward")
    _activate(registry, registry_clock, specification)
    exact_score = 10**1000
    envelope = _save_envelope(conn, _event(score=exact_score))

    ForwardDecisionConsumer(
        registry=registry,
        ledger=ledger,
        alert_builder=ResearchAlertBuilder(),
    ).consume(envelope)

    stored = ledger.list_for_event(envelope.trigger_receipt_no)
    assert len(stored) == 1
    decision = stored[0].decision
    assert decision.disposition == "eligible"
    assert decision.inputs.features[0].status == "available"
    assert decision.inputs.features[0].value == exact_score
    assert isinstance(decision.inputs.features[0].value, int)
    assert ForwardDecision.model_validate_json(decision.canonical_json()) == decision


@pytest.mark.parametrize(
    ("receipt_no", "invalid_score", "expected_kind"),
    (
        ("20260916000002", float("nan"), "nan"),
        ("20260916000003", float("inf"), "positive_infinity"),
        ("20260916000004", float("-inf"), "negative_infinity"),
    ),
)
def test_consumer_logs_non_finite_source_value_as_rejected(
    ledger_context,
    receipt_no,
    invalid_score,
    expected_kind,
):
    _, conn, registry, registry_clock, ledger, _ = ledger_context
    specification = _specification("non-finite-forward")
    _activate(registry, registry_clock, specification)
    envelope = _save_envelope(conn, _event(receipt_no, score=invalid_score))

    consumer = ForwardDecisionConsumer(
        registry=registry,
        ledger=ledger,
        alert_builder=ResearchAlertBuilder(),
    )
    consumer.consume(envelope)

    stored = ledger.list_for_event(envelope.trigger_receipt_no)
    assert len(stored) == 1
    decision = stored[0].decision
    assert decision.disposition == "rejected"
    assert decision.inputs.features[0].status == "type_mismatch"
    assert {reason.code for reason in decision.rejection_reasons} == {
        "ELIGIBILITY_INPUT_MISSING",
        "FEATURE_TYPE_MISMATCH",
    }
    source_value = decision.inputs.event.normalized_fields["score"]
    if math.isnan(invalid_score):
        assert math.isnan(source_value)
    else:
        assert source_value == invalid_score
    event_json, event_sha256 = canonical_event_snapshot(envelope.event)
    assert decision.inputs.event_snapshot_json == event_json
    assert '"score":null' in decision.inputs.event_snapshot_json
    extended = json.loads(event_json)["__kdtb_canonical_event_snapshot__"]
    assert extended["encoding"] == "m1.2-non-finite-v1"
    assert extended["non_finite_values"] == [
        {
            "kind": expected_kind,
            "path": ["normalized_fields", "score"],
        }
    ]
    assert canonical_event_snapshot(decision.inputs.event) == (
        event_json,
        event_sha256,
    )
    assert decision.inputs.event_sha256 == event_sha256
    decision_json = decision.canonical_json()

    def reject_non_finite_constant(value):
        raise AssertionError(f"outer decision JSON contains {value}")

    json.loads(decision_json, parse_constant=reject_non_finite_constant)
    assert ForwardDecision.model_validate_json(decision_json) == decision

    reloaded = LiveEventStore(conn).get_snapshot(receipt_no)
    assert reloaded is not None
    reloaded_score = reloaded.event.normalized_fields["score"]
    if math.isnan(invalid_score):
        assert math.isnan(reloaded_score)
    else:
        assert reloaded_score == invalid_score
    retried_envelope = EventEnvelope(
        delivery_id=delivery_id_for_event(
            trigger_receipt_no=receipt_no,
            event=reloaded.event,
        ),
        trigger_receipt_no=receipt_no,
        normalized_at=reloaded.normalized_at,
        event=reloaded.event,
    )
    assert retried_envelope.delivery_id == envelope.delivery_id
    consumer.consume(retried_envelope)
    assert ledger.list_for_event(receipt_no) == stored


@pytest.mark.parametrize("invalid_score", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize(
    ("operator", "predicate_value"),
    (
        ("eq", 0.5),
        ("ne", 0.5),
        ("in", (0.5,)),
        ("not_in", (0.5,)),
    ),
)
def test_raw_numeric_predicates_reject_non_finite_values(
    ledger_context,
    invalid_score,
    operator,
    predicate_value,
):
    _, conn, registry, registry_clock, ledger, _ = ledger_context
    payload = _specification("raw-non-finite-predicate").model_dump(mode="python")
    payload["eligibility_rules"] = (
        EligibilityRule(
            rule_id="raw_score_rule",
            predicate=Predicate(
                field_path="economic_event.normalized_fields.raw_score",
                operator=operator,
                value=predicate_value,
            ),
            on_missing="exclude",
        ),
    )
    specification = ExperimentSpecification.model_validate(payload)
    _activate(registry, registry_clock, specification)
    envelope = _save_envelope(
        conn,
        _event(
            normalized_fields={"raw_score": invalid_score, "score": 0.75},
        ),
    )

    ForwardDecisionConsumer(
        registry=registry,
        ledger=ledger,
        alert_builder=ResearchAlertBuilder(),
    ).consume(envelope)

    decision = ledger.list_for_event(envelope.trigger_receipt_no)[0].decision
    assert decision.inputs.features[0].status == "available"
    assert decision.disposition == "rejected"
    assert tuple(reason.code for reason in decision.rejection_reasons) == (
        "ELIGIBILITY_PREDICATE_INVALID",
    )


def test_non_finite_snapshot_cannot_be_forged_over_genuine_null(ledger_context):
    _, conn, registry, registry_clock, ledger, _ = ledger_context
    specification = _specification("source-bound-non-finite")
    _activate(registry, registry_clock, specification)
    genuine_null = _event(normalized_fields={"score": None})
    envelope = _save_envelope(conn, genuine_null)
    consumer = ForwardDecisionConsumer(
        registry=registry,
        ledger=ledger,
        alert_builder=ResearchAlertBuilder(),
    )
    consumer.consume(envelope)
    original = ledger.list_for_event(envelope.trigger_receipt_no)

    forged_event = _event(
        envelope.trigger_receipt_no,
        score=float("nan"),
    )
    null_json, null_sha256 = canonical_event_snapshot(genuine_null)
    forged_json, forged_sha256 = canonical_event_snapshot(forged_event)
    assert null_json != forged_json
    assert null_sha256 != forged_sha256
    forged_envelope = EventEnvelope(
        delivery_id=delivery_id_for_event(
            trigger_receipt_no=envelope.trigger_receipt_no,
            event=forged_event,
        ),
        trigger_receipt_no=envelope.trigger_receipt_no,
        normalized_at=envelope.normalized_at,
        event=forged_event,
    )

    with pytest.raises(ForwardDecisionLedgerError, match="stored canonical event"):
        consumer.consume(forged_envelope)
    assert ledger.list_for_event(envelope.trigger_receipt_no) == original


def test_canonical_snapshot_distinguishes_null_and_every_non_finite_kind():
    values = (None, float("nan"), float("inf"), float("-inf"))
    events = tuple(_event(normalized_fields={"score": value}) for value in values)
    snapshots = tuple(canonical_event_snapshot(event) for event in events)

    assert len({payload for payload, _ in snapshots}) == len(values)
    assert len({digest for _, digest in snapshots}) == len(values)
    assert snapshots[0][0] == json.dumps(
        events[0].model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    for expected, (payload, _) in zip(values, snapshots, strict=True):
        restored = event_from_canonical_snapshot(payload).normalized_fields["score"]
        if isinstance(expected, float) and math.isnan(expected):
            assert math.isnan(restored)
        else:
            assert restored == expected


@pytest.mark.parametrize(
    "nested_mapping",
    (
        {True: float("nan")},
        {1: float("nan"), "1": float("inf")},
    ),
    ids=("boolean-key-normalization", "integer-string-key-collision"),
)
def test_event_schema_rejects_lossy_nested_mapping_keys(nested_mapping):
    with pytest.raises(
        ValidationError,
        match="normalized field mapping keys must be strings",
    ):
        _event(normalized_fields={"nested": nested_mapping, "score": 0.75})


@pytest.mark.parametrize(
    "nested_mapping",
    (
        {True: float("nan")},
        {1: float("nan"), "1": float("inf")},
    ),
    ids=("boolean-key-normalization", "integer-string-key-collision"),
)
def test_snapshot_rejects_bypassed_lossy_mapping_before_persistence(
    ledger_context,
    nested_mapping,
):
    _, conn, registry, registry_clock, ledger, _ = ledger_context
    specification = _specification("nested-mapping-boundary")
    _activate(registry, registry_clock, specification)
    event = _event(normalized_fields={"score": 0.75}).model_copy(
        update={
            "normalized_fields": {"nested": nested_mapping, "score": 0.75},
        },
    )

    with pytest.raises(ValueError, match="cannot losslessly round-trip"):
        _save_envelope(conn, event)

    assert (
        conn.execute("SELECT COUNT(*) FROM canonical_event_snapshots").fetchone()[0]
        == 0
    )
    assert ledger.list_for_event(event.primary_receipt_no) == ()


def test_event_identity_imports_in_a_fresh_interpreter():
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from kdtb.event_identity import canonical_event_snapshot",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_missing_feature_is_logged_with_fail_closed_reasons(ledger_context):
    _, conn, registry, registry_clock, ledger, _ = ledger_context
    specification = _specification("missing-feature")
    _activate(registry, registry_clock, specification)
    envelope = _save_envelope(conn, _event(score=None))
    consumer = ForwardDecisionConsumer(
        registry=registry,
        ledger=ledger,
        alert_builder=ResearchAlertBuilder(),
    )

    consumer.consume(envelope)

    decision = ledger.list_for_event(envelope.trigger_receipt_no)[0].decision
    assert decision.disposition == "rejected"
    assert decision.inputs.features[0].status == "missing"
    assert tuple(reason.code for reason in decision.rejection_reasons) == (
        "ELIGIBILITY_INPUT_MISSING",
        "FEATURE_MISSING_EXCLUDED",
    )


def test_decision_is_idempotent_append_only_and_survives_reopen(ledger_context):
    database, conn, registry, registry_clock, ledger, ledger_clock = ledger_context
    specification = _specification("durable-forward")
    _activate(registry, registry_clock, specification)
    envelope = _save_envelope(conn, _event())
    consumer = ForwardDecisionConsumer(
        registry=registry,
        ledger=ledger,
        alert_builder=ResearchAlertBuilder(),
    )
    consumer.consume(envelope)
    original = ledger.list_for_event(envelope.trigger_receipt_no)[0]

    ledger_clock.value = datetime(2027, 1, 2, tzinfo=UTC)
    consumer.consume(envelope)
    repeated = ledger.list_for_event(envelope.trigger_receipt_no)

    assert repeated == (original,)
    assert repeated[0].recorded_at == _time(16, 2)
    with pytest.raises(sqlite3.IntegrityError, match="decisions are immutable"):
        with conn:
            conn.execute("UPDATE forward_event_decisions SET disposition = 'rejected'")
    with pytest.raises(sqlite3.IntegrityError, match="decisions are immutable"):
        with conn:
            conn.execute("DELETE FROM forward_event_decisions")
    assert conn.execute("PRAGMA recursive_triggers").fetchone() == (1,)
    replacement_recorded_at = original.recorded_at + timedelta(days=30)
    with pytest.raises(sqlite3.IntegrityError, match="decisions are immutable"):
        with conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO forward_event_decisions (
                    decision_id, experiment_id, experiment_version,
                    experiment_sha256, experiment_activated_at,
                    trigger_receipt_no, event_sha256, decision_input_sha256,
                    disposition, rejection_reasons_json, decision_sha256,
                    decision_json, decided_at, recorded_at
                )
                SELECT decision_id, experiment_id, experiment_version,
                       experiment_sha256, experiment_activated_at,
                       trigger_receipt_no, event_sha256, decision_input_sha256,
                       disposition, rejection_reasons_json, decision_sha256,
                       decision_json, decided_at, ?
                FROM forward_event_decisions
                WHERE decision_id = ?
                """,
                (
                    replacement_recorded_at.isoformat(),
                    original.decision.decision_id,
                ),
            )
    assert ledger.list_for_event(envelope.trigger_receipt_no) == (original,)

    conn.close()
    reopened = init_db(database)
    try:
        replayed = ForwardDecisionLedger(reopened).list_for_event(
            envelope.trigger_receipt_no
        )
        assert replayed == (original,)
    finally:
        reopened.close()


def test_rehashed_feature_tampering_fails_source_reconciliation(ledger_context):
    _, conn, registry, registry_clock, ledger, _ = ledger_context
    specification = _specification("tamper-forward")
    _activate(registry, registry_clock, specification)
    envelope = _save_envelope(conn, _event())
    consumer = ForwardDecisionConsumer(
        registry=registry,
        ledger=ledger,
        alert_builder=ResearchAlertBuilder(),
    )
    consumer.consume(envelope)
    decision = ledger.list_for_event(envelope.trigger_receipt_no)[0].decision
    payload = decision.model_dump(mode="json")
    payload["inputs"]["features"][0]["value"] = 0.1
    input_json = json.dumps(
        payload["inputs"],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    payload["decision_input_sha256"] = hashlib.sha256(
        input_json.encode("utf-8")
    ).hexdigest()
    payload["decision_id"] = decision_id_for(
        experiment_sha256=payload["experiment_sha256"],
        decision_input_sha256=payload["decision_input_sha256"],
    )

    with pytest.raises(ValidationError, match="feature snapshots"):
        ForwardDecision.model_validate_json(json.dumps(payload))


def test_pre_forward_source_is_logged_but_future_source_is_never_used(
    ledger_context,
):
    _, conn, registry, registry_clock, ledger, _ = ledger_context
    specification = _specification("chronology-forward")
    _activate(registry, registry_clock, specification)
    consumer = ForwardDecisionConsumer(
        registry=registry,
        ledger=ledger,
        alert_builder=ResearchAlertBuilder(),
    )
    stale = _save_envelope(
        conn,
        _event("20260914000001", source_timestamp=_time(14)),
    )

    consumer.consume(stale)

    stale_decision = ledger.list_for_event(stale.trigger_receipt_no)[0].decision
    assert stale_decision.disposition == "rejected"
    assert "PRE_FORWARD_SOURCE" in {
        reason.code for reason in stale_decision.rejection_reasons
    }

    future = _save_envelope(
        conn,
        _event("20260917000001", source_timestamp=_time(17)),
    )
    with pytest.raises(ValueError, match="before its latest source"):
        consumer.consume(future)
    assert ledger.list_for_event(future.trigger_receipt_no) == ()


def test_only_active_experiments_are_considered_and_no_outcome_columns_exist(
    ledger_context,
):
    _, conn, registry, registry_clock, ledger, _ = ledger_context
    active = _specification("active-forward")
    inactive = _specification("inactive-forward")
    registry.register(active)
    registry.register(inactive)
    registry_clock.value = _time(14)
    registry.activate(active.experiment_id, 1)
    envelope = _save_envelope(conn, _event())
    ForwardDecisionConsumer(
        registry=registry,
        ledger=ledger,
        alert_builder=ResearchAlertBuilder(),
    ).consume(envelope)

    decisions = ledger.list_for_event(envelope.trigger_receipt_no)
    assert tuple(item.decision.experiment.experiment_id for item in decisions) == (
        "active-forward",
    )
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(forward_event_decisions)")
    }
    assert not {"outcome", "return", "pnl", "fill"} & columns


def test_database_rejects_decision_before_matching_activation(ledger_context):
    _, conn, registry, registry_clock, ledger, _ = ledger_context
    specification = _specification("orphan-decision")
    registered = registry.register(specification)
    envelope = _save_envelope(conn, _event())
    inputs = build_decision_inputs(
        envelope=envelope,
        registered=registered,
        alert_builder=ResearchAlertBuilder(),
    )
    input_sha256 = inputs.sha256()
    reasons = decision_rejection_reasons(specification, inputs)
    decision = ForwardDecision(
        decision_id=decision_id_for(
            experiment_sha256=specification.sha256(),
            decision_input_sha256=input_sha256,
        ),
        experiment=specification,
        experiment_sha256=specification.sha256(),
        experiment_activated_at=_time(14),
        inputs=inputs,
        decision_input_sha256=input_sha256,
        disposition="rejected" if reasons else "eligible",
        rejection_reasons=reasons,
    )
    reasons_json = json.dumps(
        [reason.model_dump(mode="json") for reason in reasons],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        with conn:
            conn.execute(
                """
                INSERT INTO forward_event_decisions (
                    decision_id, experiment_id, experiment_version,
                    experiment_sha256, experiment_activated_at,
                    trigger_receipt_no, event_sha256, decision_input_sha256,
                    disposition, rejection_reasons_json, decision_sha256,
                    decision_json, decided_at, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision.decision_id,
                    specification.experiment_id,
                    specification.version,
                    specification.sha256(),
                    _time(14).isoformat(),
                    inputs.trigger_receipt_no,
                    inputs.event_sha256,
                    input_sha256,
                    decision.disposition,
                    reasons_json,
                    decision.sha256(),
                    decision.canonical_json(),
                    inputs.assessed_at.isoformat(),
                    _time(16, 2).isoformat(),
                ),
            )

    registry_clock.value = _time(14)
    registry.activate(specification.experiment_id, 1)
    assert ledger.list_for_event(envelope.trigger_receipt_no) == ()


def test_timestamp_feature_round_trips_as_versioned_canonical_input(ledger_context):
    _, conn, registry, registry_clock, ledger, _ = ledger_context
    payload = _specification("timestamp-forward").model_dump(mode="python")
    payload["features"] = (
        FeatureDefinition(
            name="event_timestamp",
            source="economic_event",
            source_version="m1.1-v1",
            field_path="latest_update_timestamp",
            value_type="timestamp",
            missing_policy="fail",
        ),
    )
    payload["eligibility_rules"] = ()
    specification = ExperimentSpecification.model_validate(payload)
    _activate(registry, registry_clock, specification)
    envelope = _save_envelope(conn, _event())

    ForwardDecisionConsumer(
        registry=registry,
        ledger=ledger,
        alert_builder=ResearchAlertBuilder(),
    ).consume(envelope)

    decision = ledger.list_for_event(envelope.trigger_receipt_no)[0].decision
    feature = decision.inputs.features[0]
    assert feature.status == "available"
    assert feature.value == _time(16).isoformat()
    assert ForwardDecision.model_validate_json(decision.canonical_json()) == decision
