from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Callable, Iterable

from pydantic import BaseModel, ConfigDict, model_validator

from kdtb.events.chronology import as_seoul_timestamp
from kdtb.experiments.ledger import ForwardDecisionLedger
from kdtb.schemas.experiment import require_utc
from kdtb.schemas.forward_decision import ForwardDecision
from kdtb.schemas.forward_outcome import (
    DailyCloseObservation,
    ForwardOutcome,
    TradingSessionCalendar,
    frozen_roundtrip_cost_fraction,
    frozen_session_offsets,
    outcome_id_for,
)


class ForwardOutcomeError(ValueError):
    """Raised when a prospective outcome violates a frozen boundary."""


class StoredForwardOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    outcome: ForwardOutcome
    recorded_at: datetime

    @model_validator(mode="after")
    def chronology_is_consistent(self) -> "StoredForwardOutcome":
        require_utc(self.recorded_at, field="outcome recorded_at")
        if self.recorded_at < self.outcome.evaluated_at:
            raise ValueError("outcome recording cannot precede evaluation")
        return self


def _validated_observations(
    observations: Iterable[DailyCloseObservation],
    *,
    instrument_type: str,
    symbol: str,
    market: str,
) -> tuple[DailyCloseObservation, ...]:
    result: list[DailyCloseObservation] = []
    seen_dates = set()
    for observation in observations:
        try:
            validated = DailyCloseObservation.model_validate(
                observation.model_dump(mode="python")
            )
        except Exception as error:
            raise ForwardOutcomeError("market observation failed validation") from error
        if (
            validated.instrument_type != instrument_type
            or validated.symbol != symbol
            or validated.market != market
        ):
            raise ForwardOutcomeError(
                f"{instrument_type} observation does not match expected instrument"
            )
        if validated.trading_date in seen_dates:
            raise ForwardOutcomeError(
                f"duplicate {instrument_type} observation trading date"
            )
        seen_dates.add(validated.trading_date)
        result.append(validated)
    return tuple(sorted(result, key=lambda item: item.trading_date))


class ProspectiveOutcomeEvaluator:
    """Evaluate a frozen eligible decision from source-bound daily closes."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self) -> datetime:
        return require_utc(self.clock(), field="outcome evaluator clock")

    def evaluate(
        self,
        decision: ForwardDecision,
        *,
        stock_observations: Iterable[DailyCloseObservation],
        benchmark_observations: Iterable[DailyCloseObservation],
        session_calendar: TradingSessionCalendar,
    ) -> ForwardOutcome:
        try:
            canonical = decision.canonical_json()
            validated_decision = ForwardDecision.model_validate_json(canonical)
        except Exception as error:
            raise ForwardOutcomeError("forward decision failed validation") from error
        if validated_decision != decision:
            raise ForwardOutcomeError(
                "forward decision differs from its canonical validated form"
            )
        if decision.disposition != "eligible":
            raise ForwardOutcomeError(
                "rejected decisions have no realized trade outcome"
            )

        event = decision.inputs.event
        stock_code = event.issuer.stock_code
        if stock_code is None:
            raise ForwardOutcomeError("eligible outcome requires an issuer stock code")
        if event.market not in {"KOSPI", "KOSDAQ"}:
            raise ForwardOutcomeError("eligible outcome requires a supported market")
        trigger_source = next(
            (
                source
                for source in event.source_provenance
                if source.receipt_no == decision.inputs.trigger_receipt_no
            ),
            None,
        )
        if trigger_source is None:
            raise ForwardOutcomeError("decision trigger has no source timestamp")
        event_date = as_seoul_timestamp(trigger_source.receipt_timestamp).date()

        specification = decision.experiment
        try:
            entry_offset, holding_sessions = frozen_session_offsets(
                specification.entry_rule,
                specification.exit_rule,
            )
        except ValueError as error:
            raise ForwardOutcomeError(str(error)) from error
        benchmark_mapping = next(
            (
                mapping
                for mapping in specification.benchmark.market_mappings
                if mapping.market == event.market
            ),
            None,
        )
        if benchmark_mapping is None:
            raise ForwardOutcomeError("frozen benchmark has no event-market mapping")

        try:
            validated_calendar = TradingSessionCalendar.model_validate(
                session_calendar.model_dump(mode="python")
            )
        except Exception as error:
            raise ForwardOutcomeError(
                "trading-session calendar failed validation"
            ) from error
        if (
            validated_calendar.market != event.market
            or validated_calendar.symbol != benchmark_mapping.symbol
            or not (
                validated_calendar.window_start
                <= event_date
                <= validated_calendar.window_end
            )
        ):
            raise ForwardOutcomeError(
                "trading-session calendar does not match the frozen benchmark"
            )

        future_sessions = tuple(
            session
            for session in validated_calendar.sessions
            if session.trading_date > event_date
        )
        entry_index = entry_offset - 1
        exit_index = entry_index + holding_sessions
        if len(future_sessions) <= exit_index:
            raise ForwardOutcomeError("frozen outcome horizon has not matured")
        required_sessions = future_sessions[: exit_index + 1]

        benchmark = _validated_observations(
            benchmark_observations,
            instrument_type="benchmark",
            symbol=benchmark_mapping.symbol,
            market=event.market,
        )
        benchmark_by_date = {
            observation.trading_date: observation for observation in benchmark
        }
        try:
            benchmark_sessions = tuple(
                benchmark_by_date[session.trading_date] for session in required_sessions
            )
        except KeyError as error:
            raise ForwardOutcomeError(
                "benchmark evidence is incomplete for the frozen session window"
            ) from error
        benchmark_entry = benchmark_sessions[entry_index]
        benchmark_exit = benchmark_sessions[exit_index]

        stock = _validated_observations(
            stock_observations,
            instrument_type="stock",
            symbol=stock_code,
            market=event.market,
        )
        stock_by_date = {observation.trading_date: observation for observation in stock}
        try:
            stock_entry = stock_by_date[benchmark_entry.trading_date]
            stock_exit = stock_by_date[benchmark_exit.trading_date]
        except KeyError as error:
            raise ForwardOutcomeError(
                "missing stock close on a frozen benchmark trading date"
            ) from error

        selected_observations = (
            stock_entry,
            stock_exit,
            benchmark_entry,
            benchmark_exit,
        )
        evaluation_cutoff = max(
            validated_calendar.captured_at,
            *(observation.captured_at for observation in benchmark_sessions),
            *(observation.captured_at for observation in selected_observations),
        )
        if self._now() < evaluation_cutoff:
            raise ForwardOutcomeError(
                "frozen outcome horizon is not available at evaluation time"
            )

        stock_gross = stock_exit.close / stock_entry.close - 1.0
        benchmark_gross = benchmark_exit.close / benchmark_entry.close - 1.0
        try:
            cost_fraction = frozen_roundtrip_cost_fraction(
                specification.costs,
                buy_date=stock_entry.trading_date,
                sell_date=stock_exit.trading_date,
                market=event.market,
            )
        except Exception as error:
            raise ForwardOutcomeError(
                "frozen cost model cannot be evaluated"
            ) from error
        decision_sha256 = decision.sha256()
        try:
            return ForwardOutcome(
                outcome_id=outcome_id_for(
                    decision_id=decision.decision_id,
                    decision_sha256=decision_sha256,
                ),
                decision_id=decision.decision_id,
                decision_sha256=decision_sha256,
                decision_assessed_at=decision.inputs.assessed_at,
                experiment_id=specification.experiment_id,
                experiment_version=specification.version,
                experiment_sha256=decision.experiment_sha256,
                trigger_receipt_no=decision.inputs.trigger_receipt_no,
                event_date=event_date,
                issuer_stock_code=stock_code,
                market=event.market,
                entry_rule=specification.entry_rule,
                exit_rule=specification.exit_rule,
                benchmark=specification.benchmark,
                costs=specification.costs,
                session_calendar=validated_calendar,
                benchmark_sessions=benchmark_sessions,
                stock_entry=stock_entry,
                stock_exit=stock_exit,
                benchmark_entry=benchmark_entry,
                benchmark_exit=benchmark_exit,
                stock_gross_return=stock_gross,
                benchmark_gross_return=benchmark_gross,
                roundtrip_cost_fraction=cost_fraction,
                raw_net_return=stock_gross - cost_fraction,
                abnormal_net_return=stock_gross - benchmark_gross - cost_fraction,
                evaluated_at=evaluation_cutoff,
            )
        except Exception as error:
            raise ForwardOutcomeError("forward outcome failed validation") from error


class ForwardOutcomeLedger:
    """Append-only storage for outcomes linked to immutable M2.2 decisions."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        decision_ledger: ForwardDecisionLedger | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.conn = conn
        self.decision_ledger = decision_ledger or ForwardDecisionLedger(conn)
        if self.decision_ledger.conn is not conn:
            raise ValueError("outcome and decision ledgers must share one connection")
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self) -> datetime:
        return require_utc(self.clock(), field="outcome ledger clock")

    def _validate_decision_reference(self, outcome: ForwardOutcome) -> None:
        stored = self.decision_ledger.get(
            outcome.experiment_id,
            outcome.experiment_version,
            outcome.trigger_receipt_no,
        )
        if stored is None:
            raise ForwardOutcomeError("outcome references an unstored decision")
        decision = stored.decision
        event = decision.inputs.event
        trigger_source = next(
            source
            for source in event.source_provenance
            if source.receipt_no == decision.inputs.trigger_receipt_no
        )
        expected = (
            decision.decision_id,
            decision.sha256(),
            decision.inputs.assessed_at,
            decision.experiment.experiment_id,
            decision.experiment.version,
            decision.experiment_sha256,
            decision.inputs.trigger_receipt_no,
            as_seoul_timestamp(trigger_source.receipt_timestamp).date(),
            event.issuer.stock_code,
            event.market,
            decision.experiment.entry_rule,
            decision.experiment.exit_rule,
            decision.experiment.benchmark,
            decision.experiment.costs,
            "eligible",
        )
        actual = (
            outcome.decision_id,
            outcome.decision_sha256,
            outcome.decision_assessed_at,
            outcome.experiment_id,
            outcome.experiment_version,
            outcome.experiment_sha256,
            outcome.trigger_receipt_no,
            outcome.event_date,
            outcome.issuer_stock_code,
            outcome.market,
            outcome.entry_rule,
            outcome.exit_rule,
            outcome.benchmark,
            outcome.costs,
            decision.disposition,
        )
        if actual != expected:
            raise ForwardOutcomeError(
                "outcome does not match its frozen eligible decision"
            )

    def _load_row(self, row: sqlite3.Row | tuple) -> StoredForwardOutcome:
        (
            outcome_id,
            decision_id,
            decision_sha256,
            experiment_id,
            experiment_version,
            trigger_receipt_no,
            outcome_sha256,
            outcome_json,
            evaluated_at_text,
            recorded_at_text,
        ) = row
        try:
            outcome = ForwardOutcome.model_validate_json(outcome_json)
        except Exception as error:
            raise ForwardOutcomeError(f"invalid stored outcome {outcome_id}") from error
        if outcome.canonical_json() != outcome_json:
            raise ForwardOutcomeError(f"stored outcome {outcome_id} is not canonical")
        redundant = (
            outcome.outcome_id,
            outcome.decision_id,
            outcome.decision_sha256,
            outcome.experiment_id,
            outcome.experiment_version,
            outcome.trigger_receipt_no,
            outcome.sha256(),
            outcome.evaluated_at.isoformat(),
        )
        stored = (
            outcome_id,
            decision_id,
            decision_sha256,
            experiment_id,
            experiment_version,
            trigger_receipt_no,
            outcome_sha256,
            evaluated_at_text,
        )
        if stored != redundant:
            raise ForwardOutcomeError(
                f"stored outcome metadata mismatch for {outcome_id}"
            )
        try:
            result = StoredForwardOutcome(
                outcome=outcome,
                recorded_at=datetime.fromisoformat(recorded_at_text),
            )
        except Exception as error:
            raise ForwardOutcomeError(
                f"invalid stored outcome chronology for {outcome_id}"
            ) from error
        self._validate_decision_reference(result.outcome)
        return result

    @staticmethod
    def _select_sql(where: str = "") -> str:
        return f"""
            SELECT outcome_id, decision_id, decision_sha256,
                   experiment_id, experiment_version, trigger_receipt_no,
                   outcome_sha256, outcome_json, evaluated_at, recorded_at
            FROM forward_event_outcomes
            {where}
        """

    def get(self, decision_id: str) -> StoredForwardOutcome | None:
        row = self.conn.execute(
            self._select_sql("WHERE decision_id = ?"),
            (decision_id,),
        ).fetchone()
        return self._load_row(row) if row is not None else None

    def list_for_experiment(
        self,
        experiment_id: str,
    ) -> tuple[StoredForwardOutcome, ...]:
        rows = self.conn.execute(
            self._select_sql(
                "WHERE experiment_id = ? "
                "ORDER BY evaluated_at, trigger_receipt_no, experiment_version"
            ),
            (experiment_id,),
        ).fetchall()
        return tuple(self._load_row(row) for row in rows)

    def record(self, outcome: ForwardOutcome) -> StoredForwardOutcome:
        try:
            outcome_json = outcome.canonical_json()
            validated = ForwardOutcome.model_validate_json(outcome_json)
        except Exception as error:
            raise ForwardOutcomeError(
                "forward outcome failed canonical validation"
            ) from error
        if validated != outcome:
            raise ForwardOutcomeError(
                "forward outcome differs from its canonical validated form"
            )
        self._validate_decision_reference(outcome)

        existing = self.get(outcome.decision_id)
        if existing is not None:
            if existing.outcome == outcome:
                return existing
            raise ForwardOutcomeError("forward outcome identity is immutable")

        recorded_at = self._now()
        if recorded_at < outcome.evaluated_at:
            raise ForwardOutcomeError("outcome recording cannot precede evaluation")
        try:
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO forward_event_outcomes (
                        outcome_id, decision_id, decision_sha256,
                        experiment_id, experiment_version, trigger_receipt_no,
                        outcome_sha256, outcome_json, evaluated_at, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        outcome.outcome_id,
                        outcome.decision_id,
                        outcome.decision_sha256,
                        outcome.experiment_id,
                        outcome.experiment_version,
                        outcome.trigger_receipt_no,
                        outcome.sha256(),
                        outcome_json,
                        outcome.evaluated_at.isoformat(),
                        recorded_at.isoformat(),
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise ForwardOutcomeError("forward outcome recording failed") from error

        stored = self.get(outcome.decision_id)
        if stored is None:
            raise ForwardOutcomeError("recorded forward outcome could not be reloaded")
        return stored
