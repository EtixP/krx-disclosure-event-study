from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Callable, Iterable, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kdtb.schemas.experiment import ExperimentSpecification, require_utc


class ExperimentRegistryError(ValueError):
    """Raised when an experiment registry invariant would be violated."""


class RegisteredExperiment(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    specification: ExperimentSpecification
    specification_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    registered_at: datetime
    activated_at: datetime | None = None

    @model_validator(mode="after")
    def registration_is_consistent(self) -> "RegisteredExperiment":
        require_utc(self.registered_at, field="registered_at")
        if self.specification_sha256 != self.specification.sha256():
            raise ValueError("registered specification SHA-256 does not match its JSON")
        if self.registered_at < self.specification.created_at:
            raise ValueError("registration cannot precede specification creation")
        if self.registered_at > self.specification.forward_test_start:
            raise ValueError("registration cannot follow forward-test start")
        if self.activated_at is not None:
            require_utc(self.activated_at, field="activated_at")
            if self.activated_at < self.registered_at:
                raise ValueError("activation cannot precede registration")
            if self.activated_at > self.specification.forward_test_start:
                raise ValueError("activation cannot follow forward-test start")
        return self


class ExperimentRegistry:
    """Append-only registry and deterministic activation-time version resolver."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.conn = conn
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self) -> datetime:
        return require_utc(self.clock(), field="registry clock")

    def _load_row(self, row: sqlite3.Row | tuple) -> RegisteredExperiment:
        (
            experiment_id,
            version,
            stored_sha256,
            stored_json,
            created_at_text,
            historical_cutoff_text,
            forward_test_start_text,
            registered_at_text,
            activation_sha256,
            activated_at_text,
        ) = row
        try:
            specification = ExperimentSpecification.model_validate_json(stored_json)
        except Exception as error:
            raise ExperimentRegistryError(
                f"invalid stored experiment JSON for {experiment_id} v{version}"
            ) from error
        canonical_json = specification.canonical_json()
        if canonical_json != stored_json:
            raise ExperimentRegistryError(
                f"stored experiment JSON is not canonical for {experiment_id} v{version}"
            )
        if (
            specification.experiment_id != experiment_id
            or specification.version != version
        ):
            raise ExperimentRegistryError(
                f"stored experiment identity mismatch for {experiment_id} v{version}"
            )
        stored_chronology = (
            created_at_text,
            historical_cutoff_text,
            forward_test_start_text,
        )
        specification_chronology = (
            specification.created_at.isoformat(),
            specification.historical_cutoff.isoformat(),
            specification.forward_test_start.isoformat(),
        )
        if stored_chronology != specification_chronology:
            raise ExperimentRegistryError(
                f"stored experiment chronology mismatch for {experiment_id} v{version}"
            )
        actual_sha256 = specification.sha256()
        if stored_sha256 != actual_sha256:
            raise ExperimentRegistryError(
                f"stored experiment hash mismatch for {experiment_id} v{version}"
            )
        if activation_sha256 is not None and activation_sha256 != actual_sha256:
            raise ExperimentRegistryError(
                f"activation hash mismatch for {experiment_id} v{version}"
            )
        try:
            registered_at = datetime.fromisoformat(registered_at_text)
            activated_at = (
                datetime.fromisoformat(activated_at_text)
                if activated_at_text is not None
                else None
            )
            return RegisteredExperiment(
                specification=specification,
                specification_sha256=actual_sha256,
                registered_at=registered_at,
                activated_at=activated_at,
            )
        except Exception as error:
            raise ExperimentRegistryError(
                f"invalid registry chronology for {experiment_id} v{version}"
            ) from error

    @staticmethod
    def _validate_version_chain(
        registrations: tuple[RegisteredExperiment, ...],
    ) -> None:
        for expected_version, current in enumerate(registrations, start=1):
            if current.specification.version != expected_version:
                raise ExperimentRegistryError(
                    "stored experiment versions are not contiguous"
                )
            if expected_version == 1:
                continue
            predecessor = registrations[expected_version - 2]
            if (
                current.specification.supersedes_sha256
                != predecessor.specification_sha256
            ):
                raise ExperimentRegistryError("stored experiment hash chain is broken")
            if predecessor.activated_at is None:
                raise ExperimentRegistryError(
                    "stored revision has an unactivated predecessor"
                )
            if current.specification.created_at <= predecessor.activated_at:
                raise ExperimentRegistryError(
                    "stored revision predates predecessor activation"
                )
            if (
                current.specification.forward_test_start
                <= predecessor.specification.forward_test_start
            ):
                raise ExperimentRegistryError(
                    "stored revision forward starts are not increasing"
                )
            if current.activated_at is not None:
                if current.activated_at <= predecessor.activated_at:
                    raise ExperimentRegistryError(
                        "stored activation timestamps are not increasing"
                    )

    def get(self, experiment_id: str, version: int) -> RegisteredExperiment | None:
        for registration in self.list_versions(experiment_id):
            if registration.specification.version == version:
                return registration
        return None

    def list_versions(self, experiment_id: str) -> tuple[RegisteredExperiment, ...]:
        rows = self.conn.execute(
            """
            SELECT s.experiment_id, s.version, s.specification_sha256,
                   s.specification_json, s.created_at, s.historical_cutoff,
                   s.forward_test_start, s.registered_at,
                   a.specification_sha256, a.activated_at
            FROM experiment_specifications AS s
            LEFT JOIN experiment_activations AS a
              ON a.experiment_id = s.experiment_id AND a.version = s.version
            WHERE s.experiment_id = ?
            ORDER BY s.version
            """,
            (experiment_id,),
        ).fetchall()
        registrations = tuple(self._load_row(row) for row in rows)
        self._validate_version_chain(registrations)
        return registrations

    def register(
        self,
        specification: ExperimentSpecification,
    ) -> RegisteredExperiment:
        return self._register_at(specification, registered_at=self._now())

    def _register_at(
        self,
        specification: ExperimentSpecification,
        *,
        registered_at: datetime,
    ) -> RegisteredExperiment:
        try:
            canonical_json = specification.canonical_json()
            validated = ExperimentSpecification.model_validate_json(canonical_json)
        except Exception as error:
            raise ExperimentRegistryError(
                "specification failed canonical validation"
            ) from error
        if validated != specification:
            raise ExperimentRegistryError(
                "specification differs from its canonical validated form"
            )
        specification = validated
        specification_sha256 = specification.sha256()
        existing = self.get(specification.experiment_id, specification.version)
        if existing is not None:
            if (
                existing.specification_sha256 == specification_sha256
                and existing.specification == specification
            ):
                return existing
            raise ExperimentRegistryError(
                "registered experiment versions are immutable"
            )

        registered = require_utc(registered_at, field="registered_at")
        if registered < specification.created_at:
            raise ExperimentRegistryError(
                "registration cannot precede specification creation"
            )
        if registered > specification.forward_test_start:
            raise ExperimentRegistryError(
                "registration cannot follow forward-test start"
            )

        versions = self.list_versions(specification.experiment_id)
        if specification.version != len(versions) + 1:
            raise ExperimentRegistryError("experiment versions must be contiguous")
        if specification.version == 1:
            if versions:
                raise ExperimentRegistryError("experiment version 1 already exists")
        else:
            predecessor = versions[-1]
            if predecessor.activated_at is None:
                raise ExperimentRegistryError(
                    "a predecessor must be activated before it can be revised"
                )
            if specification.supersedes_sha256 != predecessor.specification_sha256:
                raise ExperimentRegistryError(
                    "new version must hash-link its exact predecessor"
                )
            if specification.created_at <= predecessor.specification.created_at:
                raise ExperimentRegistryError(
                    "new version creation must follow its predecessor"
                )
            if specification.created_at <= predecessor.activated_at:
                raise ExperimentRegistryError(
                    "new version must be created after predecessor activation"
                )
            if (
                specification.forward_test_start
                <= predecessor.specification.forward_test_start
            ):
                raise ExperimentRegistryError(
                    "new version forward start must follow its predecessor"
                )

        try:
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO experiment_specifications (
                        experiment_id, version, specification_sha256,
                        specification_json, created_at, historical_cutoff,
                        forward_test_start, registered_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        specification.experiment_id,
                        specification.version,
                        specification_sha256,
                        canonical_json,
                        specification.created_at.isoformat(),
                        specification.historical_cutoff.isoformat(),
                        specification.forward_test_start.isoformat(),
                        registered.isoformat(),
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise ExperimentRegistryError("experiment registration failed") from error
        result = self.get(specification.experiment_id, specification.version)
        if result is None:
            raise ExperimentRegistryError("registered experiment could not be reloaded")
        return result

    def activate(
        self,
        experiment_id: str,
        version: int,
    ) -> RegisteredExperiment:
        registration = self.get(experiment_id, version)
        if registration is None:
            raise ExperimentRegistryError("cannot activate an unregistered experiment")
        if registration.activated_at is not None:
            return registration
        activated = self._now()
        if activated < registration.registered_at:
            raise ExperimentRegistryError("activation cannot precede registration")
        if activated > registration.specification.forward_test_start:
            raise ExperimentRegistryError("activation cannot follow forward-test start")
        activated_versions = [
            item.specification.version
            for item in self.list_versions(experiment_id)
            if item.activated_at is not None
        ]
        if activated_versions and version <= max(activated_versions):
            raise ExperimentRegistryError("versions must activate in increasing order")

        try:
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO experiment_activations (
                        experiment_id, version, specification_sha256, activated_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        experiment_id,
                        version,
                        registration.specification_sha256,
                        activated.isoformat(),
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise ExperimentRegistryError("experiment activation failed") from error
        result = self.get(experiment_id, version)
        if result is None:
            raise ExperimentRegistryError("activated experiment could not be reloaded")
        return result

    def revise(
        self,
        experiment_id: str,
        version: int,
        *,
        forward_test_start: datetime,
        historical_cutoff: datetime | None = None,
        changes: Mapping[str, object] | None = None,
    ) -> RegisteredExperiment:
        predecessor = self.get(experiment_id, version)
        if predecessor is None:
            raise ExperimentRegistryError("cannot revise an unregistered experiment")
        if predecessor.activated_at is None:
            raise ExperimentRegistryError(
                "a specification must be activated before creating a new version"
            )
        created = self._now()
        if created <= predecessor.activated_at:
            raise ExperimentRegistryError(
                "a new version must be created after predecessor activation"
            )
        versions = self.list_versions(experiment_id)
        if not versions or versions[-1].specification.version != version:
            raise ExperimentRegistryError("only the latest version can be revised")
        successor = predecessor.specification.new_version(
            created_at=created,
            forward_test_start=forward_test_start,
            historical_cutoff=historical_cutoff,
            changes=changes,
        )
        return self._register_at(successor, registered_at=created)

    def active_at(
        self,
        experiment_id: str,
        assessed_at: datetime,
    ) -> RegisteredExperiment | None:
        assessed = require_utc(assessed_at, field="assessed_at")
        started = [
            registration
            for registration in self.list_versions(experiment_id)
            if registration.activated_at is not None
            and registration.activated_at <= assessed
            and registration.specification.forward_test_start <= assessed
        ]
        if not started:
            return None
        current = max(started, key=lambda item: item.specification.version)
        if current.specification.evaluation.evaluation_end < assessed:
            return None
        return current

    def active_experiments_at(
        self,
        assessed_at: datetime,
    ) -> tuple[RegisteredExperiment, ...]:
        """Return every active experiment in stable experiment-ID order."""

        assessed = require_utc(assessed_at, field="assessed_at")
        rows = self.conn.execute(
            """
            SELECT DISTINCT experiment_id
            FROM experiment_specifications
            ORDER BY experiment_id
            """
        ).fetchall()
        active = (self.active_at(str(row[0]), assessed) for row in rows)
        return tuple(item for item in active if item is not None)

    def replay(
        self,
        experiment_id: str,
        assessment_times: Iterable[datetime],
    ) -> tuple[RegisteredExperiment | None, ...]:
        return tuple(
            self.active_at(experiment_id, assessed_at)
            for assessed_at in assessment_times
        )
