# pyright: reportUnusedFunction=false
from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field
from pydantic_core import ValidationError

from job_finder.benchmarks.executions import (
    EvaluateManifestCommand,
    EvaluationExecutionState,
    EvaluationRun,
    load_evaluation_execution,
    load_run,
)
from job_finder.benchmarks.manifests import (
    ManifestOperationError,
    ManifestPolicy,
    ManifestSummary,
    ManifestSummaryPage,
    create_manifest,
    list_manifests,
    load_manifest,
    preview_manifest,
    summarize_manifest,
)
from job_finder.evaluation.models import ReleaseTarget
from job_finder.evaluation.prompt_releases import PromptReleaseError
from job_finder.evaluation.relevance_releases import RelevanceReleaseError
from job_finder.mcp_tools.common import APPEND_ONLY, PROVIDER_WRITE, READ_ONLY, McpDependencies
from job_finder.projections.outbox import ProjectionQueueStatus, load_projection_queue_status

_DEFAULT_MAX_FALSE_POSITIVE_RATE = Decimal("0.05")
_DEFAULT_MAX_FALSE_NEGATIVE_RATE = Decimal("0.10")


def register_benchmark_tools(mcp: FastMCP, dependencies: McpDependencies) -> None:
    read_only = READ_ONLY
    append_only = APPEND_ONLY
    provider_write = PROVIDER_WRITE

    @mcp.tool(annotations=read_only)
    def manifest_preview(
        regular_trial_count: Annotated[int, Field(gt=0)] = 1,
        critical_trial_count: Annotated[int, Field(gt=1)] = 3,
        max_false_positive_rate: Annotated[
            Decimal, Field(ge=0, le=1)
        ] = _DEFAULT_MAX_FALSE_POSITIVE_RATE,
        max_false_negative_rate: Annotated[
            Decimal, Field(ge=0, le=1)
        ] = _DEFAULT_MAX_FALSE_NEGATIVE_RATE,
    ) -> ManifestSummary:
        """Preview the eval manifest that current curations would produce."""
        policy = _manifest_policy(
            regular_trial_count,
            critical_trial_count,
            max_false_positive_rate,
            max_false_negative_rate,
        )
        with dependencies.connect() as connection:
            return preview_manifest(connection, policy)

    @mcp.tool(annotations=append_only)
    def manifest_create(
        idempotency_key: Annotated[str, Field(min_length=1, max_length=200)],
        regular_trial_count: Annotated[int, Field(gt=0)] = 1,
        critical_trial_count: Annotated[int, Field(gt=1)] = 3,
        max_false_positive_rate: Annotated[
            Decimal, Field(ge=0, le=1)
        ] = _DEFAULT_MAX_FALSE_POSITIVE_RATE,
        max_false_negative_rate: Annotated[
            Decimal, Field(ge=0, le=1)
        ] = _DEFAULT_MAX_FALSE_NEGATIVE_RATE,
    ) -> ManifestSummary:
        """Freeze current included feedback into an immutable eval manifest."""
        policy = _manifest_policy(
            regular_trial_count,
            critical_trial_count,
            max_false_positive_rate,
            max_false_negative_rate,
        )
        try:
            with dependencies.connect() as connection:
                return summarize_manifest(
                    create_manifest(
                        connection,
                        policy=policy,
                        created_at=dependencies.now(),
                        created_by=dependencies.actor,
                        idempotency_key=idempotency_key,
                    )
                )
        except ManifestOperationError as error:
            raise ToolError(str(error)) from error

    @mcp.tool(annotations=read_only)
    def manifest_get(
        manifest_id: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
    ) -> ManifestSummary:
        """Get a bounded summary of one immutable eval manifest."""
        try:
            with dependencies.connect() as connection:
                return summarize_manifest(load_manifest(connection, manifest_id))
        except ManifestOperationError as error:
            raise ToolError(str(error)) from error

    @mcp.tool(annotations=read_only)
    def manifest_list(
        limit: Annotated[int, Field(ge=1, le=100)] = 25,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> ManifestSummaryPage:
        """List immutable eval manifests without returning every case body."""
        with dependencies.connect() as connection:
            return list_manifests(connection, limit=limit, offset=offset)

    @mcp.tool(annotations=read_only)
    def evaluation_execution_get(
        execution_id: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
    ) -> EvaluationExecutionState:
        """Get one evaluation execution, including running or failure state."""
        try:
            with dependencies.connect() as connection:
                return load_evaluation_execution(connection, execution_id)
        except ValueError as error:
            raise ToolError(str(error)) from error

    @mcp.tool(annotations=provider_write)
    def evaluation_run(
        idempotency_key: Annotated[str, Field(min_length=1, max_length=200)],
        manifest_id: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
        target: ReleaseTarget,
        implementation_ref: Annotated[str, Field(min_length=1, max_length=200)],
    ) -> EvaluationExecutionState:
        """Run or replay a manifest evaluation when this MCP deployment has providers configured."""
        if dependencies.run_evaluation is None:
            raise ToolError("Evaluation execution is not configured for this MCP server")
        try:
            with dependencies.connect() as connection:
                return dependencies.run_evaluation(
                    connection,
                    EvaluateManifestCommand(
                        idempotency_key=idempotency_key,
                        manifest_id=manifest_id,
                        target=target,
                        implementation_ref=implementation_ref,
                    ),
                )
        except (PromptReleaseError, RelevanceReleaseError, ValueError) as error:
            raise ToolError(str(error)) from error

    @mcp.tool(annotations=read_only)
    def evaluation_run_get(
        run_id: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
    ) -> EvaluationRun:
        """Get one completed exact-target evaluation run."""
        try:
            with dependencies.connect() as connection:
                return load_run(connection, run_id)
        except ValueError as error:
            raise ToolError(str(error)) from error

    @mcp.tool(annotations=read_only)
    def langfuse_projection_status(
        failure_limit: Annotated[int, Field(ge=0, le=100)] = 20,
    ) -> ProjectionQueueStatus:
        """Report projection queue counts and a bounded list of current failures."""
        with dependencies.connect() as connection:
            return load_projection_queue_status(connection, failure_limit=failure_limit)


def _manifest_policy(
    regular_trial_count: int,
    critical_trial_count: int,
    max_false_positive_rate: Decimal,
    max_false_negative_rate: Decimal,
) -> ManifestPolicy:
    try:
        return ManifestPolicy(
            regular_trial_count=regular_trial_count,
            critical_trial_count=critical_trial_count,
            max_false_positive_rate=max_false_positive_rate,
            max_false_negative_rate=max_false_negative_rate,
        )
    except ValidationError as error:
        raise ToolError(str(error)) from error
