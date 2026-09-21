from __future__ import annotations

import psycopg
from fastmcp import FastMCP

from job_finder.config import DatabaseSettings
from job_finder.database import apply_migrations
from job_finder.mcp_server import McpDependencies, create_mcp_server
from scripts.evaluate_manifest import run_stored_manifest


def create_server() -> FastMCP:
    database = DatabaseSettings.from_environment()

    def connect() -> psycopg.Connection[tuple[object, ...]]:
        return psycopg.connect(database.postgres_dsn, autocommit=True)

    with connect() as connection:
        _ = apply_migrations(connection)
    return create_mcp_server(McpDependencies(connect=connect, run_evaluation=run_stored_manifest))


if __name__ == "__main__":
    create_server().run()
