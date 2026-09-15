from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable

from pydantic import BaseModel, ConfigDict, model_validator

from kdtb.alerts.service import ResearchAlertBuilder
from kdtb.event_identity import canonical_event_snapshot
from kdtb.experiments.registry import ExperimentRegistry, RegisteredExperiment
from kdtb.live.dart_watcher import EventEnvelope
from kdtb.schemas.alert import ResearchAlert
from kdtb.schemas.forward_decision import (
    DecisionInputSnapshot,
    ForwardDecision,
    decision_id_for,
    decision_rejection_reasons,
    feature_snapshot,
)
from kdtb.schemas.experiment import require_utc


class ForwardDecisionLedgerError(ValueError):
    """Raised when a forward decision would violate an immutable boundary."""


class StoredForwardDecision(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    decision: ForwardDecision
    recorded_at: datetime

    @model_validator(mode="after")
    def chronology_is_consistent(self) -> "StoredForwardDecision":
        require_utc(self.recorded_at, field="recorded_at")
        if self.recorded_at < self.decision.inputs.assessed_at:
            raise ValueError("decision recording cannot precede decision time")
        return self


def build_decision_inputs(
    *,
    envelope: EventEnvelope,
    registered: RegisteredExperiment,
    alert_builder: ResearchAlertBuilder,
) -> DecisionInputSnapshot:
    decided_at = envelope.normalized_at
    if decided_at.tzinfo is None or decided_at.utcoffset() is None:
        raise ForwardDecisionLedgerError(
            "event normalization time must be timezone-aware"
        )
    decided_at = decided_at.astimezone(timezone.utc)
    normalized_envelope = EventEnvelope(
        delivery_id=envelope.delivery_id,
        trigger_receipt_no=envelope.trigger_receipt_no,
        normalized_at=decided_at,
        event=envelope.event,
    )
    alert = alert_builder.build(normalized_envelope, assessed_at=decided_at)
    return _decision_inputs_from_alert(alert=alert, registered=registered)


def _decision_inputs_from_alert(
    *,
    alert: ResearchAlert,
    registered: RegisteredExperiment,
) -> DecisionInputSnapshot:
    specification = registered.specification
    event_snapshot_json, event_sha256 = canonical_event_snapshot(alert.event)
    features = tuple(
        feature_snapshot(
            definition,
            event=alert.event,
            significance=alert.significance,
            historical_context=alert.historical_context,
        )
        for definition in specification.features
    )
    return DecisionInputSnapshot(
        delivery_id=alert.delivery_id,
        trigger_receipt_no=alert.trigger_receipt_no,
        event_sha256=event_sha256,
        event_snapshot_json=event_snapshot_json,
        normalized_at=alert.processed_at,
        assessed_at=alert.assessed_at,
        significance=alert.significance,
        historical_context=alert.historical_context,
        features=features,
    )


def build_forward_decision(
    registered: RegisteredExperiment,
    inputs: DecisionInputSnapshot,
) -> ForwardDecision:
    if registered.activated_at is None:
        raise ForwardDecisionLedgerError(
            "cannot build a decision for an unactivated experiment"
        )
    input_sha256 = inputs.sha256()
    rejection_reasons = decision_rejection_reasons(
        registered.specification,
        inputs,
    )
    return ForwardDecision(
        decision_id=decision_id_for(
            experiment_sha256=registered.specification_sha256,
            decision_input_sha256=input_sha256,
        ),
        experiment=registered.specification,
        experiment_sha256=registered.specification_sha256,
        experiment_activated_at=registered.activated_at,
        inputs=inputs,
        decision_input_sha256=input_sha256,
        disposition="rejected" if rejection_reasons else "eligible",
        rejection_reasons=rejection_reasons,
    )


class ForwardDecisionLedger:
    """Append-only SQLite store for pre-outcome experiment decisions."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        registry: ExperimentRegistry | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.conn = conn
        self.registry = registry or ExperimentRegistry(conn)
        if self.registry.conn is not conn:
            raise ValueError("decision ledger and registry must share one connection")
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self) -> datetime:
        return require_utc(self.clock(), field="decision ledger clock")

    def _validate_references(self, decision: ForwardDecision) -> None:
        specification = decision.experiment
        registered = self.registry.get(
            specification.experiment_id, specification.version
        )
        if registered is None:
            raise ForwardDecisionLedgerError(
                "decision references an unregistered experiment version"
            )
        if (
            registered.specification_sha256 != decision.experiment_sha256
            or registered.specification != specification
            or registered.activated_at != decision.experiment_activated_at
        ):
            raise ForwardDecisionLedgerError(
                "decision experiment does not match its registered activation"
            )
        active = self.registry.active_at(
            specification.experiment_id,
            decision.inputs.assessed_at,
        )
        if (
            active is None
            or active.specification.version != specification.version
            or active.specification_sha256 != decision.experiment_sha256
        ):
            raise ForwardDecisionLedgerError(
                "decision does not reference the active experiment version"
            )

        row = self.conn.execute(
            """
            SELECT event_sha256, event_json, normalized_at
            FROM canonical_event_snapshots
            WHERE trigger_receipt_no = ?
            """,
            (decision.inputs.trigger_receipt_no,),
        ).fetchone()
        if row is None:
            raise ForwardDecisionLedgerError(
                "decision references an unstored canonical event snapshot"
            )
        event_json, event_sha256 = canonical_event_snapshot(decision.inputs.event)
        if (
            row[0] != decision.inputs.event_sha256
            or row[0] != event_sha256
            or row[1] != event_json
            or datetime.fromisoformat(row[2]).astimezone(timezone.utc)
            != decision.inputs.normalized_at
        ):
            raise ForwardDecisionLedgerError(
                "decision inputs do not match the stored canonical event snapshot"
            )

    def _load_row(self, row: sqlite3.Row | tuple) -> StoredForwardDecision:
        (
            decision_id,
            experiment_id,
            experiment_version,
            experiment_sha256,
            experiment_activated_at,
            trigger_receipt_no,
            event_sha256,
            decision_input_sha256,
            disposition,
            rejection_reasons_json,
            decision_sha256,
            decision_json,
            decided_at_text,
            recorded_at_text,
        ) = row
        try:
            decision = ForwardDecision.model_validate_json(decision_json)
        except Exception as error:
            raise ForwardDecisionLedgerError(
                f"invalid stored forward decision {decision_id}"
            ) from error
        if decision.canonical_json() != decision_json:
            raise ForwardDecisionLedgerError(
                f"stored forward decision {decision_id} is not canonical"
            )
        redundant = (
            decision.decision_id,
            decision.experiment.experiment_id,
            decision.experiment.version,
            decision.experiment_sha256,
            decision.experiment_activated_at.isoformat(),
            decision.inputs.trigger_receipt_no,
            decision.inputs.event_sha256,
            decision.decision_input_sha256,
            decision.disposition,
            json.dumps(
                [
                    reason.model_dump(mode="json")
                    for reason in decision.rejection_reasons
                ],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            decision.sha256(),
            decision.inputs.assessed_at.isoformat(),
        )
        stored = (
            decision_id,
            experiment_id,
            experiment_version,
            experiment_sha256,
            experiment_activated_at,
            trigger_receipt_no,
            event_sha256,
            decision_input_sha256,
            disposition,
            rejection_reasons_json,
            decision_sha256,
            decided_at_text,
        )
        if stored != redundant:
            raise ForwardDecisionLedgerError(
                f"stored forward decision metadata mismatch for {decision_id}"
            )
        try:
            result = StoredForwardDecision(
                decision=decision,
                recorded_at=datetime.fromisoformat(recorded_at_text),
            )
        except Exception as error:
            raise ForwardDecisionLedgerError(
                f"invalid stored forward decision chronology for {decision_id}"
            ) from error
        self._validate_references(result.decision)
        return result

    @staticmethod
    def _select_sql(where: str = "") -> str:
        return f"""
            SELECT decision_id, experiment_id, experiment_version,
                   experiment_sha256, experiment_activated_at,
                   trigger_receipt_no, event_sha256,
                   decision_input_sha256, disposition, rejection_reasons_json,
                   decision_sha256, decision_json, decided_at, recorded_at
            FROM forward_event_decisions
            {where}
        """

    def get(
        self,
        experiment_id: str,
        experiment_version: int,
        trigger_receipt_no: str,
    ) -> StoredForwardDecision | None:
        row = self.conn.execute(
            self._select_sql(
                "WHERE experiment_id = ? AND experiment_version = ? "
                "AND trigger_receipt_no = ?"
            ),
            (experiment_id, experiment_version, trigger_receipt_no),
        ).fetchone()
        return self._load_row(row) if row is not None else None

    def list_for_event(
        self,
        trigger_receipt_no: str,
    ) -> tuple[StoredForwardDecision, ...]:
        rows = self.conn.execute(
            self._select_sql(
                "WHERE trigger_receipt_no = ? "
                "ORDER BY experiment_id, experiment_version"
            ),
            (trigger_receipt_no,),
        ).fetchall()
        return tuple(self._load_row(row) for row in rows)

    def list_for_experiment(
        self,
        experiment_id: str,
    ) -> tuple[StoredForwardDecision, ...]:
        rows = self.conn.execute(
            self._select_sql(
                "WHERE experiment_id = ? "
                "ORDER BY decided_at, trigger_receipt_no, experiment_version"
            ),
            (experiment_id,),
        ).fetchall()
        return tuple(self._load_row(row) for row in rows)

    def record(self, decision: ForwardDecision) -> StoredForwardDecision:
        return self.record_many((decision,))[0]

    def record_many(
        self,
        decisions: Iterable[ForwardDecision],
    ) -> tuple[StoredForwardDecision, ...]:
        proposed = tuple(decisions)
        if not proposed:
            return ()
        canonical: list[tuple[ForwardDecision, str]] = []
        identities: set[tuple[str, int, str]] = set()
        for decision in proposed:
            try:
                decision_json = decision.canonical_json()
                validated = ForwardDecision.model_validate_json(decision_json)
            except Exception as error:
                raise ForwardDecisionLedgerError(
                    "forward decision failed canonical validation"
                ) from error
            if validated != decision:
                raise ForwardDecisionLedgerError(
                    "forward decision differs from its canonical validated form"
                )
            identity = (
                decision.experiment.experiment_id,
                decision.experiment.version,
                decision.inputs.trigger_receipt_no,
            )
            if identity in identities:
                raise ForwardDecisionLedgerError(
                    "decision batch contains duplicate experiment/event identity"
                )
            identities.add(identity)
            self._validate_references(decision)
            canonical.append((decision, decision_json))

        results: list[StoredForwardDecision] = []
        pending: list[tuple[ForwardDecision, str]] = []
        for decision, decision_json in canonical:
            existing = self.get(
                decision.experiment.experiment_id,
                decision.experiment.version,
                decision.inputs.trigger_receipt_no,
            )
            if existing is None:
                pending.append((decision, decision_json))
            elif existing.decision == decision:
                results.append(existing)
            else:
                raise ForwardDecisionLedgerError(
                    "forward decision identity is immutable"
                )

        if not pending:
            by_identity = {
                (
                    item.decision.experiment.experiment_id,
                    item.decision.experiment.version,
                    item.decision.inputs.trigger_receipt_no,
                ): item
                for item in results
            }
            return tuple(
                by_identity[
                    (
                        decision.experiment.experiment_id,
                        decision.experiment.version,
                        decision.inputs.trigger_receipt_no,
                    )
                ]
                for decision, _ in canonical
            )

        recorded_at = self._now()
        if any(recorded_at < item.inputs.assessed_at for item, _ in pending):
            raise ForwardDecisionLedgerError(
                "decision recording cannot precede decision time"
            )

        try:
            with self.conn:
                for decision, decision_json in pending:
                    rejection_reasons_json = json.dumps(
                        [
                            reason.model_dump(mode="json")
                            for reason in decision.rejection_reasons
                        ],
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    self.conn.execute(
                        """
                        INSERT INTO forward_event_decisions (
                            decision_id, experiment_id, experiment_version,
                            experiment_sha256, experiment_activated_at,
                            trigger_receipt_no, event_sha256, decision_input_sha256,
                            disposition,
                            rejection_reasons_json, decision_sha256,
                            decision_json, decided_at, recorded_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            decision.decision_id,
                            decision.experiment.experiment_id,
                            decision.experiment.version,
                            decision.experiment_sha256,
                            decision.experiment_activated_at.isoformat(),
                            decision.inputs.trigger_receipt_no,
                            decision.inputs.event_sha256,
                            decision.decision_input_sha256,
                            decision.disposition,
                            rejection_reasons_json,
                            decision.sha256(),
                            decision_json,
                            decision.inputs.assessed_at.isoformat(),
                            recorded_at.isoformat(),
                        ),
                    )
        except sqlite3.IntegrityError as error:
            raise ForwardDecisionLedgerError(
                "forward decision recording failed"
            ) from error

        by_identity = {
            (
                item.decision.experiment.experiment_id,
                item.decision.experiment.version,
                item.decision.inputs.trigger_receipt_no,
            ): item
            for item in results
        }
        for decision, _ in pending:
            identity = (
                decision.experiment.experiment_id,
                decision.experiment.version,
                decision.inputs.trigger_receipt_no,
            )
            stored = self.get(*identity)
            if stored is None:
                raise ForwardDecisionLedgerError(
                    "recorded forward decision could not be reloaded"
                )
            by_identity[identity] = stored
        return tuple(
            by_identity[
                (
                    decision.experiment.experiment_id,
                    decision.experiment.version,
                    decision.inputs.trigger_receipt_no,
                )
            ]
            for decision, _ in canonical
        )


@dataclass(frozen=True)
class ForwardDecisionConsumer:
    """Live watcher consumer that records every active experiment/event pair."""

    registry: ExperimentRegistry
    ledger: ForwardDecisionLedger
    alert_builder: ResearchAlertBuilder
    consumer_name: str = "forward-decision-ledger-v1"

    def __post_init__(self) -> None:
        if not self.consumer_name.strip():
            raise ValueError("consumer_name must not be empty")
        if self.registry.conn is not self.ledger.conn:
            raise ValueError("decision consumer registry and ledger must share storage")

    def consume(self, envelope: EventEnvelope) -> None:
        normalized_at = envelope.normalized_at
        if normalized_at.tzinfo is None or normalized_at.utcoffset() is None:
            raise ForwardDecisionLedgerError(
                "event normalization time must be timezone-aware"
            )
        decided_at = normalized_at.astimezone(timezone.utc)
        active = self.registry.active_experiments_at(decided_at)
        if not active:
            return
        normalized_envelope = EventEnvelope(
            delivery_id=envelope.delivery_id,
            trigger_receipt_no=envelope.trigger_receipt_no,
            normalized_at=decided_at,
            event=envelope.event,
        )
        alert = self.alert_builder.build(
            normalized_envelope,
            assessed_at=decided_at,
        )
        decisions = []
        for registered in active:
            inputs = _decision_inputs_from_alert(
                alert=alert,
                registered=registered,
            )
            decisions.append(build_forward_decision(registered, inputs))
        self.ledger.record_many(decisions)
