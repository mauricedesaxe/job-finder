from __future__ import annotations

from fastmcp import FastMCP

from job_finder.mcp_tools.benchmarks import register_benchmark_tools
from job_finder.mcp_tools.common import McpDependencies as McpDependencies
from job_finder.mcp_tools.configuration import register_configuration_tools
from job_finder.mcp_tools.feedback import register_feedback_tools
from job_finder.mcp_tools.releases import register_release_tools


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
    register_configuration_tools(mcp, dependencies)
    register_feedback_tools(mcp, dependencies)
    register_benchmark_tools(mcp, dependencies)
    register_release_tools(mcp, dependencies)
    return mcp
