from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Annotated, ClassVar

import psycopg
from pydantic import BaseModel, ConfigDict, Field

from job_finder.acquisition_policy import AcquisitionPolicyRevisionId
from job_finder.acquisition_policy_service import load_acquisition_policy_revision
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
from job_finder.evaluation.prompt_releases import build_prompt_release
from job_finder.evaluation.qualification_prompt_compilations import (
    bind_qualification_prompt_release,
)
from job_finder.evaluation.models import RelevanceReleaseId
from job_finder.qualification_definition import QualificationDefinitionRevisionId
from job_finder.qualification_definition_service import load_qualification_definition_revision

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


class CreateCurrentQualificationCandidateCommand(_Model):
    actor: Annotated[str, Field(min_length=1, max_length=200)]
    timestamp: datetime


def create_current_qualification_candidate(
    connection: _Connection,
    command: CreateCurrentQualificationCandidateCommand,
    artifact_path: Path,
) -> ResolvedQualificationTarget:
    if not connection.autocommit:
        raise ValueError("Qualification candidate creation requires autocommit")
    artifact = verify_implementation_artifact(artifact_path)
    with connection.transaction():
        row = connection.execute(
            """
            SELECT acquisition.revision_id, definition.base_revision_id,
                   legacy.relevance_release_id
            FROM active_acquisition_policy acquisition
            CROSS JOIN qualification_definition_drafts definition
            CROSS JOIN active_release_target legacy
            WHERE acquisition.singleton_id = 1 AND definition.singleton_id = 1
              AND legacy.singleton_id = 1
            FOR SHARE OF acquisition, definition, legacy
            """
        ).fetchone()
        if row is None:
            raise ValueError("Current candidate inputs are missing")
        policy = load_acquisition_policy_revision(
            connection, AcquisitionPolicyRevisionId(str(row[0]))
        ).policy
        definition_id = QualificationDefinitionRevisionId(str(row[1]))
        definition = load_qualification_definition_revision(connection, definition_id).definition
        release = build_prompt_release(definition)
        relevance_versions = tuple(
            version.id
            for version in release.versions
            if version.definition.phase in ("filter", "profile")
        )
        enrichment_versions = tuple(
            version for version in release.versions if version.definition.phase == "enrichment"
        )
        deduplication_versions = tuple(
            version for version in release.versions if version.definition.phase == "deduplication"
        )
        if len(enrichment_versions) != 1 or len(deduplication_versions) != 1:
            raise ValueError("Published definition has invalid prompt phases")
        enrichment = enrichment_versions[0]
        output_schema = json.dumps(
            enrichment.output_schema,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        target = create_qualification_candidate(
            connection,
            CreateQualificationCandidateCommand(
                input_preparation=InputPreparationContent(
                    artifact_id=artifact.id, ats_sources=policy.enabled_sources
                ),
                relevance=RelevanceContent(
                    artifact_id=artifact.id,
                    qualification_definition_revision_id=definition_id,
                    relevance_release_id=RelevanceReleaseId(str(row[2])),
                    prompt_version_ids=relevance_versions,
                ),
                enrichment=EnrichmentContent(
                    artifact_id=artifact.id,
                    prompt_version_id=enrichment.id,
                    output_schema_digest=hashlib.sha256(output_schema.encode()).hexdigest(),
                ),
                deduplication=DeduplicationContent(
                    artifact_id=artifact.id,
                    prompt_version_id=deduplication_versions[0].id,
                ),
                actor=command.actor,
                timestamp=command.timestamp,
            ),
            artifact_path,
        )
        _ = bind_qualification_prompt_release(
            connection,
            target.id,
            artifact_path,
            created_at=command.timestamp,
            created_by=command.actor,
        )
        return target


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
