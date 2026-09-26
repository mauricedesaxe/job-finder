from __future__ import annotations

import asyncio
from collections.abc import Generator
from contextlib import AbstractContextManager, contextmanager
from typing import cast

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import job_finder.mcp_tools.configuration as configuration_tools
from job_finder.database import Connection
from job_finder.mcp_server import McpDependencies, create_mcp_server
from job_finder.search_configuration import (
    DEFAULT_SEARCH_CONFIGURATION,
    search_configuration_revision_id,
)


@contextmanager
def _connect() -> Generator[Connection]:
    yield cast(Connection, object())


def _no_database() -> AbstractContextManager[Connection]:
    raise AssertionError("this test must not open a database connection")


def test_mcp_server_rejects_duplicate_tool_names() -> None:
    server = create_mcp_server(McpDependencies(connect=_no_database))

    with pytest.raises(ValueError, match="Component already exists"):
        _ = server.tool(name="feedback_list")(lambda: None)


_EXPECTED_TOOLS = {
    "qualification_active_get",
    "qualification_candidate_get",
    "qualification_candidate_create",
    "qualification_candidate_compile",
    "qualification_definition_draft_get",
    "qualification_definition_draft_update",
    "qualification_definition_publish",
    "qualification_definition_revision_get",
    "qualification_evidence_list",
    "qualification_evidence_get",
    "qualification_evidence_execute",
    "qualification_fixture_set_store",
    "qualification_fixture_set_get",
    "qualification_relevance_input_store",
    "qualification_relevance_input_get",
    "qualification_relevance_comparison_create",
    "qualification_promotion_preview",
    "qualification_promotion_decide",
    "qualification_activate",
    "acquisition_active_get",
    "acquisition_draft_get",
    "acquisition_revision_get",
    "acquisition_draft_update",
    "acquisition_publish",
    "acquisition_activate",
    "feedback_list",
    "feedback_get",
    "feedback_curate",
    "manifest_preview",
    "manifest_create",
    "manifest_get",
    "manifest_list",
    "release_target_candidate_create",
    "release_target_active_get",
    "evaluation_execution_get",
    "evaluation_run",
    "evaluation_run_get",
    "release_target_compare",
    "release_target_decide",
    "release_target_activate",
    "langfuse_projection_status",
    "configuration_active_get",
    "configuration_draft_get",
    "configuration_validate",
    "configuration_preview",
    "configuration_revision_list",
    "configuration_revision_get",
}


def test_mcp_tools_are_bounded_and_validate_input() -> None:
    server = create_mcp_server(McpDependencies(connect=_no_database))

    async def exercise() -> None:
        async with Client(server) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            assert set(tools) == _EXPECTED_TOOLS
            feedback_list = tools["feedback_list"]
            assert feedback_list.annotations is not None
            assert feedback_list.annotations.read_only_hint is True
            feedback_curate = tools["feedback_curate"]
            assert feedback_curate.annotations is not None
            assert feedback_curate.annotations.read_only_hint is False
            assert tools["release_target_active_get"].annotations is not None
            assert tools["release_target_active_get"].annotations.read_only_hint is True
            assert tools["release_target_activate"].annotations is not None
            assert tools["release_target_activate"].annotations.destructive_hint is True
            assert tools["evaluation_run"].annotations is not None
            assert tools["evaluation_run"].annotations.open_world_hint is True
            candidate_schema = tools["release_target_candidate_create"].input_schema
            assert "relevance_policy" in candidate_schema["properties"]
            configuration_tools = {
                name: tool for name, tool in tools.items() if name.startswith("configuration_")
            }
            for tool in configuration_tools.values():
                assert "actor" not in tool.input_schema["properties"]
                assert "timestamp" not in tool.input_schema["properties"]

            with pytest.raises(ToolError, match="validation error"):
                _ = await client.call_tool("feedback_list", {"limit": 0})

            with pytest.raises(ToolError, match="validation error"):
                _ = await client.call_tool(
                    "manifest_get",
                    {"manifest_id": "not-a-manifest-id"},
                )

    asyncio.run(exercise())


def test_mcp_configuration_errors_hide_unexpected_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision_id = search_configuration_revision_id(DEFAULT_SEARCH_CONFIGURATION)

    def corrupt(*_args: object) -> None:
        raise RuntimeError("secret database details")

    monkeypatch.setattr(configuration_tools, "get_search_configuration_revision", corrupt)
    server = create_mcp_server(McpDependencies(connect=_connect))

    async def exercise() -> None:
        async with Client(server) as client:
            with pytest.raises(ToolError) as captured:
                _ = await client.call_tool(
                    "configuration_revision_get", {"revision_id": revision_id}
                )
            assert "secret database details" not in str(captured.value)

    asyncio.run(exercise())


def test_mcp_configuration_validation_errors_hide_rejected_values() -> None:
    sensitive_value = "DO-NOT-LEAK"
    invalid_configuration = DEFAULT_SEARCH_CONFIGURATION.model_dump(mode="json")
    invalid_configuration["search_keywords"] = [sensitive_value, sensitive_value]
    server = create_mcp_server(McpDependencies(connect=_connect))

    async def exercise() -> None:
        async with Client(server) as client:
            with pytest.raises(ToolError) as captured:
                _ = await client.call_tool(
                    "configuration_preview", {"configuration": invalid_configuration}
                )
            assert sensitive_value not in str(captured.value)

    asyncio.run(exercise())
