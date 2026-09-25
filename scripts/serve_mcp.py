from __future__ import annotations

import os
from pathlib import Path

import psycopg
from fastmcp import FastMCP

from job_finder.config import DatabaseSettings
from job_finder.database import apply_migrations
from job_finder.evaluation.manifest_execution import run_stored_manifest
from job_finder.mcp_server import McpDependencies, create_mcp_server


def create_server() -> FastMCP:
    database = DatabaseSettings.from_environment()

    def connect() -> psycopg.Connection[tuple[object, ...]]:
        return psycopg.connect(database.postgres_dsn, autocommit=True)

    with connect() as connection:
        _ = apply_migrations(connection)
    artifact = os.environ.get("JOB_FINDER_IMPLEMENTATION_ARTIFACT")
    return create_mcp_server(
        McpDependencies(
            connect=connect,
            run_evaluation=run_stored_manifest,
            implementation_artifact_path=Path(artifact) if artifact else None,
        )
    )


if __name__ == "__main__":
    create_server().run()
