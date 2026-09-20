# pyright: reportUnusedFunction=false

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

import psycopg
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field
from pydantic_core import ValidationError

from job_finder.configuration_service import (
    DEFAULT_RESULT_LIMIT,
    MAX_POSTGRES_BIGINT,
    ActivateConfigurationCommand,
    ActivateConfigurationResult,
    ConfigurationInvalid,
    ConfigurationPreview,
    ConfigurationRevisionCursor,
    ConfigurationRevisionDetails,
    ConfigurationRevisionId,
    ConfigurationRevisionNotFound,
    ConfigurationRevisionPage,
    ConfigurationValidationResult,
    DraftSaveResult,
    IdempotencyKey,
    PublishConfigurationCommand,
    PublishConfigurationResult,
    PublishedActiveSearchConfiguration,
    SaveDraftCommand,
    activate_search_configuration,
    get_active_search_configuration,
    get_search_configuration_draft,
    get_search_configuration_revision,
    list_search_configuration_revisions,
    preview_search_configuration,
    publish_search_configuration,
    save_search_configuration_draft,
    validate_search_configuration,
)
from job_finder.evaluation.langfuse import ProjectionQueueStatus, load_projection_queue_status
from job_finder.evaluation.manifests import (
    CuratedReviewEvent,
    ManifestOperationError,
    ManifestPolicy,
    ManifestSummary,
    ManifestSummaryPage,
    create_manifest,
    exclude_review_event,
    include_review_event,
    list_manifests,
    load_manifest,
    preview_manifest,
    summarize_manifest,
)
from job_finder.review.models import FeedbackCurationFilter, ReviewFeedback, ReviewFeedbackPage
from job_finder.review.postgres import (
    ReviewFeedbackNotFound,
    list_review_feedback,
    load_review_feedback,
)
from job_finder.search_configuration import SearchConfiguration, SearchConfigurationDraft

Connection = psycopg.Connection[tuple[object, ...]]
ConnectionFactory = Callable[[], AbstractContextManager[Connection]]
Clock = Callable[[], datetime]
_DEFAULT_MAX_FALSE_POSITIVE_RATE = Decimal("0.05")
_DEFAULT_MAX_FALSE_NEGATIVE_RATE = Decimal("0.10")


@dataclass(frozen=True)
class McpDependencies:
    connect: ConnectionFactory
    actor: str = "mcp-owner"
    now: Clock = lambda: datetime.now(UTC)


def create_mcp_server(dependencies: McpDependencies) -> FastMCP:
    if not dependencies.actor:
        raise ValueError("MCP actor cannot be empty")
    mcp = FastMCP(
        "Job Finder",
        instructions=(
            "Use feedback tools to inspect the latest human review revisions. "
            "Curate evidence before freezing a manifest. Never treat unsure feedback as an eval label. "
            "Read configuration state before writes, then validate and preview before publishing."
        ),
        mask_error_details=True,
        on_duplicate="error",
    )
    read_only = ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )
    append_only = ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )
    cas_write = ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=True,
        open_world_hint=False,
    )
    publish_write = ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )

    def parse_configuration(candidate: object) -> SearchConfiguration:
        validation = validate_search_configuration(candidate)
        if isinstance(validation, ConfigurationInvalid):
            raise ToolError("Search configuration is invalid; call configuration_validate")
        return validation.configuration

    @mcp.tool(annotations=read_only)
    def configuration_active_get() -> PublishedActiveSearchConfiguration:
        """Get the currently active published search configuration and CAS generation."""
        with dependencies.connect() as connection:
            return get_active_search_configuration(connection)

    @mcp.tool(annotations=read_only)
    def configuration_draft_get() -> SearchConfigurationDraft:
        """Get the mutable search configuration draft and its CAS version."""
        with dependencies.connect() as connection:
            return get_search_configuration_draft(connection)

    @mcp.tool(annotations=read_only)
    def configuration_validate(
        candidate: dict[str, object],
        issue_limit: Annotated[int, Field(ge=1, le=100)] = DEFAULT_RESULT_LIMIT,
    ) -> ConfigurationValidationResult:
        """Validate an untrusted configuration candidate without storing it."""
        return validate_search_configuration(candidate, issue_limit=issue_limit)

    @mcp.tool(annotations=read_only)
    def configuration_preview(
        configuration: dict[str, object],
        search_sample_limit: Annotated[int, Field(ge=1, le=100)] = DEFAULT_RESULT_LIMIT,
        prompt_summary_limit: Annotated[int, Field(ge=1, le=100)] = DEFAULT_RESULT_LIMIT,
    ) -> ConfigurationPreview:
        """Preview bounded searches and prompts without I/O or mutation."""
        return preview_search_configuration(
            parse_configuration(configuration),
            search_sample_limit=search_sample_limit,
            prompt_summary_limit=prompt_summary_limit,
        )

    @mcp.tool(annotations=cas_write)
    def configuration_draft_update(
        expected_version: Annotated[int, Field(ge=0, le=MAX_POSTGRES_BIGINT)],
        configuration: dict[str, object],
    ) -> DraftSaveResult:
        """Replace the draft only if its version still matches."""
        with dependencies.connect() as connection:
            return save_search_configuration_draft(
                connection,
                SaveDraftCommand(
                    expected_version=expected_version,
                    configuration=parse_configuration(configuration),
                    actor=dependencies.actor,
                    timestamp=dependencies.now(),
                ),
            )

    @mcp.tool(annotations=publish_write)
    def configuration_publish(
        idempotency_key: IdempotencyKey,
        expected_draft_version: Annotated[int, Field(ge=0, le=MAX_POSTGRES_BIGINT)],
        expected_configuration_revision_id: ConfigurationRevisionId,
    ) -> PublishConfigurationResult:
        """Idempotently publish the exact observed draft and rebase it."""
        with dependencies.connect() as connection:
            return publish_search_configuration(
                connection,
                PublishConfigurationCommand(
                    idempotency_key=idempotency_key,
                    expected_draft_version=expected_draft_version,
                    expected_configuration_revision_id=expected_configuration_revision_id,
                    actor=dependencies.actor,
                    timestamp=dependencies.now(),
                ),
            )

    @mcp.tool(annotations=read_only)
    def configuration_revision_list(
        limit: Annotated[int, Field(ge=1, le=100)] = 25,
        cursor: ConfigurationRevisionCursor | None = None,
    ) -> ConfigurationRevisionPage:
        """List immutable configuration revisions newest first without content bodies."""
        with dependencies.connect() as connection:
            return list_search_configuration_revisions(connection, limit=limit, cursor=cursor)

    @mcp.tool(annotations=read_only)
    def configuration_revision_get(
        revision_id: ConfigurationRevisionId,
    ) -> ConfigurationRevisionDetails:
        """Get one immutable revision and its optional publication."""
        try:
            with dependencies.connect() as connection:
                return get_search_configuration_revision(connection, revision_id)
        except ConfigurationRevisionNotFound as error:
            raise ToolError(str(error)) from error

    @mcp.tool(annotations=cas_write)
    def configuration_activate(
        target_revision_id: ConfigurationRevisionId,
        expected_active_revision_id: ConfigurationRevisionId,
        expected_generation: Annotated[int, Field(ge=0, le=MAX_POSTGRES_BIGINT)],
    ) -> ActivateConfigurationResult:
        """Activate a published revision only if active state still matches."""
        with dependencies.connect() as connection:
            return activate_search_configuration(
                connection,
                ActivateConfigurationCommand(
                    target_revision_id=target_revision_id,
                    expected_active_revision_id=expected_active_revision_id,
                    expected_generation=expected_generation,
                    actor=dependencies.actor,
                    timestamp=dependencies.now(),
                ),
            )

    @mcp.tool(annotations=read_only)
    def feedback_list(
        curation: FeedbackCurationFilter = "all",
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> ReviewFeedbackPage:
        """List the latest human feedback revisions and their current eval curation."""
        with dependencies.connect() as connection:
            return list_review_feedback(
                connection,
                curation=curation,
                limit=limit,
                offset=offset,
            )

    @mcp.tool(annotations=read_only)
    def feedback_get(review_event_id: UUID) -> ReviewFeedback:
        """Get one exact feedback revision, including curation and frozen-manifest usage."""
        try:
            with dependencies.connect() as connection:
                return load_review_feedback(connection, review_event_id)
        except ReviewFeedbackNotFound as error:
            raise ToolError(str(error)) from error

    @mcp.tool(annotations=append_only)
    def feedback_curate(
        review_event_id: UUID,
        action: Literal["include", "exclude"],
        reason: Annotated[str, Field(min_length=1, max_length=2000)],
        idempotency_key: Annotated[str, Field(min_length=1, max_length=200)],
        critical: bool = False,
    ) -> CuratedReviewEvent:
        """Include feedback in future eval manifests or explicitly exclude it."""
        try:
            with dependencies.connect() as connection:
                if action == "include":
                    return include_review_event(
                        connection,
                        review_event_id=review_event_id,
                        critical=critical,
                        reason=reason,
                        actor=dependencies.actor,
                        created_at=dependencies.now(),
                        idempotency_key=idempotency_key,
                    )
                if critical:
                    raise ToolError("Excluded feedback cannot be marked critical")
                return exclude_review_event(
                    connection,
                    review_event_id=review_event_id,
                    reason=reason,
                    actor=dependencies.actor,
                    created_at=dependencies.now(),
                    idempotency_key=idempotency_key,
                )
        except ManifestOperationError as error:
            raise ToolError(str(error)) from error

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
    def langfuse_projection_status(
        failure_limit: Annotated[int, Field(ge=0, le=100)] = 20,
    ) -> ProjectionQueueStatus:
        """Report projection queue counts and a bounded list of current failures."""
        with dependencies.connect() as connection:
            return load_projection_queue_status(connection, failure_limit=failure_limit)

    return mcp


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
