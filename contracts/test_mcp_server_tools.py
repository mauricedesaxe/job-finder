from __future__ import annotations

import asyncio
from collections.abc import Generator
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from typing import Callable
from uuid import UUID, uuid4

import psycopg
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from psycopg import sql

from job_finder.config import PostgresContractSettings
from job_finder.database import apply_migrations
from job_finder.evaluation import (
    CuratedReviewEvent,
    ManifestSummary,
    ManifestSummaryPage,
    ProjectionQueueStatus,
    bootstrap_prompt_release,
)
from job_finder.mcp_server import Connection, McpDependencies, create_mcp_server
from job_finder.review.models import (
    ReviewFeedback,
    ReviewFeedbackPage,
    ReviewSaved,
    ReviewSubmission,
)
from job_finder.review.postgres import (
    enqueue_qualified_review_item,
    load_review_queue,
    record_review,
)
from job_finder.search_configuration import load_active_search_configuration
from scripts.serve_mcp import create_server

_NOW = datetime(2026, 9, 19, 12, tzinfo=UTC)
_RAW_URL = "https://example.com/jobs/mcp-contract"
_TOOL_NAMES = {
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


@pytest.fixture
def authority_schema() -> Generator[str]:
    settings = PostgresContractSettings.from_environment()
    schema_name = f"job_finder_mcp_contract_{uuid4().hex}"
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
        try:
            yield schema_name
        finally:
            connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema_name))
            )


@contextmanager
def _connection(schema_name: str) -> Generator[Connection]:
    settings = PostgresContractSettings.from_environment()
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema_name)))
        yield connection


def _mcp_connect(schema_name: str) -> Callable[[], AbstractContextManager[Connection]]:
    @contextmanager
    def connect() -> Generator[Connection]:
        with _connection(schema_name) as connection:
            yield connection

    return connect


def _seed_pursued_feedback(connection: Connection) -> UUID:
    run_id = uuid4()
    release = bootstrap_prompt_release(connection)
    connection.execute(
        """
        INSERT INTO pipeline_runs (
          id, idempotency_key, kind, implementation_ref, prompt_release_id, parameters,
          status, started_at, completed_at
        ) VALUES (%s, %s, 'processing', 'mcp-contract', %s, '{}'::jsonb,
          'completed', %s, %s)
        """,
        (run_id, f"decision:{run_id}", release.id, _NOW, _NOW),
    )
    job_id = uuid4()
    snapshot_id = f"{uuid4().int:064x}"
    evaluation_id = f"{uuid4().int:064x}"
    connection.execute(
        """
        INSERT INTO jobs (id, raw_url, first_discovered_at, last_discovered_at)
        VALUES (%s, %s, %s, %s)
        """,
        (job_id, _RAW_URL, _NOW, _NOW),
    )
    connection.execute(
        """
        INSERT INTO job_snapshots (
          id, job_id, content_digest, title, company, normalized_company,
          normalized_title, source, raw_url, description, location, keywords,
          date_posted, observed_at
        ) VALUES (%s, %s, %s, 'Product Engineer', 'Acme', 'acme', 'product engineer',
          'other', %s, 'Build useful products.', 'Remote', '["python"]'::jsonb, %s, %s)
        """,
        (snapshot_id, job_id, f"{uuid4().int:064x}", _RAW_URL, _NOW.date(), _NOW),
    )
    connection.execute(
        """
        INSERT INTO evaluation_decisions (
          id, snapshot_id, pipeline_run_id, prompt_release_id, policy_version,
          outcome, matched_profile, reason, created_at
        ) VALUES (%s, %s, %s, %s, 'policy-1', 'qualified',
          'early-stage-product-engineer', 'Strong scope', %s)
        """,
        (evaluation_id, snapshot_id, run_id, release.id, _NOW),
    )
    assert enqueue_qualified_review_item(connection, evaluation_id, _NOW.date())
    item = load_review_queue(connection).items[0]
    saved = record_review(
        connection,
        ReviewSubmission(
            review_item_id=item.id,
            evaluation_id=item.evaluation_id,
            snapshot_id=item.snapshot_id,
            decision="pursue",
            target_profile="early-stage-product-engineer",
            primary_reason="role-scope",
            note="Strong product ownership.",
            actor="owner",
            created_at=_NOW,
        ),
    )
    assert isinstance(saved, ReviewSaved)
    return saved.review_event_id


def test_mcp_tools_serve_real_database_state(authority_schema: str) -> None:
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        review_event_id = _seed_pursued_feedback(connection)

    server = create_mcp_server(
        McpDependencies(
            connect=_mcp_connect(authority_schema), actor="contract-owner", now=lambda: _NOW
        )
    )

    async def exercise() -> None:
        async with Client(server) as client:
            listed = ReviewFeedbackPage.model_validate(
                (
                    await client.call_tool("feedback_list", {"curation": "uncurated"})
                ).structured_content
            )
            assert tuple(item.review_event_id for item in listed.items) == (review_event_id,)
            assert listed.items[0].decision == "pursue"
            assert listed.items[0].title == "Product Engineer"
            assert listed.items[0].company == "Acme"

            feedback = ReviewFeedback.model_validate(
                (
                    await client.call_tool(
                        "feedback_get", {"review_event_id": str(review_event_id)}
                    )
                ).structured_content
            )
            assert feedback.curation is None
            assert feedback.original_outcome == "qualified"

            with pytest.raises(ToolError, match="Review feedback does not exist"):
                _ = await client.call_tool("feedback_get", {"review_event_id": str(uuid4())})

            curated = CuratedReviewEvent.model_validate(
                (
                    await client.call_tool(
                        "feedback_curate",
                        {
                            "review_event_id": str(review_event_id),
                            "action": "include",
                            "reason": "Known false negative.",
                            "idempotency_key": "curation:contract",
                            "critical": True,
                        },
                    )
                ).structured_content
            )
            assert curated.review_event_id == review_event_id
            assert curated.action == "include"
            assert curated.critical is True
            assert curated.actor == "contract-owner"
            assert curated.created_at == _NOW

            with pytest.raises(ToolError, match="Excluded feedback cannot be marked critical"):
                _ = await client.call_tool(
                    "feedback_curate",
                    {
                        "review_event_id": str(review_event_id),
                        "action": "exclude",
                        "reason": "Not useful.",
                        "idempotency_key": "curation:rejected",
                        "critical": True,
                    },
                )

            curated_feedback = ReviewFeedback.model_validate(
                (
                    await client.call_tool(
                        "feedback_get", {"review_event_id": str(review_event_id)}
                    )
                ).structured_content
            )
            assert curated_feedback.curation is not None
            assert curated_feedback.curation.actor == "contract-owner"

            preview = ManifestSummary.model_validate(
                (await client.call_tool("manifest_preview", {})).structured_content
            )
            assert preview.id == "0" * 64
            assert preview.case_count == 1
            assert preview.critical_count == 1
            assert preview.trial_count == 3

            created = ManifestSummary.model_validate(
                (
                    await client.call_tool(
                        "manifest_create", {"idempotency_key": "manifest:contract"}
                    )
                ).structured_content
            )
            assert created.created_by == "contract-owner"
            assert created.case_count == 1
            recreated = ManifestSummary.model_validate(
                (
                    await client.call_tool(
                        "manifest_create", {"idempotency_key": "manifest:contract"}
                    )
                ).structured_content
            )
            assert recreated.id == created.id

            fetched = ManifestSummary.model_validate(
                (
                    await client.call_tool("manifest_get", {"manifest_id": created.id})
                ).structured_content
            )
            assert fetched.id == created.id
            with pytest.raises(ToolError, match="Evaluation manifest does not exist"):
                _ = await client.call_tool("manifest_get", {"manifest_id": "a" * 64})

            manifests = ManifestSummaryPage.model_validate(
                (await client.call_tool("manifest_list", {})).structured_content
            )
            assert created.id in {item.id for item in manifests.items}

            projection = ProjectionQueueStatus.model_validate(
                (await client.call_tool("langfuse_projection_status", {})).structured_content
            )
            assert projection.pending_count >= 1

    asyncio.run(exercise())


def test_serve_mcp_builds_the_server_from_the_environment(
    authority_schema: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = PostgresContractSettings.from_environment()
    monkeypatch.setenv(
        "JOB_FINDER_POSTGRES_DSN",
        f"{settings.postgres_dsn}?options=-csearch_path%3D{authority_schema}",
    )

    server = create_server()

    async def exercise() -> set[str]:
        async with Client(server) as client:
            return {tool.name for tool in await client.list_tools()}

    assert asyncio.run(exercise()) == _TOOL_NAMES
    with _connection(authority_schema) as connection:
        assert load_active_search_configuration(connection).generation == 0
