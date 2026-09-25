# pyright: reportUnusedFunction=false
from __future__ import annotations

from typing import Annotated, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from job_finder.benchmarks.comparisons import EvaluationRunComparison, preview_run_comparison
from job_finder.benchmarks.promotions import (
    PromptPromotionDecision,
    record_prompt_promotion_decision,
)
from job_finder.configuration_service import MAX_POSTGRES_BIGINT
from job_finder.evaluation.models import PromptReleaseId, ReleaseTarget, RelevanceReleaseId
from job_finder.evaluation.prompt_releases import PromptReleaseError
from job_finder.evaluation.release_targets import (
    ActivateReleaseTargetCommand,
    ActivateReleaseTargetResult,
    ActiveReleaseTarget,
    CreateReleaseTargetCandidateCommand,
    ReleaseTargetLifecycleError,
    activate_release_target,
    create_release_target_candidate,
    get_active_release_target,
)
from job_finder.evaluation.relevance_releases import (
    RelevanceExecutionPolicy,
    RelevanceReleaseError,
)
from job_finder.mcp_tools.common import APPEND_ONLY, CAS_WRITE, READ_ONLY, McpDependencies


def register_release_tools(mcp: FastMCP, dependencies: McpDependencies) -> None:
    read_only = READ_ONLY
    append_only = APPEND_ONLY
    cas_write = CAS_WRITE

    @mcp.tool(annotations=append_only)
    def release_target_candidate_create(
        prompt_release_id: Annotated[PromptReleaseId, Field(pattern=r"^[0-9a-f]{64}$")],
        relevance_release_id: Annotated[
            RelevanceReleaseId | None, Field(pattern=r"^[0-9a-f]{64}$")
        ] = None,
        relevance_policy: RelevanceExecutionPolicy | None = None,
    ) -> ReleaseTarget:
        """Create or validate an immutable prompt-and-relevance release target."""
        try:
            with dependencies.connect() as connection:
                return create_release_target_candidate(
                    connection,
                    CreateReleaseTargetCandidateCommand(
                        prompt_release_id=prompt_release_id,
                        relevance_release_id=relevance_release_id,
                        relevance_policy=relevance_policy,
                        actor=dependencies.actor,
                        timestamp=dependencies.now(),
                    ),
                )
        except (PromptReleaseError, RelevanceReleaseError, ValueError) as error:
            raise ToolError(str(error)) from error

    @mcp.tool(annotations=read_only)
    def release_target_active_get() -> ActiveReleaseTarget:
        """Get the active release target and its CAS generation."""
        try:
            with dependencies.connect() as connection:
                return get_active_release_target(connection)
        except ReleaseTargetLifecycleError as error:
            raise ToolError(str(error)) from error

    @mcp.tool(annotations=read_only)
    def release_target_compare(
        baseline_run_id: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
        candidate_run_id: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
    ) -> EvaluationRunComparison:
        """Compare two exact-target runs over their shared immutable manifest."""
        try:
            with dependencies.connect() as connection:
                return preview_run_comparison(connection, baseline_run_id, candidate_run_id)
        except ValueError as error:
            raise ToolError(str(error)) from error

    @mcp.tool(annotations=append_only)
    def release_target_decide(
        baseline_run_id: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
        candidate_run_id: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
        expected_comparison_id: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
        decision: Literal["approved", "rejected"],
        reason: Annotated[str, Field(min_length=1, max_length=2000)],
        idempotency_key: Annotated[str, Field(min_length=1, max_length=200)],
    ) -> PromptPromotionDecision:
        """Record an immutable decision over one exact run comparison."""
        try:
            with dependencies.connect() as connection:
                return record_prompt_promotion_decision(
                    connection,
                    baseline_run_id=baseline_run_id,
                    candidate_run_id=candidate_run_id,
                    expected_comparison_id=expected_comparison_id,
                    decision=decision,
                    reason=reason,
                    actor=dependencies.actor,
                    created_at=dependencies.now(),
                    idempotency_key=idempotency_key,
                )
        except ValueError as error:
            raise ToolError(str(error)) from error

    @mcp.tool(annotations=cas_write)
    def release_target_activate(
        promotion_decision_id: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
        expected_active_target: ReleaseTarget,
        expected_generation: Annotated[int, Field(ge=0, le=MAX_POSTGRES_BIGINT)],
        idempotency_key: Annotated[str, Field(min_length=1, max_length=200)],
    ) -> ActivateReleaseTargetResult:
        """Activate an approved decision's candidate if active target and generation still match."""
        try:
            with dependencies.connect() as connection:
                return activate_release_target(
                    connection,
                    ActivateReleaseTargetCommand(
                        idempotency_key=idempotency_key,
                        promotion_decision_id=promotion_decision_id,
                        expected_active_target=expected_active_target,
                        expected_generation=expected_generation,
                        actor=dependencies.actor,
                        timestamp=dependencies.now(),
                    ),
                )
        except ReleaseTargetLifecycleError as error:
            raise ToolError(str(error)) from error
