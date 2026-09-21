from __future__ import annotations

import asyncio
from collections.abc import Generator
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Callable
from uuid import UUID, uuid4

import psycopg
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from psycopg import sql
from pydantic import TypeAdapter

from job_finder.config import PostgresContractSettings
from job_finder.database import apply_migrations
from job_finder.discovery.exchange_rates import ExchangeRateSnapshot
from job_finder.evaluation import (
    ActiveReleaseTarget,
    ActivateReleaseTargetResult,
    ActiveReleaseTargetChanged,
    CompletedEvaluationExecution,
    CuratedReviewEvent,
    EvaluateManifestCommand,
    EvaluationManifestCase,
    ManifestPolicy,
    ManifestSummary,
    ManifestSummaryPage,
    PromptPromotionDecision,
    ProjectionQueueStatus,
    ReleaseTarget,
    ReleaseTargetActivated,
    bootstrap_prompt_release,
    create_manifest,
    include_review_event,
    run_manifest,
)
from job_finder.evaluation.manifests import EvaluationRunComparison
from job_finder.evaluation.models import EvaluationResult, Qualified
from job_finder.evaluation.prompt_releases import load_prompt_release
from job_finder.evaluation.relevance_releases import build_jev_faithful_policy
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
    "configuration_draft_update",
    "configuration_publish",
    "configuration_revision_list",
    "configuration_revision_get",
    "configuration_activate",
}
_ACTIVATION_RESULT: TypeAdapter[ActivateReleaseTargetResult] = TypeAdapter(
    ActivateReleaseTargetResult
)


def _unwrap_tool_union(content: dict[str, object] | None) -> object:
    assert content is not None
    return content.get("result", content)


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
            active_target = ActiveReleaseTarget.model_validate(
                (await client.call_tool("release_target_active_get", {})).structured_content
            )
            candidate = ReleaseTarget.model_validate(
                (
                    await client.call_tool(
                        "release_target_candidate_create",
                        {"prompt_release_id": active_target.target.prompt_release_id},
                    )
                ).structured_content
            )
            assert candidate == active_target.target

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


def test_mcp_release_lifecycle_activates_exact_approved_target(authority_schema: str) -> None:
    rates = ExchangeRateSnapshot(rates={"EUR": Decimal("1")}, source="fallback", observed_at=_NOW)
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        review_event_id = _seed_pursued_feedback(connection)
        include_review_event(
            connection,
            review_event_id=review_event_id,
            critical=False,
            reason="Release lifecycle contract case.",
            actor="contract-owner",
            created_at=_NOW,
            idempotency_key="release-lifecycle:curation",
        )
        manifest = create_manifest(
            connection,
            policy=ManifestPolicy(),
            created_at=_NOW,
            created_by="contract-owner",
            idempotency_key="release-lifecycle:manifest",
        )

    def run_evaluation(
        connection: Connection, command: EvaluateManifestCommand
    ) -> CompletedEvaluationExecution:
        def evaluate(
            _case: EvaluationManifestCase, _target: ReleaseTarget, _trial: int
        ) -> EvaluationResult:
            return Qualified(reason="Expected contract result.", profile_name="profile")

        execution = run_manifest(
            connection,
            command=command,
            create_exchange_rates=lambda: rates,
            create_evaluator=lambda _rates, _record: evaluate,
            now=lambda: _NOW,
        )
        assert isinstance(execution, CompletedEvaluationExecution)
        return execution

    server = create_mcp_server(
        McpDependencies(
            connect=_mcp_connect(authority_schema),
            actor="contract-owner",
            now=lambda: _NOW,
            run_evaluation=run_evaluation,
        )
    )

    async def exercise() -> None:
        async with Client(server) as client:
            active = ActiveReleaseTarget.model_validate(
                (await client.call_tool("release_target_active_get", {})).structured_content
            )
            with _connection(authority_schema) as connection:
                prompt_release = load_prompt_release(connection, active.target.prompt_release_id)
            candidate = ReleaseTarget.model_validate(
                (
                    await client.call_tool(
                        "release_target_candidate_create",
                        {
                            "prompt_release_id": active.target.prompt_release_id,
                            "relevance_policy": build_jev_faithful_policy(
                                prompt_release
                            ).model_dump(mode="json"),
                        },
                    )
                ).structured_content
            )
            assert candidate != active.target

            with pytest.raises(ToolError, match="Prompt release not found"):
                await client.call_tool(
                    "release_target_candidate_create",
                    {"prompt_release_id": "0" * 64},
                )
            with pytest.raises(ToolError, match="Evaluation execution does not exist"):
                await client.call_tool("evaluation_execution_get", {"execution_id": "0" * 64})
            with pytest.raises(ToolError, match="Evaluation run does not exist"):
                await client.call_tool("evaluation_run_get", {"run_id": "0" * 64})

            executions: list[CompletedEvaluationExecution] = []
            with pytest.raises(ToolError, match="Relevance release not found"):
                await client.call_tool(
                    "evaluation_run",
                    {
                        "idempotency_key": "release-lifecycle:invalid-target",
                        "manifest_id": manifest.id,
                        "target": {
                            "prompt_release_id": active.target.prompt_release_id,
                            "relevance_release_id": "0" * 64,
                        },
                        "implementation_ref": "contract",
                    },
                )
            for key, target in (
                ("release-lifecycle:baseline", active.target),
                ("release-lifecycle:candidate", candidate),
            ):
                content = (
                    await client.call_tool(
                        "evaluation_run",
                        {
                            "idempotency_key": key,
                            "manifest_id": manifest.id,
                            "target": target.model_dump(mode="json"),
                            "implementation_ref": "contract",
                        },
                    )
                ).structured_content
                executions.append(
                    CompletedEvaluationExecution.model_validate(_unwrap_tool_union(content))
                )

            with pytest.raises(ToolError, match="different evaluation execution"):
                await client.call_tool(
                    "evaluation_run",
                    {
                        "idempotency_key": "release-lifecycle:baseline",
                        "manifest_id": manifest.id,
                        "target": candidate.model_dump(mode="json"),
                        "implementation_ref": "contract",
                    },
                )

            fetched_execution = CompletedEvaluationExecution.model_validate(
                _unwrap_tool_union(
                    (
                        await client.call_tool(
                            "evaluation_execution_get", {"execution_id": executions[0].id}
                        )
                    ).structured_content
                )
            )
            assert fetched_execution == executions[0]
            run_ids = [execution.run.id for execution in executions]
            fetched_run = (
                await client.call_tool("evaluation_run_get", {"run_id": run_ids[0]})
            ).structured_content
            assert fetched_run is not None
            assert fetched_run["target"] == active.target.model_dump(mode="json")

            comparison = EvaluationRunComparison.model_validate(
                (
                    await client.call_tool(
                        "release_target_compare",
                        {"baseline_run_id": run_ids[0], "candidate_run_id": run_ids[1]},
                    )
                ).structured_content
            )
            assert comparison.eligible is True
            with pytest.raises(ToolError, match="release targets must differ"):
                await client.call_tool(
                    "release_target_compare",
                    {
                        "baseline_run_id": run_ids[0],
                        "candidate_run_id": run_ids[0],
                    },
                )
            with pytest.raises(ToolError, match="evidence is stale"):
                await client.call_tool(
                    "release_target_decide",
                    {
                        "baseline_run_id": run_ids[0],
                        "candidate_run_id": run_ids[1],
                        "expected_comparison_id": "0" * 64,
                        "decision": "approved",
                        "reason": "Stale evidence must remain visible.",
                        "idempotency_key": "release-lifecycle:stale-decision",
                    },
                )
            decision = PromptPromotionDecision.model_validate(
                (
                    await client.call_tool(
                        "release_target_decide",
                        {
                            "baseline_run_id": run_ids[0],
                            "candidate_run_id": run_ids[1],
                            "expected_comparison_id": comparison.id,
                            "decision": "approved",
                            "reason": "Contract candidate passed.",
                            "idempotency_key": "release-lifecycle:decision",
                        },
                    )
                ).structured_content
            )
            activation_input = {
                "promotion_decision_id": decision.id,
                "expected_active_target": active.target.model_dump(mode="json"),
                "expected_generation": active.generation,
                "idempotency_key": "release-lifecycle:activation",
            }
            activated = _ACTIVATION_RESULT.validate_python(
                _unwrap_tool_union(
                    (
                        await client.call_tool("release_target_activate", activation_input)
                    ).structured_content
                )
            )
            replayed = _ACTIVATION_RESULT.validate_python(
                _unwrap_tool_union(
                    (
                        await client.call_tool("release_target_activate", activation_input)
                    ).structured_content
                )
            )
            assert isinstance(activated, ReleaseTargetActivated)
            assert activated.replayed is False
            assert activated.active.target == candidate
            assert isinstance(replayed, ReleaseTargetActivated)
            assert replayed.replayed is True

            stale = _ACTIVATION_RESULT.validate_python(
                _unwrap_tool_union(
                    (
                        await client.call_tool(
                            "release_target_activate",
                            {**activation_input, "idempotency_key": "release-lifecycle:stale"},
                        )
                    ).structured_content
                )
            )
            assert isinstance(stale, ActiveReleaseTargetChanged)
            assert stale.active.target == candidate

    asyncio.run(exercise())
