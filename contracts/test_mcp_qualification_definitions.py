from __future__ import annotations

import asyncio
from collections.abc import Generator, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4

import psycopg
import pytest
from fastmcp import Client
from pydantic import TypeAdapter
from psycopg import sql

from contracts.test_postgres_authority import _store_default_qualification_target  # pyright: ignore[reportPrivateUsage]
from job_finder.config import PostgresContractSettings
from job_finder.database import apply_migrations
from job_finder.evaluation.implementation_artifacts import write_implementation_artifact
from job_finder.evaluation.qualification_components import qualification_target_id
from job_finder.mcp_server import McpDependencies, create_mcp_server
from job_finder.qualification_definition import (
    PersonalCriterion,
    qualification_definition_revision_id,
)
from job_finder.qualification_definition_service import (
    QualificationDefinitionDraft,
    QualificationPublicationReceipt,
)

_NOW = datetime(2026, 9, 25, tzinfo=UTC)


@pytest.fixture
def authority_schema() -> Iterator[str]:
    settings = PostgresContractSettings.from_environment()
    name = f"job_finder_qualification_definition_mcp_{uuid4().hex}"
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as connection:
        _ = connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
        try:
            yield name
        finally:
            _ = connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


def _connect(name: str) -> AbstractContextManager[psycopg.Connection[tuple[object, ...]]]:
    @contextmanager
    def connection() -> Generator[psycopg.Connection[tuple[object, ...]]]:
        settings = PostgresContractSettings.from_environment()
        with psycopg.connect(settings.postgres_dsn, autocommit=True) as item:
            _ = item.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(name)))
            yield item

    return connection()


def test_mcp_edits_publishes_and_compiles_qualification(authority_schema: str) -> None:
    root = Path(__file__).resolve().parents[1]
    with NamedTemporaryFile(dir=root, suffix=".json") as temporary:
        artifact_path = Path(temporary.name)
        with _connect(authority_schema) as connection:
            _ = apply_migrations(connection)
            _, _, target = _store_default_qualification_target(connection, _NOW)
            _ = write_implementation_artifact(root, artifact_path)
        server = create_mcp_server(
            McpDependencies(
                connect=lambda: _connect(authority_schema),
                actor="owner",
                now=lambda: _NOW,
                implementation_artifact_path=artifact_path,
            )
        )

        async def exercise() -> None:
            async with Client(server) as client:
                draft = QualificationDefinitionDraft.model_validate(
                    (
                        await client.call_tool("qualification_definition_draft_get", {})
                    ).structured_content
                )
                changed = draft.definition.model_copy(
                    update={
                        "personal_criteria": (
                            *draft.definition.personal_criteria,
                            PersonalCriterion(
                                key="remote-option",
                                name="Remote option",
                                instructions="Prefer a clear remote option.",
                            ),
                        )
                    }
                )
                saved = await client.call_tool(
                    "qualification_definition_draft_update",
                    {
                        "expected_base_revision_id": draft.base_revision_id,
                        "expected_version": draft.version,
                        "definition": changed.model_dump(mode="json"),
                    },
                )
                assert saved.structured_content is not None
                saved_result = TypeAdapter(dict[str, object]).validate_python(
                    saved.structured_content.get("result", saved.structured_content)
                )
                assert saved_result["kind"] == "saved"
                revision_id = qualification_definition_revision_id(changed)
                receipt = QualificationPublicationReceipt.model_validate(
                    (
                        await client.call_tool(
                            "qualification_definition_publish",
                            {
                                "idempotency_key": "publish-definition",
                                "expected_draft_version": draft.version + 1,
                                "expected_revision_id": revision_id,
                            },
                        )
                    ).structured_content
                )
                assert receipt.publication_revision_id == revision_id
                compiled = await client.call_tool(
                    "qualification_candidate_compile",
                    {"target_id": qualification_target_id(target)},
                )
                assert not compiled.is_error

        asyncio.run(exercise())
        with _connect(authority_schema) as connection:
            assert connection.execute(
                "SELECT count(*) FROM qualification_prompt_compilations WHERE target_id = %s",
                (qualification_target_id(target),),
            ).fetchone() == (1,)
