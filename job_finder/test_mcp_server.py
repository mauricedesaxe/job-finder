from __future__ import annotations

import asyncio
from collections.abc import Generator
from contextlib import AbstractContextManager, contextmanager
from typing import cast

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import job_finder.mcp_server as server_module
from job_finder.configuration_service import ConfigurationRevisionNotFound
from job_finder.mcp_server import Connection, McpDependencies, create_mcp_server
from job_finder.search_configuration import (
    DEFAULT_SEARCH_CONFIGURATION,
    search_configuration_revision_id,
)


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
