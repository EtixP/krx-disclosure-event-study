from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from kdtb.experiments import (
    BenchmarkDefinition,
    BenchmarkMapping,
    CostAssumptions,
    EligibilityRule,
    EvaluationPlan,
    EventDefinition,
    ExclusionRule,
    ExecutionRule,
    ExperimentRegistry,
    ExperimentRegistryError,
    ExperimentSpecification,
    FeatureDefinition,
    OutcomeCriterion,
    Predicate,
    RuleParameter,
)
from kdtb.storage.db import init_db

UTC = timezone.utc


def _time(day: int, hour: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, tzinfo=UTC)


class ManualClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _evaluation(end: datetime | None = None) -> EvaluationPlan:
    return EvaluationPlan(
        evaluation_end=end or datetime(2026, 12, 31, tzinfo=UTC),
        primary_metric="mean_abnormal_net_return",
        minimum_events=30,
        minimum_issuers=10,
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
    )


def _specification(
    *,
    experiment_id: str = "supply-contract-forward",
    created_at: datetime | None = None,
    historical_cutoff: datetime | None = None,
    forward_test_start: datetime | None = None,
) -> ExperimentSpecification:
    return ExperimentSpecification(
        experiment_id=experiment_id,
        version=1,
        name="Supply-contract magnitude forward observation",
        objective="Measure a frozen hypothesis prospectively without authorizing trades.",
        created_at=created_at or _time(10, 12),
        historical_cutoff=historical_cutoff or _time(9, 23),
        forward_test_start=forward_test_start or _time(15),
        event_definition=EventDefinition(
            event_types=("major_supply_contract",),
            markets=("KOSDAQ", "KOSPI"),
            event_statuses=("active",),
            lineage_statuses=("complete", "self_contained"),
            normalization_version="m1.1-v1",
        ),
        features=(
            FeatureDefinition(
                name="contract_to_revenue_ratio",
                source="significance",
                source_version="m1.3-v1",
                field_path="measurements[0].value",
                value_type="number",
                missing_policy="exclude",
            ),
            FeatureDefinition(
                name="historical_mean_abnormal_return",
                source="historical_context",
                source_version="m1.4-v1",
                field_path="metrics.mean_abnormal_net_return",
                value_type="number",
                missing_policy="exclude",
            ),
        ),
        benchmark=BenchmarkDefinition(
            benchmark_id="broad_market_price_index",
            source="NAVER_FINANCE_DOMESTIC_INDEX_DAILY",
            source_version="m0.3-v1",
            market_mappings=(
                BenchmarkMapping(market="KOSDAQ", symbol="KOSDAQ"),
                BenchmarkMapping(market="KOSPI", symbol="KOSPI"),
            ),
            alignment="exact_trading_date",
            return_type="price_return",
            missing_data_policy="fail",
        ),
        eligibility_rules=(
            EligibilityRule(
                rule_id="eligible_market",
                predicate=Predicate(
                    field_path="event.market",
                    operator="in",
                    value=("KOSDAQ", "KOSPI"),
                ),
                on_missing="fail",
            ),
            EligibilityRule(
                rule_id="minimum_contract_ratio",
                predicate=Predicate(
                    field_path="contract_to_revenue_ratio",
                    operator="gte",
                    value=0.1,
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
        exclusions=(
            ExclusionRule(
                code="INCOMPLETE_CONTEXT",
                predicate=Predicate(
                    field_path="historical_context.status",
                    operator="ne",
                    value="available",
                ),
                reason="Historical context must be complete at decision time.",
            ),
        ),
        evaluation=_evaluation(),
    )


@pytest.fixture
def registry(tmp_path):
    conn = init_db(tmp_path / "experiments.db")
    clock = ManualClock(_time(10, 13))
    try:
        yield ExperimentRegistry(conn, clock=clock), conn, clock
    finally:
        conn.close()


def test_specification_is_complete_canonical_hashable_and_deeply_frozen():
    specification = _specification()
    canonical = specification.canonical_json()
    restored = ExperimentSpecification.model_validate_json(canonical)

    assert restored == specification
    assert restored.canonical_json() == canonical
    assert restored.sha256() == specification.sha256()
    assert len(specification.sha256()) == 64
    payload = json.loads(canonical)
    assert payload["specification_schema_version"] == "m2.1-v1"
    assert payload["historical_cutoff"] == "2026-09-09T23:00:00Z"
    assert payload["forward_test_start"] == "2026-09-15T00:00:00Z"
    assert payload["entry_rule"]["rule_id"] == "next_trading_day_close"
    assert payload["evaluation"]["success_criteria"]
    assert payload["evaluation"]["failure_criteria"]
    assert payload["evaluation"]["success_criteria_policy"] == "all"
    assert payload["evaluation"]["failure_criteria_policy"] == "any"
    assert payload["evaluation"]["simultaneous_match_policy"] == "failure"

    with pytest.raises(ValidationError, match="frozen"):
        specification.name = "mutated"
    with pytest.raises(ValidationError, match="frozen"):
        specification.entry_rule.rule_id = "mutated"


def test_hash_changes_when_any_material_rule_changes():
    specification = _specification()
    payload = specification.model_dump(mode="python")
    payload["objective"] = "A materially different prospective objective."
    changed = ExperimentSpecification.model_validate(payload)

    assert changed.sha256() != specification.sha256()


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"historical_cutoff": _time(11)}, "historical cutoff"),
        ({"created_at": _time(16)}, "created before forward testing"),
        (
            {"evaluation": _evaluation(_time(14))},
            "evaluation end",
        ),
        (
            {
                "created_at": datetime(
                    2026, 9, 10, 21, tzinfo=timezone(timedelta(hours=9))
                )
            },
            "must use UTC",
        ),
    ],
)
def test_specification_rejects_nonprospective_chronology(updates, message):
    payload = _specification().model_dump(mode="python")
    payload.update(updates)

    with pytest.raises(ValidationError, match=message):
        ExperimentSpecification.model_validate(payload)


def test_semantic_sets_must_be_unique_and_deterministically_ordered():
    payload = _specification().model_dump(mode="python")
    payload["features"] = tuple(reversed(payload["features"]))
    with pytest.raises(ValidationError, match="deterministic sorted order"):
        ExperimentSpecification.model_validate(payload)

    payload = _specification().model_dump(mode="python")
    payload["event_definition"]["markets"] = ("KOSPI", "KOSPI")
    with pytest.raises(ValidationError, match="duplicates"):
        ExperimentSpecification.model_validate(payload)


def test_evaluation_semantics_are_frozen_and_identical_conditions_are_rejected():
    payload = _evaluation().model_dump(mode="python")
    payload["failure_criteria"] = payload["success_criteria"]

    with pytest.raises(ValidationError, match="identical conditions"):
        EvaluationPlan.model_validate(payload)

    payload = _evaluation().model_dump(mode="python")
    payload["simultaneous_match_policy"] = "success"
    with pytest.raises(ValidationError, match="Input should be 'failure'"):
        EvaluationPlan.model_validate(payload)


def test_registration_activation_and_replay_survive_reopen(tmp_path):
    database = tmp_path / "replay.db"
    conn = init_db(database)
    specification = _specification()
    clock = ManualClock(_time(10, 13))
    registry = ExperimentRegistry(conn, clock=clock)
    registered = registry.register(specification)
    clock.value = _time(14)
    activated = registry.activate(specification.experiment_id, 1)

    assert registered.activated_at is None
    assert registered.registered_at == _time(10, 13)
    assert activated.activated_at == _time(14)
    assert activated.specification_sha256 == specification.sha256()
    assert registry.active_at(specification.experiment_id, _time(14, 12)) is None
    assert (
        registry.active_at(specification.experiment_id, _time(15)).specification
        == specification
    )
    expected_versions = (None, 1, 1, None)
    assessment_times = (
        _time(14, 12),
        _time(15),
        datetime(2026, 12, 31, tzinfo=UTC),
        datetime(2027, 1, 1, tzinfo=UTC),
    )
    assert (
        tuple(
            item.specification.version if item is not None else None
            for item in registry.replay(specification.experiment_id, assessment_times)
        )
        == expected_versions
    )
    conn.close()

    reopened = init_db(database)
    try:
        replayed = ExperimentRegistry(reopened).replay(
            specification.experiment_id,
            assessment_times,
        )
        assert (
            tuple(
                item.specification.version if item is not None else None
                for item in replayed
            )
            == expected_versions
        )
        assert (
            replayed[1].specification.canonical_json() == specification.canonical_json()
        )
        assert replayed[1].specification_sha256 == specification.sha256()
    finally:
        reopened.close()


def test_registration_is_idempotent_but_existing_version_cannot_change(registry):
    experiment_registry, _, clock = registry
    specification = _specification()
    first = experiment_registry.register(specification)
    clock.value = _time(11)
    repeated = experiment_registry.register(specification)
    payload = specification.model_dump(mode="python")
    payload["objective"] = "A replacement under the same version."
    replacement = ExperimentSpecification.model_validate(payload)

    assert repeated == first
    with pytest.raises(ExperimentRegistryError, match="immutable"):
        experiment_registry.register(replacement)

    forged = specification.model_copy(
        update={"experiment_id": "forged-specification", "features": ()}
    )
    with pytest.raises(ExperimentRegistryError, match="canonical validation"):
        experiment_registry.register(forged)


def test_identical_registration_retry_is_idempotent_after_forward_start(registry):
    experiment_registry, _, clock = registry
    specification = _specification()
    first = experiment_registry.register(specification)
    clock.value = _time(16)

    assert experiment_registry.register(specification) == first

    payload = specification.model_dump(mode="python")
    payload["objective"] = "A late replacement under the same version."
    replacement = ExperimentSpecification.model_validate(payload)
    with pytest.raises(ExperimentRegistryError, match="immutable"):
        experiment_registry.register(replacement)


@pytest.mark.parametrize(
    ("activated_at", "message"),
    [
        (_time(10, 12), "precede registration"),
        (_time(16), "follow forward-test start"),
    ],
)
def test_activation_must_precede_forward_observation(registry, activated_at, message):
    experiment_registry, _, clock = registry
    specification = _specification()
    experiment_registry.register(specification)
    clock.value = activated_at

    with pytest.raises(ExperimentRegistryError, match=message):
        experiment_registry.activate(specification.experiment_id, 1)


def test_new_registration_cannot_be_backdated_after_forward_start(registry):
    experiment_registry, _, clock = registry
    clock.value = _time(16)

    with pytest.raises(ExperimentRegistryError, match="follow forward-test start"):
        experiment_registry.register(_specification())


def test_foreign_keys_reject_orphan_activation_before_registration(registry):
    experiment_registry, conn, _ = registry
    specification = _specification(experiment_id="orphan-activation")

    assert conn.execute("PRAGMA foreign_keys").fetchone() == (1,)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        with conn:
            conn.execute(
                """
                INSERT INTO experiment_activations (
                    experiment_id,
                    version,
                    specification_sha256,
                    activated_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    specification.experiment_id,
                    specification.version,
                    specification.sha256(),
                    _time(14).isoformat(),
                ),
            )

    registered = experiment_registry.register(specification)
    assert registered.activated_at is None
    assert experiment_registry.active_at(specification.experiment_id, _time(15)) is None


def test_revision_creates_hash_linked_version_without_changing_history(registry):
    experiment_registry, _, clock = registry
    original = _specification()
    experiment_registry.register(original)
    clock.value = _time(14)
    experiment_registry.activate(original.experiment_id, 1)
    clock.value = _time(20)
    revised = experiment_registry.revise(
        original.experiment_id,
        1,
        historical_cutoff=_time(19),
        forward_test_start=datetime(2026, 10, 1, tzinfo=UTC),
        changes={
            "name": "Supply-contract magnitude forward observation revision",
            "evaluation": _evaluation(datetime(2026, 10, 31, tzinfo=UTC)),
        },
    )

    assert revised.specification.version == 2
    assert revised.specification.supersedes_sha256 == original.sha256()
    assert revised.activated_at is None
    assert experiment_registry.get(original.experiment_id, 1).specification == original

    clock.value = _time(25)
    experiment_registry.activate(original.experiment_id, 2)
    assert (
        experiment_registry.active_at(
            original.experiment_id, _time(30)
        ).specification.version
        == 1
    )
    assert (
        experiment_registry.active_at(
            original.experiment_id,
            datetime(2026, 10, 1, tzinfo=UTC),
        ).specification.version
        == 2
    )
    assert (
        experiment_registry.active_at(
            original.experiment_id,
            datetime(2026, 11, 1, tzinfo=UTC),
        )
        is None
    )


def test_unactivated_or_nonlatest_version_cannot_be_revised(registry):
    experiment_registry, _, clock = registry
    original = _specification()
    experiment_registry.register(original)
    clock.value = _time(20)
    with pytest.raises(ExperimentRegistryError, match="activated"):
        experiment_registry.revise(
            original.experiment_id,
            1,
            forward_test_start=datetime(2026, 10, 1, tzinfo=UTC),
        )

    clock.value = _time(14)
    experiment_registry.activate(original.experiment_id, 1)
    clock.value = _time(20)
    experiment_registry.revise(
        original.experiment_id,
        1,
        forward_test_start=datetime(2026, 10, 1, tzinfo=UTC),
    )
    clock.value = _time(21)
    with pytest.raises(ExperimentRegistryError, match="latest"):
        experiment_registry.revise(
            original.experiment_id,
            1,
            forward_test_start=datetime(2026, 11, 1, tzinfo=UTC),
        )


def test_manual_revision_must_be_contiguous_activated_and_hash_linked(registry):
    experiment_registry, _, clock = registry
    original = _specification()
    experiment_registry.register(original)
    successor = original.new_version(
        created_at=_time(20),
        forward_test_start=datetime(2026, 10, 1, tzinfo=UTC),
    )

    clock.value = _time(20, 1)
    with pytest.raises(ExperimentRegistryError, match="predecessor must be activated"):
        experiment_registry.register(successor)

    clock.value = _time(14)
    experiment_registry.activate(original.experiment_id, 1)
    clock.value = _time(20, 1)
    payload = successor.model_dump(mode="python")
    payload["supersedes_sha256"] = "f" * 64
    wrong_link = ExperimentSpecification.model_validate(payload)
    with pytest.raises(ExperimentRegistryError, match="hash-link"):
        experiment_registry.register(wrong_link)

    payload["version"] = 3
    skipped = ExperimentSpecification.model_validate(payload)
    with pytest.raises(ExperimentRegistryError, match="contiguous"):
        experiment_registry.register(skipped)


def test_database_blocks_specification_and_activation_rewrites(registry):
    experiment_registry, conn, clock = registry
    specification = _specification()
    experiment_registry.register(specification)
    clock.value = _time(14)
    experiment_registry.activate(specification.experiment_id, 1)

    with pytest.raises(sqlite3.IntegrityError, match="specifications are immutable"):
        with conn:
            conn.execute(
                "UPDATE experiment_specifications SET specification_json = '{}'"
            )
    with pytest.raises(sqlite3.IntegrityError, match="specifications are immutable"):
        with conn:
            conn.execute("DELETE FROM experiment_specifications")
    with pytest.raises(sqlite3.IntegrityError, match="activations are immutable"):
        with conn:
            conn.execute(
                "UPDATE experiment_activations SET activated_at = ?",
                (_time(13).isoformat(),),
            )


@pytest.mark.parametrize("corruption", ["hash", "chronology"])
def test_registry_rejects_internally_inconsistent_persisted_rows(registry, corruption):
    experiment_registry, conn, _ = registry
    specification = _specification(experiment_id=f"corrupt-{corruption}")
    specification_hash = specification.sha256()
    created_at = specification.created_at.isoformat()
    if corruption == "hash":
        specification_hash = "0" * 64
    else:
        created_at = _time(8).isoformat()
    with conn:
        conn.execute(
            """
            INSERT INTO experiment_specifications (
                experiment_id, version, specification_sha256,
                specification_json, created_at, historical_cutoff,
                forward_test_start, registered_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                specification.experiment_id,
                1,
                specification_hash,
                specification.canonical_json(),
                created_at,
                specification.historical_cutoff.isoformat(),
                specification.forward_test_start.isoformat(),
                _time(10, 13).isoformat(),
            ),
        )

    with pytest.raises(
        ExperimentRegistryError, match=f"stored experiment {corruption}"
    ):
        experiment_registry.get(specification.experiment_id, 1)


def test_registry_rejects_rehashed_but_broken_persisted_version_chain(registry):
    experiment_registry, conn, clock = registry
    original = _specification()
    experiment_registry.register(original)
    clock.value = _time(14)
    experiment_registry.activate(original.experiment_id, 1)
    successor = original.new_version(
        created_at=_time(20),
        forward_test_start=datetime(2026, 10, 1, tzinfo=UTC),
    )
    payload = successor.model_dump(mode="python")
    payload["supersedes_sha256"] = "f" * 64
    broken = ExperimentSpecification.model_validate(payload)
    with conn:
        conn.execute(
            """
            INSERT INTO experiment_specifications (
                experiment_id, version, specification_sha256,
                specification_json, created_at, historical_cutoff,
                forward_test_start, registered_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                broken.experiment_id,
                broken.version,
                broken.sha256(),
                broken.canonical_json(),
                broken.created_at.isoformat(),
                broken.historical_cutoff.isoformat(),
                broken.forward_test_start.isoformat(),
                _time(20, 1).isoformat(),
            ),
        )

    with pytest.raises(ExperimentRegistryError, match="hash chain is broken"):
        experiment_registry.list_versions(original.experiment_id)
