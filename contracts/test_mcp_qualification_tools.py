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
from fastmcp.exceptions import ToolError
from psycopg import sql

from contracts.test_postgres_authority import _store_default_qualification_target  # pyright: ignore[reportPrivateUsage]
from job_finder.benchmarks.qualification_activation import ActiveQualificationTarget
from job_finder.benchmarks.qualification_promotions import (
    QualificationPromotionDecision,
    QualificationPromotionPreview,
)
from job_finder.config import PostgresContractSettings
from job_finder.database import apply_migrations
from job_finder.evaluation.implementation_artifacts import write_implementation_artifact
from job_finder.evaluation.qualification_components import (
    ResolvedQualificationTarget,
    qualification_target_id,
)
from job_finder.mcp_server import McpDependencies, create_mcp_server

_NOW = datetime(2026, 9, 25, tzinfo=UTC)


@pytest.fixture
def authority_schema() -> Iterator[str]:
    settings = PostgresContractSettings.from_environment()
    schema_name = f"job_finder_qualification_mcp_{uuid4().hex}"
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


def test_mcp_qualification_candidate_and_rejected_decision(authority_schema: str) -> None:
    root = Path(__file__).resolve().parents[1]
    with NamedTemporaryFile(dir=root, prefix=".qualification-mcp-", suffix=".json") as temporary:
        artifact_path = Path(temporary.name)
        with _connect(authority_schema) as connection:
            _ = apply_migrations(connection)
            artifact, components, baseline = _store_default_qualification_target(connection, _NOW)
            assert write_implementation_artifact(root, artifact_path) == artifact
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
                active = ActiveQualificationTarget.model_validate(
                    (await client.call_tool("qualification_active_get", {})).structured_content
                )
                assert active.target_id is None and active.generation == 0
                alternate_input = components[0].model_copy(
                    update={"ats_sources": components[0].ats_sources[:-1]}
                )
                candidate = ResolvedQualificationTarget.model_validate(
                    (
                        await client.call_tool(
                            "qualification_candidate_create",
                            {
                                "input_preparation": alternate_input.model_dump(mode="json"),
                                "relevance": components[1].model_dump(mode="json"),
                                "enrichment": components[2].model_dump(mode="json"),
                                "deduplication": components[3].model_dump(mode="json"),
                            },
                        )
                    ).structured_content
                )
                assert candidate.id != qualification_target_id(baseline)
                fetched = ResolvedQualificationTarget.model_validate(
                    (
                        await client.call_tool(
                            "qualification_candidate_get",
                            {
                                "target_id": candidate.id,
                            },
                        )
                    ).structured_content
                )
                assert fetched == candidate
                selection: dict[str, object] = {}
                preview = QualificationPromotionPreview.model_validate(
                    (
                        await client.call_tool(
                            "qualification_promotion_preview",
                            {
                                "baseline_target_id": qualification_target_id(baseline),
                                "candidate_target_id": candidate.id,
                                "evidence": selection,
                            },
                        )
                    ).structured_content
                )
                assert not preview.eligible
                rejected = QualificationPromotionDecision.model_validate(
                    (
                        await client.call_tool(
                            "qualification_promotion_decide",
                            {
                                "baseline_target_id": qualification_target_id(baseline),
                                "candidate_target_id": candidate.id,
                                "evidence": selection,
                                "decision": "rejected",
                                "reason": "Evidence missing",
                                "idempotency_key": "qualification-mcp-rejected",
                            },
                        )
                    ).structured_content
                )
                assert rejected.decision == "rejected"
                with pytest.raises(ToolError, match="Approved qualification promotion"):
                    _ = await client.call_tool(
                        "qualification_activate",
                        {
                            "idempotency_key": "qualification-mcp-activation",
                            "promotion_decision_id": rejected.id,
                            "expected_target_id": None,
                            "expected_generation": 0,
                        },
                    )

        asyncio.run(exercise())


def test_qualification_tools_require_a_configured_build_artifact(authority_schema: str) -> None:
    server = create_mcp_server(
        McpDependencies(connect=lambda: _connect(authority_schema), actor="owner", now=lambda: _NOW)
    )

    async def exercise() -> None:
        async with Client(server) as client:
            with pytest.raises(ToolError, match="Executing build artifact is not configured"):
                _ = await client.call_tool(
                    "qualification_promotion_preview",
                    {
                        "baseline_target_id": "a" * 64,
                        "candidate_target_id": "b" * 64,
                        "evidence": {},
                    },
                )

    asyncio.run(exercise())
