from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Annotated, ClassVar, Literal, NewType

import psycopg
from pydantic import BaseModel, ConfigDict, Field
from psycopg.types.json import Jsonb

from job_finder.discovery.catalog import SupportedSearchSource
from job_finder.evaluation.implementation_artifacts import ImplementationArtifactId
from job_finder.evaluation.models import PromptVersionId, RelevanceReleaseId
from job_finder.qualification_definition import QualificationDefinitionRevisionId

ComponentReleaseId = NewType("ComponentReleaseId", str)
QualificationTargetId = NewType("QualificationTargetId", str)
_DIGEST = r"^[0-9a-f]{64}$"


class ComponentModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class InputPreparationContent(ComponentModel):
    kind: Literal["input_preparation"] = "input_preparation"
    schema_version: Literal[1] = 1
    artifact_id: Annotated[ImplementationArtifactId, Field(pattern=_DIGEST)]
    ats_sources: tuple[SupportedSearchSource, ...] = Field(min_length=1)
    contract: Literal["ats-adapter-parser-structural-v1"] = "ats-adapter-parser-structural-v1"


class RelevanceContent(ComponentModel):
    kind: Literal["relevance"] = "relevance"
    schema_version: Literal[1] = 1
    artifact_id: Annotated[ImplementationArtifactId, Field(pattern=_DIGEST)]
    qualification_definition_revision_id: Annotated[
        QualificationDefinitionRevisionId, Field(pattern=_DIGEST)
    ]
    relevance_release_id: Annotated[RelevanceReleaseId, Field(pattern=_DIGEST)]
    prompt_version_ids: tuple[PromptVersionId, ...] = Field(min_length=1)
    contract: Literal["filter-profile-composition-v1"] = "filter-profile-composition-v1"


class EnrichmentContent(ComponentModel):
    kind: Literal["enrichment"] = "enrichment"
    schema_version: Literal[1] = 1
    artifact_id: Annotated[ImplementationArtifactId, Field(pattern=_DIGEST)]
    prompt_version_id: Annotated[PromptVersionId, Field(pattern=_DIGEST)]
    output_schema_digest: str = Field(pattern=_DIGEST)
    contract: Literal["canonical-job-fields-v1"] = "canonical-job-fields-v1"


class DeduplicationContent(ComponentModel):
    kind: Literal["deduplication"] = "deduplication"
    schema_version: Literal[1] = 1
    artifact_id: Annotated[ImplementationArtifactId, Field(pattern=_DIGEST)]
    prompt_version_id: Annotated[PromptVersionId, Field(pattern=_DIGEST)]
    contract: Literal["company-title-ledger-fallback-v1"] = "company-title-ledger-fallback-v1"


ComponentContent = (
    InputPreparationContent | RelevanceContent | EnrichmentContent | DeduplicationContent
)


class QualificationTargetContent(ComponentModel):
    schema_version: Literal[1] = 1
    artifact_id: Annotated[ImplementationArtifactId, Field(pattern=_DIGEST)]
    qualification_definition_revision_id: Annotated[
        QualificationDefinitionRevisionId, Field(pattern=_DIGEST)
    ]
    input_preparation_release_id: Annotated[ComponentReleaseId, Field(pattern=_DIGEST)]
    relevance_release_id: Annotated[ComponentReleaseId, Field(pattern=_DIGEST)]
    enrichment_release_id: Annotated[ComponentReleaseId, Field(pattern=_DIGEST)]
    deduplication_release_id: Annotated[ComponentReleaseId, Field(pattern=_DIGEST)]
    composition_contract: Literal["prepared-relevant-enriched-deduplicated-v1"] = (
        "prepared-relevant-enriched-deduplicated-v1"
    )


def component_release_id(content: ComponentContent) -> ComponentReleaseId:
    return ComponentReleaseId(_content_digest(content))


def qualification_target_id(content: QualificationTargetContent) -> QualificationTargetId:
    return QualificationTargetId(_content_digest(content))


def build_qualification_target(
    input_preparation: InputPreparationContent,
    relevance: RelevanceContent,
    enrichment: EnrichmentContent,
    deduplication: DeduplicationContent,
) -> QualificationTargetContent:
    artifacts = {
        input_preparation.artifact_id,
        relevance.artifact_id,
        enrichment.artifact_id,
        deduplication.artifact_id,
    }
    if len(artifacts) != 1:
        raise ValueError("Qualification components must share one implementation artifact")
    return QualificationTargetContent(
        artifact_id=input_preparation.artifact_id,
        qualification_definition_revision_id=relevance.qualification_definition_revision_id,
        input_preparation_release_id=component_release_id(input_preparation),
        relevance_release_id=component_release_id(relevance),
        enrichment_release_id=component_release_id(enrichment),
        deduplication_release_id=component_release_id(deduplication),
    )


def _content_digest(content: ComponentModel) -> str:
    encoded = json.dumps(
        content.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def store_component_release(
    connection: psycopg.Connection[tuple[object, ...]],
    content: ComponentContent,
    *,
    created_at: datetime,
    created_by: str,
) -> ComponentReleaseId:
    release_id = component_release_id(content)
    _ = connection.execute(
        """
        INSERT INTO qualification_component_releases (
          id, kind, artifact_id, content, created_at, created_by
        ) VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (id) DO NOTHING
        """,
        (
            release_id,
            content.kind,
            content.artifact_id,
            Jsonb(content.model_dump(mode="json")),
            created_at,
            created_by,
        ),
    )
    row = connection.execute(
        "SELECT content FROM qualification_component_releases WHERE id = %s", (release_id,)
    ).fetchone()
    if row is None or row[0] != content.model_dump(mode="json"):
        raise ValueError("Stored qualification component differs from its identity")
    return release_id


def store_qualification_target(
    connection: psycopg.Connection[tuple[object, ...]],
    content: QualificationTargetContent,
    *,
    created_at: datetime,
    created_by: str,
) -> QualificationTargetId:
    target_id = qualification_target_id(content)
    _ = connection.execute(
        """
        INSERT INTO qualification_targets (
          id, artifact_id, qualification_definition_revision_id,
          input_preparation_release_id, relevance_release_id,
          enrichment_release_id, deduplication_release_id,
          content, created_at, created_by
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (id) DO NOTHING
        """,
        (
            target_id,
            content.artifact_id,
            content.qualification_definition_revision_id,
            content.input_preparation_release_id,
            content.relevance_release_id,
            content.enrichment_release_id,
            content.deduplication_release_id,
            Jsonb(content.model_dump(mode="json")),
            created_at,
            created_by,
        ),
    )
    row = connection.execute(
        "SELECT content FROM qualification_targets WHERE id = %s", (target_id,)
    ).fetchone()
    if row is None or row[0] != content.model_dump(mode="json"):
        raise ValueError("Stored qualification target differs from its identity")
    return target_id
