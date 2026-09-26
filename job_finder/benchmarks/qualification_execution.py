from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import ClassVar, Literal

import psycopg
from pydantic import BaseModel, ConfigDict, Field

from job_finder.benchmarks.composition_execution import execute_composition_fixture_set
from job_finder.benchmarks.deduplication_execution import execute_deduplication_fixture_set
from job_finder.benchmarks.enrichment_execution import execute_enrichment_fixture_set
from job_finder.benchmarks.input_preparation_execution import execute_input_preparation_fixture_set
from job_finder.benchmarks.qualification_evidence import (
    ExperimentInputId,
    FixtureSetId,
    Phase,
    QualificationEvidenceId,
    RelevanceExperimentInput,
    experiment_input_id,
)
from job_finder.benchmarks.relevance_execution import execute_relevance_experiment
from job_finder.evaluation.implementation_artifacts import (
    ImplementationArtifactId,
    verify_implementation_artifact,
)
from job_finder.evaluation.qualification_components import QualificationTargetId
from job_finder.provider_credentials import ExecutionProviderCredentials

Connection = psycopg.Connection[tuple[object, ...]]
CredentialResolver = Callable[[Connection], ExecutionProviderCredentials]
_DIGEST = r"^[0-9a-f]{64}$"


class QualificationEvidenceExecution(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    idempotency_key: str = Field(min_length=1)
    target_id: QualificationTargetId = Field(pattern=_DIGEST)
    phase: Phase
    input_id: str = Field(pattern=_DIGEST)
    artifact_id: ImplementationArtifactId = Field(pattern=_DIGEST)
    state: Literal["running", "completed", "failed"]
    evidence_id: QualificationEvidenceId | None = None
    failure: str | None = None
    created_at: datetime
    finished_at: datetime | None = None


def execute_qualification_evidence(
    connection: Connection,
    *,
    idempotency_key: str,
    target_id: QualificationTargetId,
    phase: Phase,
    input_id: str,
    artifact_path: Path,
    resolve_credentials: CredentialResolver,
    completed_at: datetime,
    created_by: str,
) -> QualificationEvidenceExecution:
    if not connection.autocommit:
        raise ValueError("Qualification evidence execution requires an autocommit connection")
    if not idempotency_key:
        raise ValueError("Idempotency key is required")
    artifact_id = verify_implementation_artifact(artifact_path).id
    lock_key = f"qualification_evidence_execution:{idempotency_key}"
    connection.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (lock_key,))
    try:
        existing = load_qualification_evidence_execution(connection, idempotency_key)
        if existing is not None:
            if (
                existing.target_id != target_id
                or existing.phase != phase
                or existing.input_id != input_id
                or existing.artifact_id != artifact_id
            ):
                raise ValueError("Idempotency key belongs to a different evidence request")
            if existing.state == "running":
                _finish(
                    connection,
                    idempotency_key,
                    "failed",
                    None,
                    "interrupted_execution",
                    completed_at,
                )
                return _require_execution(connection, idempotency_key)
            return existing

        connection.execute(
            """
            INSERT INTO qualification_evidence_executions (
              idempotency_key, target_id, phase, input_id, artifact_id, state, created_at
            ) VALUES (%s, %s, %s, %s, %s, 'running', %s)
            """,
            (idempotency_key, target_id, phase, input_id, artifact_id, completed_at),
        )
        try:
            evidence_id = _run_phase(
                connection,
                target_id,
                phase,
                input_id,
                artifact_path,
                resolve_credentials,
                completed_at,
                created_by,
            )
        except Exception:
            _finish(connection, idempotency_key, "failed", None, "execution_failed", completed_at)
            return _require_execution(connection, idempotency_key)
        _finish(connection, idempotency_key, "completed", evidence_id, None, completed_at)
        return _require_execution(connection, idempotency_key)
    finally:
        connection.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (lock_key,))


def _run_phase(
    connection: Connection,
    target_id: QualificationTargetId,
    phase: Phase,
    input_id: str,
    artifact_path: Path,
    resolve_credentials: CredentialResolver,
    completed_at: datetime,
    created_by: str,
) -> QualificationEvidenceId:
    if phase == "input_preparation":
        return execute_input_preparation_fixture_set(
            connection,
            target_id,
            FixtureSetId(input_id),
            artifact_path,
            completed_at=completed_at,
            created_by=created_by,
        )
    credentials = resolve_credentials(connection)
    openrouter = credentials.openrouter.get_secret_value()
    if phase == "relevance":
        row = connection.execute(
            "SELECT content FROM relevance_experiment_inputs WHERE id = %s", (input_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Frozen relevance input does not exist")
        frozen = RelevanceExperimentInput.model_validate(row[0])
        if experiment_input_id(frozen) != input_id:
            raise ValueError("Frozen relevance input identity is invalid")
        key = (
            openrouter
            if frozen.provider_settings.provider == "openrouter"
            else credentials.typesafe.get_secret_value()
        )
        return execute_relevance_experiment(
            connection,
            target_id,
            ExperimentInputId(input_id),
            artifact_path,
            api_key=key,
            completed_at=completed_at,
            created_by=created_by,
        )
    if phase == "enrichment":
        return execute_enrichment_fixture_set(
            connection,
            target_id,
            FixtureSetId(input_id),
            artifact_path,
            api_key=openrouter,
            completed_at=completed_at,
            created_by=created_by,
        )
    if phase == "deduplication":
        return execute_deduplication_fixture_set(
            connection,
            target_id,
            FixtureSetId(input_id),
            artifact_path,
            api_key=openrouter,
            completed_at=completed_at,
            created_by=created_by,
        )
    return execute_composition_fixture_set(
        connection,
        target_id,
        FixtureSetId(input_id),
        artifact_path,
        openrouter_api_key=openrouter,
        typesafe_api_key=credentials.typesafe.get_secret_value(),
        completed_at=completed_at,
        created_by=created_by,
    )


def _finish(
    connection: Connection,
    key: str,
    state: Literal["completed", "failed"],
    evidence_id: QualificationEvidenceId | None,
    failure: str | None,
    finished_at: datetime,
) -> None:
    with connection.transaction():
        connection.execute(
            """
            UPDATE qualification_evidence_executions
            SET state = %s, evidence_id = %s, failure = %s, finished_at = %s
            WHERE idempotency_key = %s AND state = 'running'
            """,
            (state, evidence_id, failure, finished_at, key),
        )


def load_qualification_evidence_execution(
    connection: Connection, idempotency_key: str
) -> QualificationEvidenceExecution | None:
    row = connection.execute(
        """
        SELECT idempotency_key, target_id, phase, input_id, artifact_id, state,
               evidence_id, failure, created_at, finished_at
        FROM qualification_evidence_executions WHERE idempotency_key = %s
        """,
        (idempotency_key,),
    ).fetchone()
    if row is None:
        return None
    return QualificationEvidenceExecution.model_validate(
        dict(
            zip(
                (
                    "idempotency_key",
                    "target_id",
                    "phase",
                    "input_id",
                    "artifact_id",
                    "state",
                    "evidence_id",
                    "failure",
                    "created_at",
                    "finished_at",
                ),
                row,
                strict=True,
            )
        )
    )


def _require_execution(connection: Connection, key: str) -> QualificationEvidenceExecution:
    execution = load_qualification_evidence_execution(connection, key)
    if execution is None:
        raise RuntimeError("Qualification evidence execution disappeared")
    return execution
