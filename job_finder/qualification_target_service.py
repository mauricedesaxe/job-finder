from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated, ClassVar

import psycopg
from pydantic import BaseModel, ConfigDict, Field

from job_finder.benchmarks.qualification_activation import (
    ActiveQualificationTarget,
    get_active_qualification_target,
)
from job_finder.evaluation.implementation_artifacts import (
    store_implementation_artifact,
    verify_implementation_artifact,
)
from job_finder.evaluation.qualification_components import (
    DeduplicationContent,
    EnrichmentContent,
    InputPreparationContent,
    QualificationTargetId,
    RelevanceContent,
    ResolvedQualificationTarget,
    build_qualification_target,
    load_qualification_target,
    resolve_executable_qualification_target,
    store_component_release,
    store_qualification_target,
)

_Connection = psycopg.Connection[tuple[object, ...]]


class _Model(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class CreateQualificationCandidateCommand(_Model):
    input_preparation: InputPreparationContent
    relevance: RelevanceContent
    enrichment: EnrichmentContent
    deduplication: DeduplicationContent
    actor: Annotated[str, Field(min_length=1, max_length=200)]
    timestamp: datetime


def create_qualification_candidate(
    connection: _Connection,
    command: CreateQualificationCandidateCommand,
    artifact_path: Path,
) -> ResolvedQualificationTarget:
    if not connection.autocommit:
        raise ValueError("Qualification candidate creation requires autocommit")
    artifact = verify_implementation_artifact(artifact_path)
    components = (
        command.input_preparation,
        command.relevance,
        command.enrichment,
        command.deduplication,
    )
    if any(component.artifact_id != artifact.id for component in components):
        raise ValueError("Qualification candidate must use the executing build artifact")
    with connection.transaction():
        _ = store_implementation_artifact(
            connection, artifact, created_at=command.timestamp, created_by=command.actor
        )
        for component in components:
            _ = store_component_release(
                connection, component, created_at=command.timestamp, created_by=command.actor
            )
        target = build_qualification_target(*components)
        target_id = store_qualification_target(
            connection, target, created_at=command.timestamp, created_by=command.actor
        )
        return resolve_executable_qualification_target(connection, target_id, artifact_path)


def get_qualification_candidate(
    connection: _Connection, target_id: QualificationTargetId
) -> ResolvedQualificationTarget:
    return load_qualification_target(connection, target_id)


def get_active_qualification_authority(connection: _Connection) -> ActiveQualificationTarget:
    return get_active_qualification_target(connection)
