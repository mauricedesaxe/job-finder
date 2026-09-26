from __future__ import annotations

import os
from pathlib import Path

import psycopg
from fastmcp import FastMCP

from job_finder.config import DatabaseSettings
from job_finder.database import apply_migrations
from job_finder.evaluation.manifest_execution import run_stored_manifest
from job_finder.mcp_server import McpDependencies, create_mcp_server
from job_finder.provider_credentials import (
    credential_cipher,
    resolve_execution_provider_credentials,
)
from pydantic import SecretStr


def create_dependencies() -> McpDependencies:
    database = DatabaseSettings.from_environment()

    def connect() -> psycopg.Connection[tuple[object, ...]]:
        return psycopg.connect(database.postgres_dsn, autocommit=True)

    with connect() as connection:
        _ = apply_migrations(connection)
    artifact = os.environ.get("JOB_FINDER_IMPLEMENTATION_ARTIFACT")
    encryption_key = os.environ.get("JOB_FINDER_CREDENTIAL_ENCRYPTION_KEY")
    cipher = credential_cipher(SecretStr(encryption_key)) if encryption_key else None
    jina_fallback = os.environ.get("JINA_API_KEY") or None
    openrouter_fallback = os.environ.get("OPENROUTER_API_KEY") or None
    typesafe_fallback = os.environ.get("TYPESAFE_API_KEY") or None

    def resolve_credentials(connection: psycopg.Connection[tuple[object, ...]]):
        return resolve_execution_provider_credentials(
            connection,
            cipher=cipher,
            jina_fallback=jina_fallback,
            openrouter_fallback=openrouter_fallback,
            typesafe_fallback=typesafe_fallback,
        )

    return McpDependencies(
        connect=connect,
        run_evaluation=run_stored_manifest,
        implementation_artifact_path=Path(artifact) if artifact else None,
        resolve_provider_credentials=resolve_credentials,
    )


def create_server() -> FastMCP:
    return create_mcp_server(create_dependencies())


if __name__ == "__main__":
    create_server().run()
