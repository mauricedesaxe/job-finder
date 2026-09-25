# pyright: reportUnusedFunction=false
from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

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
from job_finder.mcp_tools.common import CAS_WRITE, PUBLISH_WRITE, READ_ONLY, McpDependencies
from job_finder.search_configuration import SearchConfiguration, SearchConfigurationDraft


def register_configuration_tools(mcp: FastMCP, dependencies: McpDependencies) -> None:
    read_only = READ_ONLY
    cas_write = CAS_WRITE
    publish_write = PUBLISH_WRITE

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
