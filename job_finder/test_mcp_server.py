from __future__ import annotations

import asyncio
from collections.abc import Generator
from contextlib import contextmanager

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from job_finder.mcp_server import Connection, McpDependencies, create_mcp_server


@contextmanager
def _no_database() -> Generator[Connection]:
    raise AssertionError("this test must not open a database connection")
    yield


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

            with pytest.raises(ToolError, match="validation error"):
                _ = await client.call_tool("feedback_list", {"limit": 0})

            with pytest.raises(ToolError, match="validation error"):
                _ = await client.call_tool(
                    "manifest_get",
                    {"manifest_id": "not-a-manifest-id"},
                )

    asyncio.run(exercise())
