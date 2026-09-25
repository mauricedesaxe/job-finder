from __future__ import annotations

import asyncio
from collections.abc import Generator, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest
from fastmcp import Client
from psycopg import sql
from pydantic import TypeAdapter

from job_finder.acquisition_policy import acquisition_policy_revision_id
from job_finder.acquisition_policy_activation import AcquisitionActivationReceipt
from job_finder.acquisition_policy_service import (
    AcquisitionPolicyDraft,
    ActiveAcquisitionPolicy,
    AcquisitionPublicationReceipt,
)
from job_finder.config import PostgresContractSettings
from job_finder.database import apply_migrations
from job_finder.mcp_server import McpDependencies, create_mcp_server

_NOW = datetime(2026, 9, 25, tzinfo=UTC)


@pytest.fixture
def authority_schema() -> Iterator[str]:
    settings = PostgresContractSettings.from_environment()
    schema_name = f"job_finder_acquisition_mcp_{uuid4().hex}"
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as connection:
        _ = connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
        try:
            yield schema_name
        finally:
            _ = connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema_name))
            )


def _connect(schema_name: str) -> AbstractContextManager[psycopg.Connection[tuple[object, ...]]]:
    @contextmanager
    def connection() -> Generator[psycopg.Connection[tuple[object, ...]]]:
        settings = PostgresContractSettings.from_environment()
        with psycopg.connect(settings.postgres_dsn, autocommit=True) as item:
            _ = item.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema_name)))
            yield item

    return connection()


def test_mcp_acquisition_lifecycle_uses_independent_authority(authority_schema: str) -> None:
    with _connect(authority_schema) as connection:
        _ = apply_migrations(connection)
    server = create_mcp_server(
        McpDependencies(connect=lambda: _connect(authority_schema), actor="owner", now=lambda: _NOW)
    )

    async def exercise() -> None:
        async with Client(server) as client:
            active = ActiveAcquisitionPolicy.model_validate(
                (await client.call_tool("acquisition_active_get", {})).structured_content
            )
            draft = AcquisitionPolicyDraft.model_validate(
                (await client.call_tool("acquisition_draft_get", {})).structured_content
            )
            candidate = draft.policy.model_copy(
                update={
                    "search_keywords": (*draft.policy.search_keywords, "site reliability engineer")
                }
            )
            saved = (
                await client.call_tool(
                    "acquisition_draft_update",
                    {
                        "expected_base_revision_id": draft.base_revision_id,
                        "expected_version": draft.version,
                        "policy": candidate.model_dump(mode="json"),
                    },
                )
            ).structured_content
            assert saved is not None
            saved_result = TypeAdapter(dict[str, object]).validate_python(
                saved.get("result", saved)
            )
            assert saved_result["kind"] == "saved"
            revision_id = acquisition_policy_revision_id(candidate)
            published = AcquisitionPublicationReceipt.model_validate(
                (
                    await client.call_tool(
                        "acquisition_publish",
                        {
                            "idempotency_key": "mcp-acquisition-publish",
                            "expected_draft_version": draft.version + 1,
                            "expected_revision_id": revision_id,
                        },
                    )
                ).structured_content
            )
            assert published.outcome == "published"
            activation = {
                "idempotency_key": "mcp-acquisition-activate",
                "candidate_revision_id": revision_id,
                "expected_revision_id": active.revision.id,
                "expected_generation": active.generation,
            }
            activated = AcquisitionActivationReceipt.model_validate(
                (await client.call_tool("acquisition_activate", activation)).structured_content
            )
            assert activated.outcome == "activated"
            replayed = AcquisitionActivationReceipt.model_validate(
                (await client.call_tool("acquisition_activate", activation)).structured_content
            )
            assert replayed.replayed
            latest = ActiveAcquisitionPolicy.model_validate(
                (await client.call_tool("acquisition_active_get", {})).structured_content
            )
            assert latest.revision.id == revision_id
            assert latest.generation == active.generation + 1

    asyncio.run(exercise())
