from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import ClassVar

import psycopg
from pydantic import BaseModel, ConfigDict

from job_finder.evaluation.models import PromptReleaseId
from job_finder.evaluation.prompt_releases import (
    PromptRelease,
    build_prompt_release,
    load_prompt_release,
    store_prompt_release,
)
from job_finder.evaluation.qualification_components import (
    QualificationTargetId,
    ResolvedQualificationTarget,
    resolve_executable_qualification_target,
)
from job_finder.qualification_definition import (
    QualificationDefinition,
    QualificationDefinitionRevisionId,
    qualification_definition_revision_id,
)


class CompiledQualificationTarget(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    target: ResolvedQualificationTarget
    prompt_release: PromptRelease


def _published_definition(
    connection: psycopg.Connection[tuple[object, ...]],
    revision_id: QualificationDefinitionRevisionId,
) -> QualificationDefinition:
    row = connection.execute(
        """
        SELECT revision.content
        FROM qualification_definition_revisions revision
        JOIN qualification_definition_publications publication
          ON publication.revision_id = revision.id
        WHERE revision.id = %s
        """,
        (revision_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Qualification target definition is not published")
    definition = QualificationDefinition.model_validate(row[0])
    if qualification_definition_revision_id(definition) != revision_id:
        raise ValueError("Published qualification definition has invalid identity")
    return definition


def _require_target_prompt_members(
    target: ResolvedQualificationTarget, release: PromptRelease
) -> None:
    expected = (
        *target.relevance.prompt_version_ids,
        target.enrichment.prompt_version_id,
        target.deduplication.prompt_version_id,
    )
    if tuple(version.id for version in release.versions) != expected:
        raise ValueError("Prompt release differs from target component members")
    phases = tuple(version.definition.phase for version in release.versions)
    if not all(phase in ("filter", "profile") for phase in phases[:-2]) or phases[-2:] != (
        "enrichment",
        "deduplication",
    ):
        raise ValueError("Prompt release has invalid qualification phases")


def bind_qualification_prompt_release(
    connection: psycopg.Connection[tuple[object, ...]],
    target_id: QualificationTargetId,
    artifact_path: Path,
    *,
    created_at: datetime,
    created_by: str,
) -> PromptReleaseId:
    target = resolve_executable_qualification_target(connection, target_id, artifact_path)
    definition = _published_definition(
        connection, target.content.qualification_definition_revision_id
    )
    release = build_prompt_release(definition)
    _require_target_prompt_members(target, release)
    _ = store_prompt_release(connection, release, created_at=created_at, created_by=created_by)
    _ = connection.execute(
        """
        INSERT INTO qualification_prompt_compilations (
          target_id, artifact_id, qualification_definition_revision_id,
          prompt_release_id, created_at, created_by
        ) VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (target_id) DO NOTHING
        """,
        (
            target_id,
            target.content.artifact_id,
            target.content.qualification_definition_revision_id,
            release.id,
            created_at,
            created_by,
        ),
    )
    row = connection.execute(
        "SELECT prompt_release_id FROM qualification_prompt_compilations WHERE target_id = %s",
        (target_id,),
    ).fetchone()
    if row is None or row[0] != release.id:
        raise ValueError("Stored qualification prompt compilation differs from its target")
    return release.id


def load_compiled_qualification_target(
    connection: psycopg.Connection[tuple[object, ...]],
    target_id: QualificationTargetId,
    artifact_path: Path,
) -> CompiledQualificationTarget:
    target = resolve_executable_qualification_target(connection, target_id, artifact_path)
    row = connection.execute(
        """
        SELECT artifact_id, qualification_definition_revision_id, prompt_release_id
        FROM qualification_prompt_compilations WHERE target_id = %s
        """,
        (target_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Qualification target has no compiled prompt release")
    if row[0] != target.content.artifact_id or row[1] != (
        target.content.qualification_definition_revision_id
    ):
        raise ValueError("Stored prompt compilation differs from its target")
    release = load_prompt_release(connection, PromptReleaseId(str(row[2])))
    _require_target_prompt_members(target, release)
    definition = _published_definition(
        connection, target.content.qualification_definition_revision_id
    )
    if build_prompt_release(definition) != release:
        raise ValueError("Stored prompt release differs from verified definition compilation")
    return CompiledQualificationTarget(target=target, prompt_release=release)
