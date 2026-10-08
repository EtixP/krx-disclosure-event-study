from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from kdtb.alerts import ResearchAlertBuilder
from kdtb.data.krx_calendar import (
    KRX_HOLIDAY_PAYLOAD_2026_UTF8,
    KRX_HOLIDAY_SOURCE,
    KRX_HOLIDAY_SOURCE_VERSION,
    KRX_HOLIDAY_SOURCE_YEAR,
    krx_holiday_payload_sha256,
)
from kdtb.data.krx_session_hours import (
    KRX_EXCEPTION_EVIDENCE_SHA256,
    KRX_EXCEPTION_POLICY_ID,
    KRX_EXCEPTION_POLICY_VERSION,
    KRX_REGULAR_HOURS_PAYLOAD_UTF8,
    KRX_REGULAR_HOURS_SOURCE,
    KRX_REGULAR_HOURS_SOURCE_VERSION,
    session_hours_payload_sha256,
)
from kdtb.event_identity import delivery_id_for_event
from kdtb.experiments import (
    BenchmarkDefinition,
    BenchmarkMapping,
    CostAssumptions,
    DailyCloseObservation,
    EligibilityRule,
    EvaluationPlan,
    EventDefinition,
    ExecutionRule,
    ExperimentRegistry,
    ExperimentSpecification,
    FeatureDefinition,
    ForwardDecisionConsumer,
    ForwardDecisionLedger,
    ForwardOutcome,
    ForwardOutcomeError,
    ForwardOutcomeLedger,
    OutcomeCriterion,
    Predicate,
    ProspectiveOutcomeEvaluator,
    RuleParameter,
    TradingSession,
    TradingSessionCalendar,
    daily_close_observation_sha256,
    trading_session_calendar_sha256,
)
from kdtb.live import EventEnvelope
from kdtb.live.store import LiveEventStore
from kdtb.schemas.economic_event import (
    EconomicEvent,
    EventIssuer,
    EventSourceProvenance,
)
from kdtb.schemas.forward_outcome import frozen_roundtrip_cost_fraction
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
    experiment_id: str = "prospective-outcome",
    *,
    minimum_score: float = 0.5,
    entry_rule: ExecutionRule | None = None,
) -> ExperimentSpecification:
    return ExperimentSpecification(
        experiment_id=experiment_id,
        version=1,
        name="Frozen prospective daily-close outcome",
        objective="Evaluate a frozen decision without rewriting it.",
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
                field_path="normalized_fields.score",
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
        entry_rule=entry_rule
        or ExecutionRule(
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
    *,
    score: float = 0.75,
    receipt_no: str = "20260916000001",
    source_timestamp: datetime | None = None,
) -> EconomicEvent:
    source_timestamp = source_timestamp or _time(16)
    return EconomicEvent(
        economic_event_id=f"dart:{receipt_no}",
        event_type="custom_event",
        issuer=EventIssuer(
            corp_code="00126380",
            corp_name="prospective issuer",
            stock_code="005930",
        ),
        market="KOSPI",
        primary_receipt_no=receipt_no,
        original_timestamp=source_timestamp,
        latest_update_timestamp=source_timestamp,
        status="active",
        normalized_fields={"score": score},
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


def _save_envelope(conn: sqlite3.Connection, event: EconomicEvent) -> EventEnvelope:
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
        normalized_at=source.receipt_timestamp.astimezone(UTC) + timedelta(hours=1),
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


def _record_decision(
    conn: sqlite3.Connection,
    *,
    specification: ExperimentSpecification | None = None,
    score: float = 0.75,
    event: EconomicEvent | None = None,
):
    specification = specification or _specification()
    registry_clock = ManualClock(_time(10, 13))
    registry = ExperimentRegistry(conn, clock=registry_clock)
    registry.register(specification)
    registry_clock.value = _time(14)
    registry.activate(specification.experiment_id, specification.version)
    envelope = _save_envelope(conn, event or _event(score=score))
    decision_ledger = ForwardDecisionLedger(
        conn,
        registry=registry,
        clock=ManualClock(envelope.normalized_at + timedelta(hours=1)),
    )
    ForwardDecisionConsumer(
        registry=registry,
        ledger=decision_ledger,
        alert_builder=ResearchAlertBuilder(),
    ).consume(envelope)
    return (
        decision_ledger.list_for_event(envelope.trigger_receipt_no)[0],
        decision_ledger,
    )


def _observation(
    instrument_type: str,
    symbol: str,
    trading_date: date,
    close: float,
) -> DailyCloseObservation:
    if instrument_type == "benchmark":
        source = "NAVER_FINANCE_DOMESTIC_INDEX_DAILY"
        source_version = "m0.3-v1"
    else:
        source = "TEST_CAPTURED_DAILY_CLOSE"
        source_version = "fixture-v1"
    available_at = datetime.combine(trading_date, datetime.min.time(), tzinfo=UTC)
    available_at += timedelta(hours=7)
    captured_at = available_at + timedelta(minutes=5)
    return DailyCloseObservation(
        instrument_type=instrument_type,
        symbol=symbol,
        market="KOSPI",
        trading_date=trading_date,
        close=close,
        source=source,
        source_version=source_version,
        available_at=available_at,
        captured_at=captured_at,
        observation_sha256=daily_close_observation_sha256(
            instrument_type=instrument_type,
            symbol=symbol,
            market="KOSPI",
            trading_date=trading_date,
            close=close,
            source=source,
            source_version=source_version,
            available_at=available_at,
            captured_at=captured_at,
        ),
    )


def _market_observations():
    stock_dates = (
        date(2026, 9, 16),
        date(2026, 9, 17),
        date(2026, 9, 18),
        date(2026, 9, 21),
        date(2026, 9, 22),
        date(2026, 9, 23),
        date(2026, 9, 24),
        date(2026, 9, 25),
        date(2026, 9, 28),
        date(2026, 9, 29),
    )
    stock_closes = (
        100.0,
        110.0,
        112.0,
        115.0,
        118.0,
        121.0,
        122.0,
        123.0,
        124.0,
        125.0,
    )
    stock = tuple(
        _observation("stock", "005930", observed, close)
        for observed, close in zip(stock_dates, stock_closes, strict=True)
    )
    benchmark_closes = (
        995.0,
        1000.0,
        1010.0,
        1020.0,
        1035.0,
        1050.0,
        1060.0,
        1070.0,
        1080.0,
        1090.0,
    )
    benchmark = tuple(
        _observation("benchmark", "KOSPI", observed, close)
        for observed, close in zip(stock_dates, benchmark_closes, strict=True)
    )
    return stock, benchmark


def _session_calendar(
    session_dates: tuple[date, ...] | None = None,
    *,
    window_start: date | None = None,
    window_end: date | None = None,
    closed_weekdays: tuple[date, ...] = (),
) -> TradingSessionCalendar:
    session_dates = session_dates or (
        date(2026, 9, 16),
        date(2026, 9, 17),
        date(2026, 9, 18),
        date(2026, 9, 21),
        date(2026, 9, 22),
        date(2026, 9, 23),
    )
    sessions = tuple(
        TradingSession(
            trading_date=trading_date,
            close_at=datetime.combine(
                trading_date,
                datetime.min.time(),
                tzinfo=UTC,
            )
            + timedelta(hours=6, minutes=30),
        )
        for trading_date in session_dates
    )
    captured_at = _time(15)
    values = {
        "market": "KOSPI",
        "symbol": "KOSPI",
        "source": KRX_HOLIDAY_SOURCE,
        "source_version": KRX_HOLIDAY_SOURCE_VERSION,
        "source_year": KRX_HOLIDAY_SOURCE_YEAR,
        "source_payload_utf8": KRX_HOLIDAY_PAYLOAD_2026_UTF8,
        "source_payload_sha256": krx_holiday_payload_sha256(
            KRX_HOLIDAY_PAYLOAD_2026_UTF8
        ),
        "session_hours_source": KRX_REGULAR_HOURS_SOURCE,
        "session_hours_source_version": KRX_REGULAR_HOURS_SOURCE_VERSION,
        "session_hours_source_payload_utf8": KRX_REGULAR_HOURS_PAYLOAD_UTF8,
        "session_hours_source_payload_sha256": session_hours_payload_sha256(
            KRX_REGULAR_HOURS_PAYLOAD_UTF8
        ),
        "exception_policy_id": KRX_EXCEPTION_POLICY_ID,
        "exception_policy_version": KRX_EXCEPTION_POLICY_VERSION,
        "exception_evidence_sha256": KRX_EXCEPTION_EVIDENCE_SHA256,
        "window_start": window_start or session_dates[0],
        "window_end": window_end or session_dates[-1],
        "sessions": sessions,
        "closed_weekdays": closed_weekdays,
        "captured_at": captured_at,
    }
    return TradingSessionCalendar(
        **values,
        calendar_sha256=trading_session_calendar_sha256(**values),
    )


@pytest.fixture
def outcome_context(tmp_path):
    database = tmp_path / "forward-outcomes.db"
    conn = init_db(database)
    stored_decision, decision_ledger = _record_decision(conn)
    evaluator_clock = ManualClock(_time(23, 9))
    outcome_ledger = ForwardOutcomeLedger(
        conn,
        decision_ledger=decision_ledger,
        clock=ManualClock(_time(23, 10)),
    )
    try:
        yield (
            database,
            conn,
            stored_decision,
            evaluator_clock,
            outcome_ledger,
        )
    finally:
        conn.close()


def _evaluate(stored_decision, evaluator_clock):
    stock, benchmark = _market_observations()
    return ProspectiveOutcomeEvaluator(clock=evaluator_clock).evaluate(
        stored_decision.decision,
        stock_observations=stock,
        benchmark_observations=benchmark,
        session_calendar=_session_calendar(),
    )


def test_evaluator_uses_frozen_rules_exact_dates_benchmark_and_costs(outcome_context):
    _, _, stored_decision, evaluator_clock, _ = outcome_context
    outcome = _evaluate(stored_decision, evaluator_clock)
    specification = stored_decision.decision.experiment
    expected_cost = frozen_roundtrip_cost_fraction(
        specification.costs,
        buy_date=date(2026, 9, 17),
        sell_date=date(2026, 9, 23),
        market="KOSPI",
    )

    assert outcome.stock_entry.trading_date == date(2026, 9, 17)
    assert outcome.stock_exit.trading_date == date(2026, 9, 23)
    assert outcome.benchmark_entry.trading_date == outcome.stock_entry.trading_date
    assert outcome.benchmark_exit.trading_date == outcome.stock_exit.trading_date
    assert outcome.stock_gross_return == 121.0 / 110.0 - 1.0
    assert outcome.benchmark_gross_return == 1050.0 / 1000.0 - 1.0
    assert outcome.roundtrip_cost_fraction == expected_cost
    assert outcome.raw_net_return == outcome.stock_gross_return - expected_cost
    assert outcome.abnormal_net_return == (
        outcome.stock_gross_return - outcome.benchmark_gross_return - expected_cost
    )
    assert outcome.entry_rule == specification.entry_rule
    assert outcome.exit_rule == specification.exit_rule
    assert outcome.benchmark == specification.benchmark
    assert outcome.costs == specification.costs
    assert outcome.sample_kind == "prospective_forward"
    assert outcome.is_decision is False
    assert outcome.is_outcome is True
    assert outcome.authorizes_execution is False

    evaluator_clock.value = _time(23, 11)
    assert _evaluate(stored_decision, evaluator_clock) == outcome


def test_outcome_storage_is_separate_idempotent_immutable_and_durable(outcome_context):
    database, conn, stored_decision, evaluator_clock, outcome_ledger = outcome_context
    decision_before = conn.execute(
        "SELECT decision_json, recorded_at FROM forward_event_decisions"
    ).fetchone()
    outcome = _evaluate(stored_decision, evaluator_clock)

    first = outcome_ledger.record(outcome)
    repeated = outcome_ledger.record(outcome)

    assert repeated == first
    assert conn.execute("SELECT COUNT(*) FROM forward_event_decisions").fetchone() == (
        1,
    )
    assert conn.execute("SELECT COUNT(*) FROM forward_event_outcomes").fetchone() == (
        1,
    )
    assert (
        conn.execute(
            "SELECT decision_json, recorded_at FROM forward_event_decisions"
        ).fetchone()
        == decision_before
    )
    decision_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(forward_event_decisions)")
    }
    assert not {
        "stock_gross_return",
        "abnormal_net_return",
        "outcome_json",
    }.intersection(decision_columns)

    with pytest.raises(sqlite3.IntegrityError, match="outcomes are immutable"):
        with conn:
            conn.execute("UPDATE forward_event_outcomes SET recorded_at = '2099-01-01'")
    with pytest.raises(sqlite3.IntegrityError, match="outcomes are immutable"):
        with conn:
            conn.execute("DELETE FROM forward_event_outcomes")
    with pytest.raises(sqlite3.IntegrityError, match="outcomes are immutable"):
        with conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO forward_event_outcomes
                SELECT outcome_id, decision_id, decision_sha256,
                       experiment_id, experiment_version, trigger_receipt_no,
                       outcome_sha256, outcome_json, evaluated_at, '2099-01-01'
                FROM forward_event_outcomes
                """
            )

    conn.close()
    reopened = init_db(database)
    try:
        replayed = ForwardOutcomeLedger(reopened).get(outcome.decision_id)
        assert replayed == first
        decision_after = reopened.execute(
            "SELECT decision_json, recorded_at FROM forward_event_decisions"
        ).fetchone()
        assert decision_after == decision_before
    finally:
        reopened.close()


def test_outcome_rejects_metric_tampering_and_changed_frozen_costs(outcome_context):
    _, _, stored_decision, evaluator_clock, outcome_ledger = outcome_context
    outcome = _evaluate(stored_decision, evaluator_clock)
    payload = outcome.model_dump(mode="python")
    payload["abnormal_net_return"] += 0.5
    with pytest.raises(ValidationError, match="metrics do not match"):
        ForwardOutcome.model_validate(payload)

    observation_payload = outcome.stock_exit.model_dump(mode="python")
    observation_payload["close"] += 1.0
    with pytest.raises(ValidationError, match="observation hash"):
        DailyCloseObservation.model_validate(observation_payload)

    calendar_payload = outcome.session_calendar.model_dump(mode="python")
    calendar_payload["source_payload_sha256"] = "d" * 64
    with pytest.raises(ValidationError, match="source payload hash"):
        TradingSessionCalendar.model_validate(calendar_payload)

    intermediate_payload = outcome.benchmark_sessions[1].model_dump(mode="python")
    intermediate_payload["market"] = "KOSDAQ"
    intermediate_payload["observation_sha256"] = daily_close_observation_sha256(
        **{
            key: intermediate_payload[key]
            for key in (
                "instrument_type",
                "symbol",
                "market",
                "trading_date",
                "close",
                "source",
                "source_version",
                "available_at",
                "captured_at",
            )
        }
    )
    payload = outcome.model_dump(mode="python")
    payload["benchmark_sessions"] = (
        outcome.benchmark_sessions[0],
        DailyCloseObservation.model_validate(intermediate_payload),
        *outcome.benchmark_sessions[2:],
    )
    with pytest.raises(ValidationError, match="frozen benchmark"):
        ForwardOutcome.model_validate(payload)

    payload = outcome.model_dump(mode="python")
    changed_costs = outcome.costs.model_copy(update={"commission_per_side": 0.001})
    changed_cost = frozen_roundtrip_cost_fraction(
        changed_costs,
        buy_date=outcome.stock_entry.trading_date,
        sell_date=outcome.stock_exit.trading_date,
        market=outcome.market,
    )
    payload["costs"] = changed_costs
    payload["roundtrip_cost_fraction"] = changed_cost
    payload["raw_net_return"] = outcome.stock_gross_return - changed_cost
    payload["abnormal_net_return"] = (
        outcome.stock_gross_return - outcome.benchmark_gross_return - changed_cost
    )
    self_consistent_forgery = ForwardOutcome.model_validate(payload)
    with pytest.raises(ForwardOutcomeError, match="frozen eligible decision"):
        outcome_ledger.record(self_consistent_forgery)


def test_evaluator_fails_closed_for_immature_missing_or_future_market_data(
    outcome_context,
):
    _, _, stored_decision, evaluator_clock, _ = outcome_context
    stock, benchmark = _market_observations()
    evaluator = ProspectiveOutcomeEvaluator(clock=evaluator_clock)

    with pytest.raises(ForwardOutcomeError, match="has not matured"):
        evaluator.evaluate(
            stored_decision.decision,
            stock_observations=stock,
            benchmark_observations=benchmark,
            session_calendar=_session_calendar(
                tuple(item.trading_date for item in benchmark[:5])
            ),
        )
    benchmark_without_september_18 = tuple(
        item for item in benchmark if item.trading_date != date(2026, 9, 18)
    )
    with pytest.raises(ForwardOutcomeError, match="benchmark evidence is incomplete"):
        evaluator.evaluate(
            stored_decision.decision,
            stock_observations=stock,
            benchmark_observations=benchmark_without_september_18,
            session_calendar=_session_calendar(),
        )
    stock_without_september_23 = tuple(
        item for item in stock if item.trading_date != date(2026, 9, 23)
    )
    with pytest.raises(ForwardOutcomeError, match="missing stock close"):
        evaluator.evaluate(
            stored_decision.decision,
            stock_observations=stock_without_september_23,
            benchmark_observations=benchmark,
            session_calendar=_session_calendar(),
        )

    evaluator_clock.value = _time(23, 7)
    with pytest.raises(ForwardOutcomeError, match="not available at evaluation time"):
        evaluator.evaluate(
            stored_decision.decision,
            stock_observations=stock,
            benchmark_observations=benchmark,
            session_calendar=_session_calendar(),
        )


def test_future_close_cannot_claim_pre_trading_date_availability(outcome_context):
    _, _, stored_decision, evaluator_clock, _ = outcome_context
    stock, benchmark = _market_observations()
    original = stock[5]
    available_at = _time(16, 3)
    captured_at = available_at + timedelta(minutes=5)
    forged_hash = daily_close_observation_sha256(
        instrument_type=original.instrument_type,
        symbol=original.symbol,
        market=original.market,
        trading_date=original.trading_date,
        close=original.close,
        source=original.source,
        source_version=original.source_version,
        available_at=available_at,
        captured_at=captured_at,
    )
    forged = original.model_copy(
        update={
            "available_at": available_at,
            "captured_at": captured_at,
            "observation_sha256": forged_hash,
        }
    )

    with pytest.raises(ValidationError, match="before its trading date"):
        DailyCloseObservation.model_validate(forged.model_dump(mode="python"))
    with pytest.raises(ForwardOutcomeError, match="observation failed validation"):
        ProspectiveOutcomeEvaluator(clock=evaluator_clock).evaluate(
            stored_decision.decision,
            stock_observations=(*stock[:5], forged, *stock[6:]),
            benchmark_observations=benchmark,
            session_calendar=_session_calendar(),
        )


def test_non_trading_day_disclosure_enters_on_next_session(tmp_path):
    conn = init_db(tmp_path / "weekend-outcome.db")
    try:
        event = _event(
            receipt_no="20260919000001",
            source_timestamp=_time(19),
        )
        stored_decision, _ = _record_decision(
            conn,
            specification=_specification("weekend-outcome"),
            event=event,
        )
        stock, benchmark = _market_observations()
        session_dates = (
            date(2026, 9, 21),
            date(2026, 9, 22),
            date(2026, 9, 23),
            date(2026, 9, 28),
            date(2026, 9, 29),
        )
        outcome = ProspectiveOutcomeEvaluator(clock=ManualClock(_time(29, 9))).evaluate(
            stored_decision.decision,
            stock_observations=stock,
            benchmark_observations=benchmark,
            session_calendar=_session_calendar(
                session_dates,
                window_start=date(2026, 9, 19),
                closed_weekdays=(date(2026, 9, 24), date(2026, 9, 25)),
            ),
        )

        assert outcome.event_date == date(2026, 9, 19)
        assert outcome.stock_entry.trading_date == date(2026, 9, 21)
        assert outcome.stock_exit.trading_date == date(2026, 9, 29)
    finally:
        conn.close()


def test_schema_and_ledger_reject_self_consistent_wrong_horizon(outcome_context):
    _, conn, stored_decision, evaluator_clock, outcome_ledger = outcome_context
    outcome = _evaluate(stored_decision, evaluator_clock)
    stock, benchmark = _market_observations()
    wrong_stock_entry = stock[2]
    wrong_benchmark_entry = benchmark[2]
    wrong_stock_gross = outcome.stock_exit.close / wrong_stock_entry.close - 1.0
    wrong_benchmark_gross = (
        outcome.benchmark_exit.close / wrong_benchmark_entry.close - 1.0
    )
    wrong_cost = frozen_roundtrip_cost_fraction(
        outcome.costs,
        buy_date=wrong_stock_entry.trading_date,
        sell_date=outcome.stock_exit.trading_date,
        market=outcome.market,
    )
    updates = {
        "stock_entry": wrong_stock_entry,
        "benchmark_entry": wrong_benchmark_entry,
        "stock_gross_return": wrong_stock_gross,
        "benchmark_gross_return": wrong_benchmark_gross,
        "roundtrip_cost_fraction": wrong_cost,
        "raw_net_return": wrong_stock_gross - wrong_cost,
        "abnormal_net_return": wrong_stock_gross - wrong_benchmark_gross - wrong_cost,
    }
    payload = outcome.model_dump(mode="python")
    payload.update(updates)

    with pytest.raises(ValidationError, match="violate frozen rules"):
        ForwardOutcome.model_validate(payload)
    with pytest.raises(ForwardOutcomeError, match="canonical validation"):
        outcome_ledger.record(outcome.model_copy(update=updates))
    assert conn.execute("SELECT COUNT(*) FROM forward_event_outcomes").fetchone() == (
        0,
    )


def test_session_calendar_is_derived_from_retained_krx_bytes(outcome_context):
    _, conn, stored_decision, _, _ = outcome_context
    dates_without_friday = (
        date(2026, 9, 16),
        date(2026, 9, 17),
        date(2026, 9, 21),
        date(2026, 9, 22),
        date(2026, 9, 23),
    )

    with pytest.raises(ValidationError, match="classify every weekday"):
        _session_calendar(dates_without_friday)

    authoritative = _session_calendar(
        window_end=date(2026, 9, 24),
        closed_weekdays=(date(2026, 9, 24),),
    )
    payload = authoritative.model_dump(mode="python")
    shifted_sessions = tuple(
        session
        for session in authoritative.sessions
        if session.trading_date != date(2026, 9, 18)
    ) + (
        TradingSession(
            trading_date=date(2026, 9, 24),
            close_at=_time(24, 6) + timedelta(minutes=30),
        ),
    )
    attack = {
        **payload,
        "sessions": shifted_sessions,
        "closed_weekdays": (date(2026, 9, 18),),
    }
    calendar_hash_fields = (
        "market",
        "symbol",
        "source",
        "source_version",
        "source_year",
        "source_payload_utf8",
        "source_payload_sha256",
        "session_hours_source",
        "session_hours_source_version",
        "session_hours_source_payload_utf8",
        "session_hours_source_payload_sha256",
        "exception_policy_id",
        "exception_policy_version",
        "exception_evidence_sha256",
        "window_start",
        "window_end",
        "sessions",
        "closed_weekdays",
        "captured_at",
    )
    attack["calendar_sha256"] = trading_session_calendar_sha256(
        **{key: attack[key] for key in calendar_hash_fields}
    )
    with pytest.raises(ValidationError, match="retained KRX evidence"):
        TradingSessionCalendar.model_validate(attack)

    forged_calendar = authoritative.model_copy(
        update={
            "sessions": shifted_sessions,
            "closed_weekdays": (date(2026, 9, 18),),
            "calendar_sha256": attack["calendar_sha256"],
        }
    )
    stock, benchmark = _market_observations()
    with pytest.raises(ForwardOutcomeError, match="calendar failed validation"):
        ProspectiveOutcomeEvaluator(clock=ManualClock(_time(24, 9))).evaluate(
            stored_decision.decision,
            stock_observations=stock,
            benchmark_observations=benchmark,
            session_calendar=forged_calendar,
        )

    raw = json.loads(authoritative.source_payload_utf8)
    raw["block1"] = [row for row in raw["block1"] if row["calnd_dd"] != "2026-09-24"]
    attack["source_payload_utf8"] = json.dumps(
        raw,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    attack["source_payload_sha256"] = krx_holiday_payload_sha256(
        attack["source_payload_utf8"]
    )
    attack["calendar_sha256"] = trading_session_calendar_sha256(
        **{key: attack[key] for key in calendar_hash_fields}
    )
    with pytest.raises(ValidationError, match="failed KRX parsing"):
        TradingSessionCalendar.model_validate(attack)

    hours_attack = authoritative.model_dump(mode="python")
    hours_attack["sessions"] = authoritative.sessions
    hours_attack["session_hours_source_payload_utf8"] = hours_attack[
        "session_hours_source_payload_utf8"
    ].replace("09:00 - 15:30", "09:00 - 16:30", 1)
    hours_attack["session_hours_source_payload_sha256"] = session_hours_payload_sha256(
        hours_attack["session_hours_source_payload_utf8"]
    )
    hours_attack["calendar_sha256"] = trading_session_calendar_sha256(
        **{key: hours_attack[key] for key in calendar_hash_fields}
    )
    with pytest.raises(ValidationError, match="session-hours evidence is incomplete"):
        TradingSessionCalendar.model_validate(hours_attack)
    assert conn.execute("SELECT COUNT(*) FROM forward_event_outcomes").fetchone() == (
        0,
    )


def test_known_exceptional_session_fails_without_exact_krx_hours(tmp_path):
    conn = init_db(tmp_path / "exceptional-hours.db")
    try:
        event = _event(
            receipt_no="20261112000001",
            source_timestamp=datetime(2026, 11, 12, tzinfo=UTC),
        )
        stored_decision, decision_ledger = _record_decision(
            conn,
            specification=_specification("exceptional-hours"),
            event=event,
        )
        ordinary_dates = (
            date(2026, 11, 12),
            date(2026, 11, 13),
            date(2026, 11, 16),
            date(2026, 11, 17),
            date(2026, 11, 18),
        )
        ordinary = _session_calendar(
            ordinary_dates,
            window_start=date(2026, 11, 12),
            window_end=date(2026, 11, 18),
        )
        forged_sessions = ordinary.sessions + (
            TradingSession(
                trading_date=date(2026, 11, 19),
                close_at=datetime(2026, 11, 19, 6, 30, tzinfo=UTC),
            ),
        )
        values = ordinary.model_dump(mode="python")
        values.update(
            {
                "window_end": date(2026, 11, 19),
                "sessions": forged_sessions,
            }
        )
        hash_fields = (
            "market",
            "symbol",
            "source",
            "source_version",
            "source_year",
            "source_payload_utf8",
            "source_payload_sha256",
            "session_hours_source",
            "session_hours_source_version",
            "session_hours_source_payload_utf8",
            "session_hours_source_payload_sha256",
            "exception_policy_id",
            "exception_policy_version",
            "exception_evidence_sha256",
            "window_start",
            "window_end",
            "sessions",
            "closed_weekdays",
            "captured_at",
        )
        values["calendar_sha256"] = trading_session_calendar_sha256(
            **{key: values[key] for key in hash_fields}
        )

        with pytest.raises(
            ValidationError, match="session-hours evidence is incomplete"
        ):
            TradingSessionCalendar.model_validate(values)

        forged_calendar = ordinary.model_copy(
            update={
                "window_end": values["window_end"],
                "sessions": forged_sessions,
                "calendar_sha256": values["calendar_sha256"],
            }
        )
        outcome_dates = ordinary_dates[1:] + (date(2026, 11, 19),)
        stock = tuple(
            _observation("stock", "005930", observed, 100.0 + offset)
            for offset, observed in enumerate(outcome_dates)
        )
        benchmark = tuple(
            _observation("benchmark", "KOSPI", observed, 1_000.0 + offset)
            for offset, observed in enumerate(outcome_dates)
        )
        with pytest.raises(ForwardOutcomeError, match="calendar failed validation"):
            ProspectiveOutcomeEvaluator(
                clock=ManualClock(datetime(2026, 11, 19, 7, 5, tzinfo=UTC))
            ).evaluate(
                stored_decision.decision,
                stock_observations=stock,
                benchmark_observations=benchmark,
                session_calendar=forged_calendar,
            )

        assert (
            ForwardOutcomeLedger(
                conn,
                decision_ledger=decision_ledger,
            ).list_for_experiment("exceptional-hours")
            == ()
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM forward_event_outcomes"
        ).fetchone() == (0,)
    finally:
        conn.close()


def test_rejected_decision_and_unsupported_frozen_rule_have_no_outcome(tmp_path):
    rejected_conn = init_db(tmp_path / "rejected.db")
    try:
        rejected, _ = _record_decision(
            rejected_conn,
            specification=_specification("rejected-outcome", minimum_score=0.9),
        )
        stock, benchmark = _market_observations()
        with pytest.raises(ForwardOutcomeError, match="rejected decisions"):
            ProspectiveOutcomeEvaluator(clock=ManualClock(_time(23, 9))).evaluate(
                rejected.decision,
                stock_observations=stock,
                benchmark_observations=benchmark,
                session_calendar=_session_calendar(),
            )
        assert rejected_conn.execute(
            "SELECT COUNT(*) FROM forward_event_outcomes"
        ).fetchone() == (0,)
    finally:
        rejected_conn.close()

    unsupported_conn = init_db(tmp_path / "unsupported.db")
    unsupported_rule = ExecutionRule(
        rule_id="same_day_vwap",
        rule_version="future-v1",
        parameters=(RuleParameter(name="session_offset", value=1),),
    )
    try:
        unsupported, _ = _record_decision(
            unsupported_conn,
            specification=_specification(
                "unsupported-outcome",
                entry_rule=unsupported_rule,
            ),
        )
        stock, benchmark = _market_observations()
        with pytest.raises(ForwardOutcomeError, match="unsupported frozen entry"):
            ProspectiveOutcomeEvaluator(clock=ManualClock(_time(23, 9))).evaluate(
                unsupported.decision,
                stock_observations=stock,
                benchmark_observations=benchmark,
                session_calendar=_session_calendar(),
            )
    finally:
        unsupported_conn.close()


def test_outcome_table_foreign_key_rejects_orphan(outcome_context):
    _, conn, stored_decision, evaluator_clock, _ = outcome_context
    outcome = _evaluate(stored_decision, evaluator_clock)
    payload = json.loads(outcome.canonical_json())
    payload["decision_id"] = "f" * 64
    orphan_json = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        with conn:
            conn.execute(
                """
                INSERT INTO forward_event_outcomes (
                    outcome_id, decision_id, decision_sha256,
                    experiment_id, experiment_version, trigger_receipt_no,
                    outcome_sha256, outcome_json, evaluated_at, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "e" * 64,
                    "f" * 64,
                    outcome.decision_sha256,
                    outcome.experiment_id,
                    outcome.experiment_version,
                    outcome.trigger_receipt_no,
                    hashlib.sha256(orphan_json.encode()).hexdigest(),
                    orphan_json,
                    outcome.evaluated_at.isoformat(),
                    _time(23, 10).isoformat(),
                ),
            )
