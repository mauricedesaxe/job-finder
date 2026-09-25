# pyright: reportUnusedFunction=false
from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from job_finder.acquisition_policy import AcquisitionPolicy, AcquisitionPolicyRevisionId
from job_finder.acquisition_policy_activation import (
    ActivateAcquisitionPolicyCommand,
    AcquisitionActivationError,
    AcquisitionActivationReceipt,
    activate_acquisition_policy,
)
from job_finder.acquisition_policy_service import (
    AcquisitionDraftChanged,
    AcquisitionDraftSaved,
    AcquisitionPolicyDraft,
    AcquisitionPolicyRevision,
    AcquisitionPolicyServiceError,
    AcquisitionPublicationReceipt,
    ActiveAcquisitionPolicy,
    PublishAcquisitionPolicyCommand,
    ReplaceAcquisitionPolicyDraftCommand,
    get_acquisition_policy_draft,
    get_active_acquisition_policy,
    load_acquisition_policy_revision,
    publish_acquisition_policy,
    replace_acquisition_policy_draft,
)
from job_finder.mcp_tools.common import CAS_WRITE, PUBLISH_WRITE, READ_ONLY, McpDependencies

_RevisionId = Annotated[AcquisitionPolicyRevisionId, Field(pattern=r"^[0-9a-f]{64}$")]


def register_acquisition_tools(mcp: FastMCP, dependencies: McpDependencies) -> None:
    @mcp.tool(annotations=READ_ONLY)
    def acquisition_active_get() -> ActiveAcquisitionPolicy:
        """Get the independent active acquisition policy and its CAS generation."""
        try:
            with dependencies.connect() as connection:
                return get_active_acquisition_policy(connection)
        except AcquisitionPolicyServiceError as error:
            raise ToolError(str(error)) from error

    @mcp.tool(annotations=READ_ONLY)
    def acquisition_draft_get() -> AcquisitionPolicyDraft:
        """Get the acquisition policy draft and its optimistic version."""
        try:
            with dependencies.connect() as connection:
                return get_acquisition_policy_draft(connection)
        except AcquisitionPolicyServiceError as error:
            raise ToolError(str(error)) from error

    @mcp.tool(annotations=READ_ONLY)
    def acquisition_revision_get(revision_id: _RevisionId) -> AcquisitionPolicyRevision:
        """Get one immutable acquisition policy revision."""
        try:
            with dependencies.connect() as connection:
                return load_acquisition_policy_revision(connection, revision_id)
        except AcquisitionPolicyServiceError as error:
            raise ToolError(str(error)) from error

    @mcp.tool(annotations=CAS_WRITE)
    def acquisition_draft_update(
        expected_base_revision_id: _RevisionId,
        expected_version: Annotated[int, Field(ge=0, le=2**63 - 2)],
        policy: AcquisitionPolicy,
    ) -> AcquisitionDraftSaved | AcquisitionDraftChanged:
        """Replace only the observed acquisition draft, leaving qualification untouched."""
        try:
            with dependencies.connect() as connection:
                return replace_acquisition_policy_draft(
                    connection,
                    ReplaceAcquisitionPolicyDraftCommand(
                        expected_base_revision_id=expected_base_revision_id,
                        expected_version=expected_version,
                        policy=policy,
                        actor=dependencies.actor,
                        timestamp=dependencies.now(),
                    ),
                )
        except AcquisitionPolicyServiceError as error:
            raise ToolError(str(error)) from error

    @mcp.tool(annotations=PUBLISH_WRITE)
    def acquisition_publish(
        idempotency_key: Annotated[str, Field(min_length=1, max_length=200)],
        expected_draft_version: Annotated[int, Field(ge=0, le=2**63 - 2)],
        expected_revision_id: _RevisionId,
    ) -> AcquisitionPublicationReceipt:
        """Publish the observed acquisition draft with an idempotent receipt."""
        try:
            with dependencies.connect() as connection:
                return publish_acquisition_policy(
                    connection,
                    PublishAcquisitionPolicyCommand(
                        idempotency_key=idempotency_key,
                        expected_draft_version=expected_draft_version,
                        expected_revision_id=expected_revision_id,
                        actor=dependencies.actor,
                        timestamp=dependencies.now(),
                    ),
                )
        except AcquisitionPolicyServiceError as error:
            raise ToolError(str(error)) from error

    @mcp.tool(annotations=CAS_WRITE)
    def acquisition_activate(
        idempotency_key: Annotated[str, Field(min_length=1, max_length=200)],
        candidate_revision_id: _RevisionId,
        expected_revision_id: _RevisionId,
        expected_generation: Annotated[int, Field(ge=0, le=2**63 - 1)],
    ) -> AcquisitionActivationReceipt:
        """Activate a published acquisition policy if active authority still matches."""
        try:
            with dependencies.connect() as connection:
                return activate_acquisition_policy(
                    connection,
                    ActivateAcquisitionPolicyCommand(
                        idempotency_key=idempotency_key,
                        candidate_revision_id=candidate_revision_id,
                        expected_revision_id=expected_revision_id,
                        expected_generation=expected_generation,
                        actor=dependencies.actor,
                        timestamp=dependencies.now(),
                    ),
                )
        except AcquisitionActivationError as error:
            raise ToolError(str(error)) from error
