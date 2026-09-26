# ruff: noqa: F401
from __future__ import annotations

import base64
from typing import Literal
from collections.abc import Callable, Generator, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from secrets import token_hex
from threading import Barrier, Event
from tempfile import NamedTemporaryFile
from typing import LiteralString, cast
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import JsonValue, SecretStr, TypeAdapter

import job_finder.configuration_service as configuration_service_module
import job_finder.evaluation.manifest_execution as manifest_execution_module
import job_finder.evaluation.relevance_releases as relevance_releases_module
from job_finder.acquisition_policy import AcquisitionPolicy, acquisition_policy_revision_id
from job_finder.ats.models import AtsAvailable, AtsNotApplicable, CompensationObservation
from job_finder.ats.policy import format_ats_description
from job_finder.benchmarks.comparisons import preview_run_comparison
from job_finder.benchmarks.executions import (
    CompletedEvaluationExecution,
    EvaluateManifestCommand,
    FailedEvaluationExecution,
    exchange_rate_snapshot_digest,
    load_evaluation_execution_by_key,
    load_run,
    run_manifest,
)
from job_finder.benchmarks.manifests import (
    EvaluationManifestCase,
    ManifestPolicy,
    create_manifest,
    exclude_review_event,
    include_review_event,
    list_manifests,
    preview_manifest,
)
from job_finder.benchmarks.manifests import load_manifest
from job_finder.benchmarks.promotions import record_prompt_promotion_decision
from job_finder.benchmarks.scoring import EvaluationMetrics, score_results, score_trial
from job_finder.benchmarks.qualification_activation import (
    ActivateQualificationTargetCommand,
    QualificationActivationError,
    activate_qualification_target,
    get_active_qualification_target,
)
from job_finder.benchmarks.qualification_promotions import (
    PromotionEvidenceSelection,
    preview_qualification_promotion,
    record_qualification_promotion_decision,
)
from job_finder.benchmarks.qualification_evidence import (
    FixtureCase,
    PhaseFixtureSet,
    ProviderExperimentSettings,
    QualificationEvidence,
    QualificationEvidenceId,
    RelevanceExperimentInput,
    qualification_evidence_id,
    record_relevance_comparison,
    store_fixture_set,
    store_qualification_evidence,
    store_relevance_experiment_input,
)
from job_finder.benchmarks.input_preparation_execution import (
    execute_input_preparation_fixture_set,
)
from job_finder.benchmarks.enrichment_execution import execute_enrichment_fixture_set
from job_finder.benchmarks.deduplication_execution import execute_deduplication_fixture_set
from job_finder.benchmarks.composition_execution import execute_composition_fixture_set
from job_finder.benchmarks.provider_attempts import (
    provider_attempt_evidence,
    store_provider_attempts,
)
from job_finder.benchmarks.relevance_execution import execute_relevance_experiment
from job_finder.config import PostgresContractSettings
from job_finder.configuration_service import (
    ActivationTargetUnpublished,
    ActiveConfigurationChanged,
    ActivateConfigurationCommand,
    ConfigurationActivated,
    ConfigurationPublished,
    ConfigurationRevisionCursor,
    ConfigurationRevisionNotFound,
    DraftChanged,
    DraftSaved,
    PublicationIdempotencyKeyConflict,
    PublishConfigurationCommand,
    PublishDraftChanged,
    SaveDraftCommand,
    activate_search_configuration,
    get_active_search_configuration,
    get_search_configuration_draft,
    get_search_configuration_revision,
    list_search_configuration_revisions,
    load_published_active_search_configuration,
    publish_search_configuration,
    save_search_configuration_draft,
)
from job_finder.database import (
    INITIAL_SEARCH_CONFIGURATION_REVISION_ID,
    MIGRATIONS_PATH,
    apply_migrations,
)
from job_finder.evaluation.implementation_artifacts import (
    ImplementationArtifact,
    build_implementation_artifact,
    implementation_artifact_id,
    store_implementation_artifact,
    write_implementation_artifact,
)
from job_finder.evaluation.qualification_components import (
    DeduplicationContent,
    EnrichmentContent,
    InputPreparationContent,
    RelevanceContent,
    QualificationTargetContent,
    QualificationTargetId,
    build_qualification_target,
    component_release_id,
    load_qualification_target,
    qualification_target_id,
    store_component_release,
    store_qualification_target,
)
from job_finder.evaluation.qualification_prompt_compilations import (
    bind_qualification_prompt_release,
    load_compiled_qualification_target,
)
from job_finder.policy_projection import project_legacy_search_configuration
from job_finder.qualification_definition import (
    QualificationDefinition,
    QualificationDefinitionRevisionId,
    qualification_definition_revision_id,
)
from job_finder.projections.outbox import (
    LangfuseProjection,
    LangfuseUnavailable,
    ProjectionDelivered,
    ProjectionFailed,
    ProjectionIdle,
    ProjectionLeaseLost,
    deliver_next_projection,
    load_projection_queue_status,
)
from job_finder.projections.rebuild import rebuild_langfuse_projections
from job_finder.evaluation.prompt_releases import (
    PromptRelease,
    bootstrap_prompt_release,
    load_prompt_release,
)
from job_finder.execution_budget import (
    BudgetSaved,
    ExecutionAdmitted,
    ExecutionBlocked,
    admit_scheduled_execution,
    postgres_budget_setup_service,
    reserve_discovery,
    reserve_job_capacity,
)
from job_finder.evaluation.release_targets import (
    ActivateReleaseTargetCommand,
    ActiveReleaseTargetChanged,
    ReleaseTargetActivated,
    ReleaseTargetLifecycleError,
    activate_release_target,
    get_active_release_target,
)
from job_finder.evaluation.models import (
    CriterionAccepted,
    EvaluationResult,
    InputDigest,
    ModelCallAttempt,
    ModelRequestId,
    RetryableOperationalError,
    TerminalOperationalError,
    ModelCallContext,
    PromptAccepted,
    ProviderRequestObservation,
    PromptReleaseId,
    PromptVersionId,
    Qualified,
    Rejected,
    ReleaseTarget,
    RelevanceReleaseId,
)
from job_finder.evaluation.manifest_execution import run_stored_manifest
from job_finder.jobs.decision_persistence import postgres_decision_store
from job_finder.jobs.decisions import (
    DecisionContext,
    PersistedDecision,
    process_qualified_job,
)
from job_finder.jobs.enrichment import EnrichedJob
from job_finder.jobs.listings import JobListing
from job_finder.jobs.title_deduplication import TitleDuplicate
from job_finder.review.feedback import (
    ReviewSaved,
    ReviewSubmission,
    list_review_feedback,
    load_review_feedback,
    record_review,
)
from job_finder.review.owner_access import (
    OnboardingStage,
    OwnerBootstrapped,
    import_legacy_owner_password,
    postgres_owner_access_service,
)
from job_finder.review.queue import (
    deterministic_rejected_sample,
    enqueue_qualified_review_item,
    enqueue_rejected_audit_sample,
    load_review_queue,
)
from scripts.serve_review import create_app as create_review_server
from job_finder.search_configuration import (
    DEFAULT_SEARCH_CONFIGURATION,
    SearchConfigurationDraft,
    SearchConfigurationRevision,
    SearchConfigurationRevisionId,
    SupportedSearchSource,
    build_search_configuration_revision,
    compare_and_swap_active_search_configuration,
    load_active_search_configuration,
    load_search_configuration_draft,
    load_search_configuration_publication,
    load_search_configuration_revision,
    replace_search_configuration_draft,
    search_configuration_revision_id,
    store_search_configuration_revision,
)
from job_finder.evaluation.openrouter import (
    HttpResponse,
    RetryPolicy,
    enqueue_model_call_projection,
    evaluate_prompt,
    postgres_model_call_persistence,
    prompt_input_digest,
)
from job_finder.evaluation.jev import JEV_MODEL, JevHttpResponse
from job_finder.evaluation.prompts import ENRICHMENT, PROMPTS
from job_finder.evaluation.prompt_releases import (
    PromptReleaseError,
    build_prompt_release,
    store_prompt_release,
)
from job_finder.evaluation.relevance_releases import (
    CodeArtifactIdentity,
    JevFaithfulExecutionPolicy,
    RelevanceReleaseError,
    build_gemini_policy,
    build_jev_atomic_policy,
    build_jev_faithful_policy,
    build_relevance_release,
    load_relevance_release,
    store_relevance_release,
    validate_release_target,
)
from job_finder.discovery.exchange_rates import ExchangeRateSnapshot
from job_finder.pipeline.runs import prepare_orchestration_run
from job_finder.provider_credentials import (
    ProviderCapability,
    ProviderCredentialChanged,
    ProviderCredentialStored,
    ProviderKind,
    ProviderValidation,
    credential_cipher,
    postgres_provider_setup_service,
    resolve_execution_provider_credentials,
)


EXPECTED_MIGRATIONS = (
    "0001_authoritative_job_state.sql",
    "0002_model_call_response_model.sql",
    "0003_one_review_event_per_item.sql",
    "0004_evaluation_manifests.sql",
    "0005_dagster_orchestration.sql",
    "0006_stored_prompt_execution.sql",
    "0007_model_call_request_messages.sql",
    "0008_pending_usage_response_model.sql",
    "0009_frozen_daily_reviews.sql",
    "0010_review_event_revisions.sql",
    "0011_review_queue.sql",
    "0012_company_application_cooldown.sql",
    "0013_snapshot_compensation.sql",
    "0014_snapshot_corrections.sql",
    "0015_manifest_idempotency.sql",
    "0016_search_configuration_revisions.sql",
    "0017_search_configuration_publications.sql",
    "0018_published_search_configuration_pointers.sql",
    "0019_configuration_publication_receipts.sql",
    "0020_pipeline_run_configuration_revisions.sql",
    "0021_typesafe_model_provider.sql",
    "0022_relevance_releases.sql",
    "0023_unbounded_review_event_notes.sql",
    "0024_evaluation_run_executions.sql",
    "0025_release_target_promotion_decisions.sql",
    "0026_release_target_lifecycle.sql",
    "0027_work_recovery_receipts.sql",
    "0028_append_only_job_reevaluations.sql",
    "0029_owner_onboarding.sql",
    "0030_provider_credentials.sql",
    "0031_execution_budget.sql",
    "0032_legacy_execution_budget.sql",
    "0033_onboarding_test_search_requests.sql",
    "0034_work_dismissals.sql",
    "0035_onboarding_work_scope.sql",
    "0036_execution_budget_authority.sql",
    "0037_run_budget_authority_guard.sql",
    "0038_policy_revisions.sql",
    "0039_policy_lifecycle_state.sql",
    "0040_qualification_components.sql",
    "0041_qualification_evidence.sql",
    "0042_qualification_prompt_compilations.sql",
    "0043_qualification_provider_attempts.sql",
    "0044_qualification_promotion_authority.sql",
    "0045_unique_qualification_promotion_pair.sql",
    "0046_split_run_authority.sql",
    "0047_split_reservation_authority.sql",
    "0048_split_run_execution_projections.sql",
    "0049_split_onboarding_request_authority.sql",
    "0050_qualification_evidence_executions.sql",
    "0051_qualification_first_activation.sql",
    "0052_review_accounts.sql",
    "0053_individual_owner_claim.sql",
)


@pytest.fixture
def authority_schema() -> Iterator[str]:
    settings = PostgresContractSettings.from_environment()
    schema_name = f"job_finder_contract_{uuid4().hex}"
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
        try:
            yield schema_name
        finally:
            connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema_name))
            )


def _store_default_qualification_target(
    connection: psycopg.Connection[tuple[object, ...]],
    now: datetime,
    *,
    relevance_release_id: RelevanceReleaseId | None = None,
) -> tuple[
    ImplementationArtifact,
    tuple[InputPreparationContent, RelevanceContent, EnrichmentContent, DeduplicationContent],
    QualificationTargetContent,
]:
    artifact = build_implementation_artifact(Path(__file__).resolve().parents[1])
    _ = store_implementation_artifact(connection, artifact, created_at=now, created_by="build")
    definition_row = connection.execute(
        "SELECT revision_id FROM qualification_definition_publications LIMIT 1"
    ).fetchone()
    relevance_row = connection.execute("SELECT id FROM relevance_releases LIMIT 1").fetchone()
    assert definition_row is not None and relevance_row is not None
    versions = connection.execute(
        "SELECT id, phase, output_schema FROM prompt_versions ORDER BY phase, id"
    ).fetchall()
    relevance_versions = tuple(
        PromptVersionId(str(row[0])) for row in versions if row[1] in ("filter", "profile")
    )
    enrichment_version = next(row for row in versions if row[1] == "enrichment")
    deduplication_version = next(row for row in versions if row[1] == "deduplication")
    output_schema = json.dumps(
        enrichment_version[2], sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    input_preparation = InputPreparationContent(
        artifact_id=artifact.id,
        ats_sources=tuple(SupportedSearchSource),
    )
    relevance = RelevanceContent(
        artifact_id=artifact.id,
        qualification_definition_revision_id=QualificationDefinitionRevisionId(
            str(definition_row[0])
        ),
        relevance_release_id=relevance_release_id or RelevanceReleaseId(str(relevance_row[0])),
        prompt_version_ids=relevance_versions,
    )
    enrichment = EnrichmentContent(
        artifact_id=artifact.id,
        prompt_version_id=PromptVersionId(str(enrichment_version[0])),
        output_schema_digest=hashlib.sha256(output_schema.encode()).hexdigest(),
    )
    deduplication = DeduplicationContent(
        artifact_id=artifact.id,
        prompt_version_id=PromptVersionId(str(deduplication_version[0])),
    )
    components = (input_preparation, relevance, enrichment, deduplication)
    for content in components:
        _ = store_component_release(connection, content, created_at=now, created_by="owner")
    target = build_qualification_target(*components)
    _ = store_qualification_target(connection, target, created_at=now, created_by="owner")
    return artifact, components, target


def _seed_evaluation_execution_context(
    connection: psycopg.Connection[tuple[object, ...]],
    now: datetime,
    *,
    ats_evidence: AtsAvailable | None = None,
    prepare_ats_description: bool = True,
) -> tuple[str, ReleaseTarget, ExchangeRateSnapshot]:
    apply_migrations(connection)
    prompt_release = bootstrap_prompt_release(connection)
    pipeline_run_id = uuid4()
    _insert_prompt_run(connection, pipeline_run_id, prompt_release.id, now)
    decision_id = _insert_review_decision(
        connection,
        pipeline_run_id,
        prompt_release.id,
        now,
        91,
        "qualified",
        ats_evidence=ats_evidence,
        prepare_ats_description=prepare_ats_description,
    )
    assert enqueue_qualified_review_item(connection, decision_id, now.date())
    review_item = load_review_queue(connection).items[0]
    feedback = record_review(
        connection,
        ReviewSubmission(
            review_item_id=review_item.id,
            evaluation_id=review_item.evaluation_id,
            snapshot_id=review_item.snapshot_id,
            decision="pursue",
            target_profile="applied-ai-product-engineer",
            primary_reason="technology-fit",
            actor="owner",
            created_at=now,
        ),
    )
    assert isinstance(feedback, ReviewSaved)
    include_review_event(
        connection,
        review_event_id=feedback.review_event_id,
        critical=False,
        reason="Execution lifecycle contract fixture.",
        actor="owner",
        created_at=now,
        idempotency_key=f"curation:execution:{pipeline_run_id}",
    )
    manifest = create_manifest(
        connection,
        policy=ManifestPolicy(),
        created_at=now,
        created_by="contract",
        idempotency_key=f"manifest:execution:{pipeline_run_id}",
    )
    relevance_release = store_relevance_release(
        connection,
        build_relevance_release(build_gemini_policy(prompt_release)),
        created_at=now,
        created_by="contract",
    )
    target = ReleaseTarget(
        prompt_release_id=prompt_release.id,
        relevance_release_id=relevance_release.id,
    )
    rates = ExchangeRateSnapshot(rates={"EUR": Decimal("1.10")}, source="fallback", observed_at=now)
    return manifest.id, target, rates


def _insert_accepted_model_call_attempt(
    connection: psycopg.Connection[tuple[object, ...]],
    release: PromptRelease,
    now: datetime,
) -> ModelCallAttempt:
    version = release.versions[0]
    model_run_id = uuid4()
    processing_attempt_id = uuid4()
    attempt_id = uuid4()
    request_id = token_hex(32)
    input_digest = token_hex(32)
    connection.execute(
        """
        INSERT INTO pipeline_runs (
          id, idempotency_key, kind, implementation_ref, prompt_release_id, parameters,
          status, started_at, completed_at
        ) VALUES (%s, %s, 'evaluation', 'rebuild-ref', %s, '{}'::jsonb,
          'completed', %s, %s)
        """,
        (model_run_id, f"model-call:{model_run_id}", release.id, now, now),
    )
    connection.execute(
        """
        INSERT INTO processing_attempts (
          id, pipeline_run_id, operation_key, attempt_number, input_digest,
          status, started_at, completed_at
        ) VALUES (%s, %s, 'rebuild_contract', 0, %s, 'completed', %s, %s)
        """,
        (processing_attempt_id, model_run_id, input_digest, now, now),
    )
    connection.execute(
        """
        INSERT INTO model_call_attempts (
          id, processing_attempt_id, pipeline_run_id, prompt_release_id, request_id,
          attempt_number, operation_key, prompt_name, prompt_version_id, input_digest,
          requested_model, provider, provider_response_id, status, parsed_output,
          raw_response, input_tokens, output_tokens, cost_usd, latency_ms, observed_at,
          request_messages, response_model, error
        ) VALUES (
          %s, %s, %s, %s, %s, 0, 'rebuild_contract', %s, %s, %s,
          'parity-model', 'typesafe', NULL, 'accepted', %s, %s, 7, 3, %s, 11, %s,
          %s, 'parity-model', NULL
        )
        """,
        (
            attempt_id,
            processing_attempt_id,
            model_run_id,
            release.id,
            request_id,
            version.definition.name,
            version.id,
            input_digest,
            Jsonb({"pass": True}),
            Jsonb({"choices": []}),
            Decimal("0.00000001"),
            now,
            Jsonb([{"role": "user", "content": "parity"}]),
        ),
    )
    return ModelCallAttempt(
        id=attempt_id,
        context=ModelCallContext(
            processing_attempt_id=processing_attempt_id,
            pipeline_run_id=model_run_id,
            prompt_release_id=release.id,
            operation_key="rebuild_contract",
            input_digest=InputDigest(input_digest),
        ),
        request_id=ModelRequestId(request_id),
        attempt_number=0,
        prompt_name=version.definition.name,
        prompt_version_id=version.id,
        requested_model="parity-model",
        response_model="parity-model",
        provider_response_id=None,
        status="accepted",
        parsed_output={"pass": True},
        raw_response={"choices": []},
        input_tokens=7,
        output_tokens=3,
        cost_usd=Decimal("0.00000001"),
        latency_ms=11,
        error=None,
        observed_at=now,
        request_messages=({"role": "user", "content": "parity"},),
    )


def _insert_review_decision(
    connection: psycopg.Connection[tuple[object, ...]],
    run_id: UUID,
    prompt_release_id: str,
    now: datetime,
    value: int,
    outcome: str,
    *,
    ats_evidence: AtsAvailable | None = None,
    prepare_ats_description: bool = True,
) -> str:
    job_id = UUID(int=value)
    snapshot_id = f"{value + 100:064x}"
    evaluation_id = f"{value + 1000:064x}"
    raw_url = (
        f"https://jobs.lever.co/acme/review-{value}"
        if ats_evidence is not None
        else f"https://example.com/jobs/review-{value}"
    )
    source = "lever" if ats_evidence is not None else "other"
    description = (
        format_ats_description(ats_evidence, ats_evidence.description or "Build useful tools.")
        if ats_evidence is not None and prepare_ats_description
        else "Build useful tools."
    )
    location = ats_evidence.location if ats_evidence is not None else "Remote"
    connection.execute(
        """
        INSERT INTO jobs (id, raw_url, first_discovered_at, last_discovered_at)
        VALUES (%s, %s, %s, %s)
        """,
        (job_id, raw_url, now, now),
    )
    connection.execute(
        """
        INSERT INTO job_snapshots (
          id, job_id, content_digest, title, company, normalized_company,
          normalized_title, source, raw_url, description, location, keywords,
          date_posted, observed_at, ats_evidence
        ) VALUES (%s, %s, %s, %s, 'Acme', 'acme', %s, %s, %s,
          %s, %s, '["python"]'::jsonb, %s, %s, %s)
        """,
        (
            snapshot_id,
            job_id,
            f"{value + 200:064x}",
            f"Engineer {value}",
            f"engineer {value}",
            source,
            raw_url,
            description,
            location,
            now.date(),
            now,
            Jsonb(ats_evidence.model_dump(mode="json")) if ats_evidence is not None else None,
        ),
    )
    connection.execute(
        """
        INSERT INTO evaluation_decisions (
          id, snapshot_id, pipeline_run_id, prompt_release_id, policy_version,
          outcome, matched_profile, reason, created_at
        ) VALUES (%s, %s, %s, %s, 'policy-1', %s, %s, 'Evaluation reason', %s)
        """,
        (
            evaluation_id,
            snapshot_id,
            run_id,
            prompt_release_id,
            outcome,
            "applied-ai-product-engineer" if outcome == "qualified" else None,
            now,
        ),
    )
    return evaluation_id


def _insert_prompt_run(
    connection: psycopg.Connection[tuple[object, ...]],
    run_id: UUID,
    prompt_release_id: str,
    now: datetime,
) -> None:
    connection.execute(
        """
        INSERT INTO pipeline_runs (
          id, idempotency_key, kind, implementation_ref, prompt_release_id, parameters,
          status, started_at, completed_at
        ) VALUES (%s, %s, 'processing', 'test-ref', %s, '{}'::jsonb,
          'completed', %s, %s)
        """,
        (run_id, f"decision:{run_id}", prompt_release_id, now, now),
    )


def _decision_listing() -> JobListing:
    return JobListing(
        title="Sr Eng - Acme",
        company="acme.io",
        url="https://example.com/jobs/decision",
        source="other",
        keywords_matched=("python",),
        date_posted=date(2026, 9, 9),
        date_scraped=date(2026, 9, 10),
        description="Raw description",
        location="",
    )


def _decision_enrichment() -> EnrichedJob:
    return EnrichedJob(
        title="Senior Engineer",
        company="Acme",
        description="## Overview\nBuild things.",
        location="Remote",
    )


def _publish_configuration_revision(
    connection: psycopg.Connection[tuple[object, ...]],
    revision: SearchConfigurationRevision,
    published_at: datetime,
) -> None:
    release = store_prompt_release(
        connection,
        build_prompt_release(revision.configuration),
        created_at=published_at,
        created_by="test",
    )
    connection.execute(
        """
        INSERT INTO search_configuration_publications (
          revision_id, prompt_release_id, published_at, published_by
        ) VALUES (%s, %s, %s, 'test')
        """,
        (revision.id, release.id, published_at),
    )


def _seed_initial_configuration_publication(
    connection: psycopg.Connection[tuple[object, ...]],
    published_at: datetime,
) -> PromptReleaseId:
    revision = build_search_configuration_revision(
        DEFAULT_SEARCH_CONFIGURATION,
        created_at=published_at,
        created_by="test",
    )
    assert revision.id == INITIAL_SEARCH_CONFIGURATION_REVISION_ID
    _ = store_search_configuration_revision(connection, revision)
    release = store_prompt_release(
        connection,
        build_prompt_release(revision.configuration),
        created_at=published_at,
        created_by="test",
    )
    connection.execute(
        """
        INSERT INTO search_configuration_publications (
          revision_id, prompt_release_id, published_at, published_by
        ) VALUES (%s, %s, %s, 'test')
        """,
        (revision.id, release.id, published_at),
    )
    return release.id


def _insert_legacy_orchestration_run(
    connection: psycopg.Connection[tuple[object, ...]],
    run_id: UUID,
    prompt_release_id: PromptReleaseId,
    started_at: datetime,
) -> None:
    with connection.transaction():
        connection.execute(
            """
            INSERT INTO pipeline_runs (
              id, idempotency_key, kind, implementation_ref, prompt_release_id,
              parameters, status, started_at
            ) VALUES (%s, %s, 'orchestration', 'test-ref', %s, '{}'::jsonb, 'running', %s)
            """,
            (run_id, f"orchestration:{run_id}", prompt_release_id, started_at),
        )
        connection.execute(
            """
            INSERT INTO run_exchange_rate_snapshots (
              pipeline_run_id, content_digest, rates, source, observed_at
            ) VALUES (%s, %s, '{"EUR": "1.1"}'::jsonb, 'frankfurter', %s)
            """,
            (run_id, "0" * 64, started_at),
        )


def _publication_command(
    draft: SearchConfigurationDraft,
    idempotency_key: str,
    timestamp: datetime,
) -> PublishConfigurationCommand:
    return PublishConfigurationCommand(
        idempotency_key=idempotency_key,
        expected_draft_version=draft.version,
        expected_configuration_revision_id=search_configuration_revision_id(draft.configuration),
        actor="owner",
        timestamp=timestamp,
    )


@contextmanager
def _connection(
    schema_name: str,
) -> Generator[psycopg.Connection[tuple[object, ...]], None, None]:
    settings = PostgresContractSettings.from_environment()
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema_name)))
        yield connection


def _apply_migrations_through(
    connection: psycopg.Connection[tuple[object, ...]],
    final_name: str,
) -> None:
    _ = connection.execute(
        """
        CREATE TABLE job_finder_schema_migrations (
          name TEXT PRIMARY KEY,
          sha256 CHAR(64) NOT NULL,
          applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    for path in sorted(MIGRATIONS_PATH.glob("*.sql")):
        if path.name > final_name:
            break
        content = path.read_bytes()
        _ = connection.execute(sql.SQL(cast(LiteralString, content.decode())), prepare=False)
        _ = connection.execute(
            "INSERT INTO job_finder_schema_migrations (name, sha256) VALUES (%s, %s)",
            (path.name, hashlib.sha256(content).hexdigest()),
        )


def _public_tables(connection: psycopg.Connection[tuple[object, ...]]) -> tuple[str, ...]:
    return tuple(
        str(row[0])
        for row in connection.execute(
            """
            SELECT tablename
            FROM pg_tables
            WHERE schemaname = current_schema()
              AND tablename <> 'job_finder_schema_migrations'
            ORDER BY tablename
            """
        ).fetchall()
    )


def _table_contents(
    connection: psycopg.Connection[tuple[object, ...]],
    tables: tuple[str, ...],
) -> dict[str, object]:
    contents: dict[str, object] = {}
    for table in tables:
        row = connection.execute(
            sql.SQL(
                """
                SELECT COALESCE(jsonb_agg(row_data ORDER BY row_data::text), '[]'::jsonb)
                FROM (SELECT to_jsonb(stored_row) AS row_data FROM {} AS stored_row) AS rows
                """
            ).format(sql.Identifier(table))
        ).fetchone()
        assert row is not None
        contents[table] = row[0]
    return contents


def _insert_run_and_job(
    connection: psycopg.Connection[tuple[object, ...]],
    run_id: UUID,
    job_id: UUID,
    now: datetime,
) -> None:
    _insert_run(connection, run_id, now)
    connection.execute(
        """
        INSERT INTO jobs (id, raw_url, first_discovered_at, last_discovered_at)
        VALUES (%s, 'https://example.com/job', %s, %s)
        """,
        (job_id, now, now),
    )


def _insert_run(
    connection: psycopg.Connection[tuple[object, ...]],
    run_id: UUID,
    now: datetime,
) -> None:
    connection.execute(
        """
        INSERT INTO pipeline_runs (
          id, idempotency_key, kind, implementation_ref, parameters, status,
          started_at, completed_at
        ) VALUES (%s, %s, 'processing', 'test-ref', '{}'::jsonb, 'completed', %s, %s)
        """,
        (run_id, f"run:{run_id}", now, now),
    )
