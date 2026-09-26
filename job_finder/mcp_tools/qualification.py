# pyright: reportUnusedFunction=false
from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from job_finder.benchmarks.qualification_activation import (
    ActivateQualificationTargetCommand,
    ActiveQualificationTarget,
    QualificationActivationError,
    QualificationActivationReceipt,
    activate_qualification_target,
)
from job_finder.benchmarks.qualification_promotions import (
    PromotionEvidenceSelection,
    QualificationPromotionDecision,
    QualificationPromotionPreview,
    preview_qualification_promotion,
    record_qualification_promotion_decision,
)
from job_finder.evaluation.qualification_components import (
    DeduplicationContent,
    EnrichmentContent,
    InputPreparationContent,
    QualificationTargetId,
    RelevanceContent,
    ResolvedQualificationTarget,
)
from job_finder.mcp_tools.common import APPEND_ONLY, CAS_WRITE, READ_ONLY, McpDependencies
from job_finder.qualification_target_service import (
    CreateQualificationCandidateCommand,
    create_qualification_candidate,
    get_active_qualification_authority,
    get_qualification_candidate,
)

_TargetId = Annotated[QualificationTargetId, Field(pattern=r"^[0-9a-f]{64}$")]


def register_qualification_tools(mcp: FastMCP, dependencies: McpDependencies) -> None:
    _register_candidate_tools(mcp, dependencies)
    _register_promotion_tools(mcp, dependencies)


def _artifact_path(dependencies: McpDependencies) -> Path:
    path = dependencies.implementation_artifact_path
    if path is None:
        raise ToolError("Executing build artifact is not configured")
    return path


def _register_candidate_tools(mcp: FastMCP, dependencies: McpDependencies) -> None:
    @mcp.tool(annotations=READ_ONLY)
    def qualification_active_get() -> ActiveQualificationTarget:
        """Get the independent active qualification target and CAS generation."""
        with dependencies.connect() as connection:
            return get_active_qualification_authority(connection)

    @mcp.tool(annotations=READ_ONLY)
    def qualification_candidate_get(target_id: _TargetId) -> ResolvedQualificationTarget:
        """Inspect one complete immutable qualification target candidate."""
        try:
            with dependencies.connect() as connection:
                return get_qualification_candidate(connection, target_id)
        except ValueError as error:
            raise ToolError(str(error)) from error

    @mcp.tool(annotations=APPEND_ONLY)
    def qualification_candidate_create(
        input_preparation: InputPreparationContent,
        relevance: RelevanceContent,
        enrichment: EnrichmentContent,
        deduplication: DeduplicationContent,
    ) -> ResolvedQualificationTarget:
        """Store four components as one complete candidate for this executing build."""
        try:
            with dependencies.connect() as connection:
                return create_qualification_candidate(
                    connection,
                    CreateQualificationCandidateCommand(
                        input_preparation=input_preparation,
                        relevance=relevance,
                        enrichment=enrichment,
                        deduplication=deduplication,
                        actor=dependencies.actor,
                        timestamp=dependencies.now(),
                    ),
                    _artifact_path(dependencies),
                )
        except ValueError as error:
            raise ToolError(str(error)) from error


def _register_promotion_tools(mcp: FastMCP, dependencies: McpDependencies) -> None:
    @mcp.tool(annotations=READ_ONLY)
    def qualification_promotion_preview(
        baseline_target_id: _TargetId | None,
        candidate_target_id: _TargetId,
        evidence: PromotionEvidenceSelection,
    ) -> QualificationPromotionPreview:
        """Check canonical evidence. Use a null baseline for the first activation."""
        try:
            with dependencies.connect() as connection:
                return preview_qualification_promotion(
                    connection,
                    baseline_target_id,
                    candidate_target_id,
                    evidence,
                    _artifact_path(dependencies),
                )
        except ValueError as error:
            raise ToolError(str(error)) from error

    @mcp.tool(annotations=APPEND_ONLY)
    def qualification_promotion_decide(
        baseline_target_id: _TargetId | None,
        candidate_target_id: _TargetId,
        evidence: PromotionEvidenceSelection,
        decision: Literal["approved", "rejected"],
        reason: Annotated[str, Field(min_length=1, max_length=2000)],
        idempotency_key: Annotated[str, Field(min_length=1, max_length=200)],
    ) -> QualificationPromotionDecision:
        """Record an immutable decision. A null baseline requires every phase to pass."""
        try:
            with dependencies.connect() as connection:
                return record_qualification_promotion_decision(
                    connection,
                    baseline_target_id=baseline_target_id,
                    candidate_target_id=candidate_target_id,
                    evidence=evidence,
                    artifact_path=_artifact_path(dependencies),
                    decision=decision,
                    reason=reason,
                    actor=dependencies.actor,
                    created_at=dependencies.now(),
                    idempotency_key=idempotency_key,
                )
        except ValueError as error:
            raise ToolError(str(error)) from error

    @mcp.tool(annotations=CAS_WRITE)
    def qualification_activate(
        idempotency_key: Annotated[str, Field(min_length=1, max_length=200)],
        promotion_decision_id: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
        expected_target_id: _TargetId | None,
        expected_generation: Annotated[int, Field(ge=0, le=2**63 - 1)],
    ) -> QualificationActivationReceipt:
        """Activate an approved target. For the first activation expect null target and generation 0."""
        try:
            with dependencies.connect() as connection:
                return activate_qualification_target(
                    connection,
                    ActivateQualificationTargetCommand(
                        idempotency_key=idempotency_key,
                        promotion_decision_id=promotion_decision_id,
                        expected_target_id=expected_target_id,
                        expected_generation=expected_generation,
                        actor=dependencies.actor,
                        timestamp=dependencies.now(),
                    ),
                    _artifact_path(dependencies),
                )
        except QualificationActivationError as error:
            raise ToolError(str(error)) from error
