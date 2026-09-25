# pyright: reportUnusedFunction=false
from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from job_finder.benchmarks.manifests import (
    CuratedReviewEvent,
    ManifestOperationError,
    exclude_review_event,
    include_review_event,
)
from job_finder.mcp_tools.common import APPEND_ONLY, READ_ONLY, McpDependencies
from job_finder.review.feedback import (
    FeedbackCurationFilter,
    ReviewFeedback,
    ReviewFeedbackPage,
    ReviewFeedbackNotFound,
    list_review_feedback,
    load_review_feedback,
)


def register_feedback_tools(mcp: FastMCP, dependencies: McpDependencies) -> None:
    read_only = READ_ONLY
    append_only = APPEND_ONLY

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
