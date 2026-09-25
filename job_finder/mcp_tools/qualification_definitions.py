# pyright: reportUnusedFunction=false
from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from job_finder.evaluation.models import PromptReleaseId
from job_finder.evaluation.qualification_components import QualificationTargetId
from job_finder.evaluation.qualification_prompt_compilations import (
    bind_qualification_prompt_release,
)
from job_finder.mcp_tools.common import (
    APPEND_ONLY,
    CAS_WRITE,
    PUBLISH_WRITE,
    READ_ONLY,
    McpDependencies,
)
from job_finder.qualification_definition import (
    QualificationDefinition,
    QualificationDefinitionRevisionId,
)
from job_finder.qualification_definition_service import (
    PublishQualificationDefinitionCommand,
    QualificationDefinitionDraft,
    QualificationDefinitionRevision,
    QualificationDraftChanged,
    QualificationDraftSaved,
    QualificationPublicationReceipt,
    ReplaceQualificationDefinitionDraftCommand,
    get_qualification_definition_draft,
    load_qualification_definition_revision,
    publish_qualification_definition,
    replace_qualification_definition_draft,
)

_RevisionId = Annotated[QualificationDefinitionRevisionId, Field(pattern=r"^[0-9a-f]{64}$")]
_TargetId = Annotated[QualificationTargetId, Field(pattern=r"^[0-9a-f]{64}$")]


def register_qualification_definition_tools(mcp: FastMCP, dependencies: McpDependencies) -> None:
    @mcp.tool(annotations=READ_ONLY)
    def qualification_definition_draft_get() -> QualificationDefinitionDraft:
        """Get the independent qualification definition draft and CAS version."""
        with dependencies.connect() as connection:
            return get_qualification_definition_draft(connection)

    @mcp.tool(annotations=CAS_WRITE)
    def qualification_definition_draft_update(
        expected_base_revision_id: _RevisionId,
        expected_version: Annotated[int, Field(ge=0, le=2**63 - 2)],
        definition: QualificationDefinition,
    ) -> QualificationDraftSaved | QualificationDraftChanged:
        """Replace only the observed qualification definition draft."""
        with dependencies.connect() as connection:
            return replace_qualification_definition_draft(
                connection,
                ReplaceQualificationDefinitionDraftCommand(
                    expected_base_revision_id=expected_base_revision_id,
                    expected_version=expected_version,
                    definition=definition,
                    actor=dependencies.actor,
                    timestamp=dependencies.now(),
                ),
            )

    @mcp.tool(annotations=PUBLISH_WRITE)
    def qualification_definition_publish(
        idempotency_key: Annotated[str, Field(min_length=1, max_length=200)],
        expected_draft_version: Annotated[int, Field(ge=0, le=2**63 - 2)],
        expected_revision_id: _RevisionId,
    ) -> QualificationPublicationReceipt:
        """Publish the exact observed qualification definition and rebase its draft."""
        with dependencies.connect() as connection:
            return publish_qualification_definition(
                connection,
                PublishQualificationDefinitionCommand(
                    idempotency_key=idempotency_key,
                    expected_draft_version=expected_draft_version,
                    expected_revision_id=expected_revision_id,
                    actor=dependencies.actor,
                    timestamp=dependencies.now(),
                ),
            )

    @mcp.tool(annotations=READ_ONLY)
    def qualification_definition_revision_get(
        revision_id: _RevisionId,
    ) -> QualificationDefinitionRevision:
        """Get one immutable qualification definition revision."""
        with dependencies.connect() as connection:
            return load_qualification_definition_revision(connection, revision_id)

    @mcp.tool(annotations=APPEND_ONLY)
    def qualification_candidate_compile(target_id: _TargetId) -> PromptReleaseId:
        """Bind a complete candidate to its verified prompt release for execution."""
        artifact_path = dependencies.implementation_artifact_path
        if artifact_path is None:
            raise ToolError("Executing build artifact is not configured")
        try:
            with dependencies.connect() as connection:
                return bind_qualification_prompt_release(
                    connection,
                    target_id,
                    artifact_path,
                    created_at=dependencies.now(),
                    created_by=dependencies.actor,
                )
        except ValueError as error:
            raise ToolError(str(error)) from error
