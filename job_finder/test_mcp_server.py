from __future__ import annotations

import asyncio
from collections.abc import Generator
from contextlib import contextmanager
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import cast

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import job_finder.mcp_server as server_module
from job_finder.configuration_service import (
    ActivationTargetUnpublished,
    ActiveConfigurationChanged,
    ActivateConfigurationCommand,
    ConfigurationRevisionCursor,
    ConfigurationRevisionDetails,
    ConfigurationRevisionNotFound,
    ConfigurationRevisionPage,
    ConfigurationRevisionSummary,
    DraftChanged,
    PublicationIdempotencyKeyConflict,
    PublishConfigurationCommand,
    PublishDraftChanged,
    PublishedActiveSearchConfiguration,
    SaveDraftCommand,
    preview_search_configuration,
)
from job_finder.mcp_server import Connection, McpDependencies, create_mcp_server
from job_finder.search_configuration import (
    ActiveSearchConfiguration,
    DEFAULT_SEARCH_CONFIGURATION,
    SearchConfigurationDraft,
    SearchConfigurationPublication,
    SearchConfigurationRevision,
    SearchConfigurationRevisionId,
    search_configuration_revision_id,
)

NOW = datetime(2026, 9, 19, 12, tzinfo=UTC)


@contextmanager
def _connect() -> Generator[Connection]:
    yield cast(Connection, object())


def _no_database() -> AbstractContextManager[Connection]:
    raise AssertionError("this test must not open a database connection")


def test_mcp_tools_are_bounded_and_validate_input() -> None:
    server = create_mcp_server(McpDependencies(connect=_no_database))

    async def exercise() -> None:
        async with Client(server) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            assert set(tools) == {
                "feedback_list",
                "feedback_get",
                "feedback_curate",
                "manifest_preview",
                "manifest_create",
                "manifest_get",
                "manifest_list",
                "langfuse_projection_status",
                "configuration_active_get",
                "configuration_draft_get",
                "configuration_validate",
                "configuration_preview",
                "configuration_draft_update",
                "configuration_publish",
                "configuration_revision_list",
                "configuration_revision_get",
                "configuration_activate",
            }
            feedback_list = tools["feedback_list"]
            assert feedback_list.annotations is not None
            assert feedback_list.annotations.read_only_hint is True
            assert feedback_list.input_schema["properties"]["limit"]["maximum"] == 100
            assert feedback_list.output_schema is not None
            assert feedback_list.output_schema["properties"]["items"]["maxItems"] == 100
            feedback_curate = tools["feedback_curate"]
            assert feedback_curate.annotations is not None
            assert feedback_curate.annotations.read_only_hint is False
            manifest_list = tools["manifest_list"]
            assert manifest_list.output_schema is not None
            assert manifest_list.output_schema["properties"]["items"]["maxItems"] == 100
            projection = tools["langfuse_projection_status"]
            assert projection.output_schema is not None
            assert projection.output_schema["properties"]["failures"]["maxItems"] == 100
            configuration_tools = {
                name: tool for name, tool in tools.items() if name.startswith("configuration_")
            }
            for name in (
                "configuration_active_get",
                "configuration_draft_get",
                "configuration_validate",
                "configuration_preview",
                "configuration_revision_list",
                "configuration_revision_get",
            ):
                annotations = configuration_tools[name].annotations
                assert annotations is not None
                assert annotations.read_only_hint is True
                assert annotations.destructive_hint is False
                assert annotations.open_world_hint is False
            for name in ("configuration_draft_update", "configuration_activate"):
                annotations = configuration_tools[name].annotations
                assert annotations is not None
                assert annotations.read_only_hint is False
                assert annotations.destructive_hint is True
                assert annotations.idempotent_hint is True
                assert annotations.open_world_hint is False
            publish_annotations = configuration_tools["configuration_publish"].annotations
            assert publish_annotations is not None
            assert publish_annotations.destructive_hint is False
            assert publish_annotations.idempotent_hint is True
            for tool in configuration_tools.values():
                assert "actor" not in tool.input_schema["properties"]
                assert "timestamp" not in tool.input_schema["properties"]
            assert (
                configuration_tools["configuration_validate"].input_schema["properties"][
                    "issue_limit"
                ]["maximum"]
                == 100
            )
            assert (
                configuration_tools["configuration_preview"].input_schema["properties"][
                    "search_sample_limit"
                ]["maximum"]
                == 100
            )
            assert (
                configuration_tools["configuration_preview"].input_schema["properties"][
                    "prompt_summary_limit"
                ]["maximum"]
                == 100
            )
            revision_list = configuration_tools["configuration_revision_list"]
            assert revision_list.input_schema["properties"]["limit"]["maximum"] == 100
            assert revision_list.output_schema is not None
            assert revision_list.output_schema["properties"]["items"]["maxItems"] == 100

            with pytest.raises(ToolError, match="validation error"):
                _ = await client.call_tool("feedback_list", {"limit": 0})

            with pytest.raises(ToolError, match="validation error"):
                _ = await client.call_tool(
                    "manifest_get",
                    {"manifest_id": "not-a-manifest-id"},
                )

    asyncio.run(exercise())


def test_mcp_configuration_tools_delegate_with_server_authority_and_structured_conflicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision_id = search_configuration_revision_id(DEFAULT_SEARCH_CONFIGURATION)
    preview = preview_search_configuration(DEFAULT_SEARCH_CONFIGURATION)
    revision = SearchConfigurationRevision(
        id=revision_id,
        configuration=DEFAULT_SEARCH_CONFIGURATION,
        created_at=NOW,
        created_by="agent-owner",
    )
    publication = SearchConfigurationPublication(
        revision_id=revision_id,
        prompt_release_id=preview.prompt_release_id,
        published_at=NOW,
        published_by="agent-owner",
    )
    draft = SearchConfigurationDraft(
        base_revision_id=revision_id,
        version=4,
        configuration=DEFAULT_SEARCH_CONFIGURATION,
        updated_at=NOW,
        updated_by="agent-owner",
    )
    active = PublishedActiveSearchConfiguration(
        active=ActiveSearchConfiguration(
            generation=2,
            revision=revision,
            activated_at=NOW,
            activated_by="agent-owner",
        ),
        publication=publication,
    )
    page = ConfigurationRevisionPage(
        items=(
            ConfigurationRevisionSummary(
                revision_id=revision_id,
                created_at=NOW,
                created_by="agent-owner",
                publication=publication,
            ),
        ),
        next_cursor=ConfigurationRevisionCursor(created_at=NOW, revision_id=revision_id),
    )
    next_cursor = ConfigurationRevisionCursor(created_at=NOW, revision_id=revision_id)
    details = ConfigurationRevisionDetails(revision=revision, publication=publication)
    clock_calls = 0

    def now() -> datetime:
        nonlocal clock_calls
        clock_calls += 1
        return NOW

    def get_active(_connection: Connection) -> PublishedActiveSearchConfiguration:
        return active

    def get_draft(_connection: Connection) -> SearchConfigurationDraft:
        return draft

    monkeypatch.setattr(server_module, "get_active_search_configuration", get_active)
    monkeypatch.setattr(server_module, "get_search_configuration_draft", get_draft)

    def list_revisions(
        _connection: Connection,
        *,
        limit: int,
        cursor: ConfigurationRevisionCursor | None,
    ) -> ConfigurationRevisionPage:
        assert limit == 5
        assert cursor == next_cursor
        return page

    monkeypatch.setattr(server_module, "list_search_configuration_revisions", list_revisions)

    def get_revision(
        _connection: Connection, _revision_id: SearchConfigurationRevisionId
    ) -> ConfigurationRevisionDetails:
        return details

    monkeypatch.setattr(server_module, "get_search_configuration_revision", get_revision)

    def save(_connection: Connection, command: SaveDraftCommand) -> DraftChanged:
        assert command.actor == "agent-owner"
        assert command.timestamp == NOW
        assert command.expected_version == 4
        return DraftChanged(current_draft=draft)

    def publish(
        _connection: Connection, command: PublishConfigurationCommand
    ) -> PublishDraftChanged | PublicationIdempotencyKeyConflict:
        assert command.actor == "agent-owner"
        assert command.timestamp == NOW
        if command.idempotency_key == "publish:conflict":
            return PublicationIdempotencyKeyConflict(idempotency_key=command.idempotency_key)
        return PublishDraftChanged(
            replayed=False,
            expected_draft_version=command.expected_draft_version,
            expected_configuration_revision_id=command.expected_configuration_revision_id,
            observed_draft_version=command.expected_draft_version + 1,
            observed_configuration_revision_id=command.expected_configuration_revision_id,
        )

    def activate(
        _connection: Connection, command: ActivateConfigurationCommand
    ) -> ActiveConfigurationChanged | ActivationTargetUnpublished:
        assert command.actor == "agent-owner"
        assert command.timestamp == NOW
        if command.expected_generation == 3:
            return ActivationTargetUnpublished(target_revision_id=command.target_revision_id)
        return ActiveConfigurationChanged(active_configuration=active)

    monkeypatch.setattr(server_module, "save_search_configuration_draft", save)
    monkeypatch.setattr(server_module, "publish_search_configuration", publish)
    monkeypatch.setattr(server_module, "activate_search_configuration", activate)
    server = create_mcp_server(McpDependencies(connect=_connect, actor="agent-owner", now=now))

    async def exercise() -> None:
        async with Client(server) as client:
            assert (
                PublishedActiveSearchConfiguration.model_validate(
                    (await client.call_tool("configuration_active_get", {})).structured_content
                )
                == active
            )
            assert (
                SearchConfigurationDraft.model_validate(
                    (await client.call_tool("configuration_draft_get", {})).structured_content
                )
                == draft
            )
            validation = await client.call_tool(
                "configuration_validate",
                {"candidate": DEFAULT_SEARCH_CONFIGURATION.model_dump(mode="json")},
            )
            assert validation.structured_content is not None
            assert validation.structured_content["result"]["kind"] == "valid"
            preview_result = await client.call_tool(
                "configuration_preview",
                {"configuration": DEFAULT_SEARCH_CONFIGURATION.model_dump(mode="json")},
            )
            assert preview_result.structured_content is not None
            assert preview_result.structured_content["configuration_revision_id"] == revision_id
            listed = await client.call_tool(
                "configuration_revision_list",
                {"limit": 5, "cursor": next_cursor.model_dump(mode="json")},
            )
            assert ConfigurationRevisionPage.model_validate(listed.structured_content) == page
            got = await client.call_tool("configuration_revision_get", {"revision_id": revision_id})
            assert ConfigurationRevisionDetails.model_validate(got.structured_content) == details
            saved = await client.call_tool(
                "configuration_draft_update",
                {
                    "expected_version": 4,
                    "configuration": DEFAULT_SEARCH_CONFIGURATION.model_dump(mode="json"),
                },
            )
            assert saved.structured_content is not None
            assert (
                DraftChanged.model_validate(saved.structured_content["result"]).current_draft
                == draft
            )
            published = await client.call_tool(
                "configuration_publish",
                {
                    "idempotency_key": "publish:1",
                    "expected_draft_version": 4,
                    "expected_configuration_revision_id": revision_id,
                },
            )
            assert published.structured_content is not None
            assert (
                PublishDraftChanged.model_validate(published.structured_content["result"]).kind
                == "draft_changed"
            )
            activated = await client.call_tool(
                "configuration_activate",
                {
                    "target_revision_id": revision_id,
                    "expected_active_revision_id": revision_id,
                    "expected_generation": 2,
                },
            )
            assert activated.structured_content is not None
            assert (
                ActiveConfigurationChanged.model_validate(
                    activated.structured_content["result"]
                ).active_configuration
                == active
            )
            publish_conflict = await client.call_tool(
                "configuration_publish",
                {
                    "idempotency_key": "publish:conflict",
                    "expected_draft_version": 4,
                    "expected_configuration_revision_id": revision_id,
                },
            )
            assert publish_conflict.structured_content is not None
            assert (
                PublicationIdempotencyKeyConflict.model_validate(
                    publish_conflict.structured_content["result"]
                ).kind
                == "idempotency_key_conflict"
            )
            unpublished = await client.call_tool(
                "configuration_activate",
                {
                    "target_revision_id": revision_id,
                    "expected_active_revision_id": revision_id,
                    "expected_generation": 3,
                },
            )
            assert unpublished.structured_content is not None
            assert (
                ActivationTargetUnpublished.model_validate(
                    unpublished.structured_content["result"]
                ).kind
                == "unpublished_target"
            )

    asyncio.run(exercise())
    assert clock_calls == 5


def test_mcp_configuration_revision_get_sanitizes_only_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision_id = search_configuration_revision_id(DEFAULT_SEARCH_CONFIGURATION)

    def missing(*_args: object) -> None:
        raise ConfigurationRevisionNotFound("Search configuration revision does not exist")

    monkeypatch.setattr(server_module, "get_search_configuration_revision", missing)
    server = create_mcp_server(McpDependencies(connect=_connect))

    async def exercise_missing() -> None:
        async with Client(server) as client:
            with pytest.raises(ToolError, match="Search configuration revision does not exist"):
                _ = await client.call_tool(
                    "configuration_revision_get", {"revision_id": revision_id}
                )

    asyncio.run(exercise_missing())

    def corrupt(*_args: object) -> None:
        raise RuntimeError("secret database details")

    monkeypatch.setattr(server_module, "get_search_configuration_revision", corrupt)
    server = create_mcp_server(McpDependencies(connect=_connect))

    async def exercise_corrupt() -> None:
        async with Client(server) as client:
            with pytest.raises(ToolError) as captured:
                _ = await client.call_tool(
                    "configuration_revision_get", {"revision_id": revision_id}
                )
            assert "secret database details" not in str(captured.value)

    asyncio.run(exercise_corrupt())


def test_mcp_configuration_validation_errors_hide_rejected_values() -> None:
    sensitive_value = "DO-NOT-LEAK"
    invalid_configuration = DEFAULT_SEARCH_CONFIGURATION.model_dump(mode="json")
    invalid_configuration["search_keywords"] = [sensitive_value, sensitive_value]
    server = create_mcp_server(McpDependencies(connect=_connect))

    async def exercise() -> None:
        async with Client(server) as client:
            for tool_name, arguments in (
                ("configuration_preview", {"configuration": invalid_configuration}),
                (
                    "configuration_draft_update",
                    {"expected_version": 0, "configuration": invalid_configuration},
                ),
            ):
                with pytest.raises(ToolError) as captured:
                    _ = await client.call_tool(tool_name, arguments)
                assert sensitive_value not in str(captured.value)

    asyncio.run(exercise())
