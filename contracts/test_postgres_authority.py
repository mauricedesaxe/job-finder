from __future__ import annotations

import asyncio
import base64
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
from typing import LiteralString, cast
from uuid import UUID, uuid4

import psycopg
import pytest
from fastmcp import Client
from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import JsonValue, SecretStr, TypeAdapter

import job_finder.configuration_service as configuration_service_module
import job_finder.evaluation.manifest_execution as manifest_execution_module
import job_finder.evaluation.relevance_releases as relevance_releases_module
from job_finder.acquisition_policy import AcquisitionPolicy, acquisition_policy_revision_id
from job_finder.ats.models import AtsAvailable, AtsNotApplicable, CompensationObservation
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
from job_finder.benchmarks.promotions import record_prompt_promotion_decision
from job_finder.benchmarks.qualification_evidence import (
    FixtureCase,
    PhaseFixtureSet,
    ProviderExperimentSettings,
    QualificationEvidence,
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
from job_finder.benchmarks.provider_attempts import (
    provider_attempt_evidence,
    store_provider_attempts,
)
from job_finder.config import PostgresContractSettings
from job_finder.configuration_service import (
    ActivationTargetUnpublished,
    ActiveConfigurationChanged,
    ActivateConfigurationCommand,
    ConfigurationActivated,
    ConfigurationPreview,
    ConfigurationPublished,
    ConfigurationRevisionCursor,
    ConfigurationRevisionDetails,
    ConfigurationRevisionNotFound,
    ConfigurationRevisionPage,
    ConfigurationValid,
    DraftChanged,
    DraftSaved,
    PublicationIdempotencyKeyConflict,
    PublishConfigurationCommand,
    PublishDraftChanged,
    PublishedActiveSearchConfiguration,
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
    build_qualification_target,
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
from job_finder.jobs.decision_pipeline import (
    DecisionContext,
    PersistedDecision,
    postgres_decision_store,
    process_qualified_job,
)
from job_finder.jobs.enrichment import EnrichedJob
from job_finder.jobs.models import JobListing
from job_finder.jobs.title_deduplication import TitleDuplicate
from job_finder.review.configuration_editor import postgres_configuration_editor_service
from job_finder.review.feedback import (
    ReviewSaved,
    ReviewSubmission,
    list_review_feedback,
    load_review_feedback,
    record_review,
)
from job_finder.review.onboarding import postgres_onboarding_progress_service
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
    build_jev_faithful_policy,
    build_relevance_release,
    load_relevance_release,
    store_relevance_release,
    validate_release_target,
)
from job_finder.discovery.exchange_rates import ExchangeRateSnapshot
from job_finder.discovery.jina import SearchSucceeded
from job_finder.mcp_server import McpDependencies, create_mcp_server
from job_finder.pipeline.orchestration import PipelineBoundaries, discover_jobs
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
)


def test_migrations_are_repeatable(authority_schema: str) -> None:
    with _connection(authority_schema) as connection:
        first = apply_migrations(connection)
        second = apply_migrations(connection)

        assert first == EXPECTED_MIGRATIONS
        assert list(first) == sorted(first)
        assert second == first
        assert connection.execute(
            "SELECT count(*) FROM job_finder_schema_migrations"
        ).fetchone() == (len(EXPECTED_MIGRATIONS),)
        assert connection.execute(
            "SELECT stage, password_hash FROM owner_onboarding WHERE singleton_id = 1"
        ).fetchone() == ("owner_account", None)


def test_policy_projection_migration_preserves_legacy_rows_and_repeats(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 25, tzinfo=UTC)
    original = DEFAULT_SEARCH_CONFIGURATION
    configurations = (
        original,
        original.model_copy(update={"search_keywords": (*original.search_keywords, "a new role")}),
        original.model_copy(
            update={
                "personal_criteria": (
                    original.personal_criteria[0].model_copy(update={"name": "Renamed criterion"}),
                    *original.personal_criteria[1:],
                )
            }
        ),
    )
    with _connection(authority_schema) as connection:
        _apply_migrations_through(connection, "0037_run_budget_authority_guard.sql")
        revisions = tuple(
            store_search_configuration_revision(
                connection,
                build_search_configuration_revision(
                    configuration, created_at=now, created_by="migration-test"
                ),
            )
            for configuration in configurations
        )
        legacy_tables = _public_tables(connection)
        legacy_rows = _table_contents(connection, legacy_tables)

        assert apply_migrations(connection) == EXPECTED_MIGRATIONS
        assert _table_contents(connection, legacy_tables) == legacy_rows
        assert connection.execute(
            "SELECT count(*) FROM legacy_search_configuration_policy_projections"
        ).fetchone() == (3,)
        assert connection.execute(
            "SELECT count(*) FROM acquisition_policy_revisions"
        ).fetchone() == (2,)
        assert connection.execute(
            "SELECT count(*) FROM qualification_definition_revisions"
        ).fetchone() == (2,)

        for revision in revisions:
            projection = project_legacy_search_configuration(revision.configuration)
            row = connection.execute(
                """
                SELECT bridge.acquisition_policy_revision_id, acquisition.content,
                       bridge.qualification_definition_revision_id, qualification.content
                FROM legacy_search_configuration_policy_projections bridge
                JOIN acquisition_policy_revisions acquisition
                  ON acquisition.id = bridge.acquisition_policy_revision_id
                JOIN qualification_definition_revisions qualification
                  ON qualification.id = bridge.qualification_definition_revision_id
                WHERE bridge.legacy_revision_id = %s
                """,
                (revision.id,),
            ).fetchone()
            assert row is not None
            acquisition = AcquisitionPolicy.model_validate(row[1])
            qualification = QualificationDefinition.model_validate(row[3])
            assert row[0] == acquisition_policy_revision_id(projection.acquisition)
            assert row[2] == qualification_definition_revision_id(projection.qualification)
            assert acquisition == projection.acquisition
            assert qualification == projection.qualification

        projection_tables = (
            "acquisition_policy_revisions",
            "qualification_definition_revisions",
            "legacy_search_configuration_policy_projections",
        )
        projected_rows = _table_contents(connection, projection_tables)
        _ = connection.execute("SELECT backfill_legacy_search_configuration_policies()")
        assert _table_contents(connection, projection_tables) == projected_rows
        assert _table_contents(connection, legacy_tables) == legacy_rows

        later_revision = store_search_configuration_revision(
            connection,
            build_search_configuration_revision(
                original.model_copy(
                    update={
                        "enabled_sources": tuple(reversed(original.enabled_sources)),
                        "target_profiles": (
                            original.target_profiles[0].model_copy(
                                update={"name": "Later profile name"}
                            ),
                            *original.target_profiles[1:],
                        ),
                    }
                ),
                created_at=now + timedelta(days=1),
                created_by="migration-test",
            ),
        )
        _ = connection.execute("SELECT backfill_legacy_search_configuration_policies()")
        assert connection.execute(
            """
            SELECT count(*) FROM legacy_search_configuration_policy_projections
            WHERE legacy_revision_id = %s
            """,
            (later_revision.id,),
        ).fetchone() == (1,)
        caught_up_rows = _table_contents(connection, projection_tables)
        _ = connection.execute("SELECT backfill_legacy_search_configuration_policies()")
        assert _table_contents(connection, projection_tables) == caught_up_rows


def test_split_policy_state_seeds_without_changing_legacy_authority(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 25, tzinfo=UTC)
    first = DEFAULT_SEARCH_CONFIGURATION
    second = first.model_copy(update={"search_keywords": (*first.search_keywords, "another role")})
    with _connection(authority_schema) as connection:
        _apply_migrations_through(connection, "0038_policy_revisions.sql")
        first_revision = store_search_configuration_revision(
            connection,
            build_search_configuration_revision(first, created_at=now, created_by="owner"),
        )
        second_revision = store_search_configuration_revision(
            connection,
            build_search_configuration_revision(
                second, created_at=now + timedelta(seconds=1), created_by="owner"
            ),
        )
        prompt = build_prompt_release(first)
        _ = store_prompt_release(connection, prompt, created_at=now, created_by="owner")
        alternate_prompt = build_prompt_release(
            first.model_copy(
                update={
                    "target_profiles": (
                        first.target_profiles[0].model_copy(
                            update={"instructions": "Revised profile instructions."}
                        ),
                        *first.target_profiles[1:],
                    )
                }
            )
        )
        _ = store_prompt_release(connection, alternate_prompt, created_at=now, created_by="owner")
        assert alternate_prompt.id != prompt.id
        for revision, release in ((first_revision, prompt), (second_revision, alternate_prompt)):
            _ = connection.execute(
                """
                INSERT INTO search_configuration_publications (
                  revision_id, prompt_release_id, published_at, published_by
                ) VALUES (%s, %s, %s, 'owner')
                """,
                (revision.id, release.id, now),
            )
        _ = connection.execute(
            """
            INSERT INTO search_configuration_drafts (
              singleton_id, base_revision_id, version, content, updated_at, updated_by
            ) VALUES (1, %s, 7, %s, %s, 'owner')
            """,
            (second_revision.id, Jsonb(second.model_dump(mode="json")), now),
        )
        _ = connection.execute(
            """
            INSERT INTO active_search_configuration (
              singleton_id, revision_id, generation, activated_at, activated_by
            ) VALUES (1, %s, 4, %s, 'owner')
            """,
            (second_revision.id, now),
        )
        legacy_tables = tuple(
            table
            for table in _public_tables(connection)
            if table
            not in {
                "acquisition_policy_revisions",
                "qualification_definition_revisions",
                "legacy_search_configuration_policy_projections",
            }
        )
        legacy_rows = _table_contents(connection, legacy_tables)

        assert apply_migrations(connection) == EXPECTED_MIGRATIONS
        assert _table_contents(connection, legacy_tables) == legacy_rows
        projection = project_legacy_search_configuration(second)
        acquisition_id = acquisition_policy_revision_id(projection.acquisition)
        qualification_id = qualification_definition_revision_id(projection.qualification)
        assert connection.execute(
            "SELECT revision_id, generation FROM active_acquisition_policy"
        ).fetchone() == (acquisition_id, 0)
        assert connection.execute(
            "SELECT base_revision_id, version, content FROM acquisition_policy_drafts"
        ).fetchone() == (acquisition_id, 0, projection.acquisition.model_dump(mode="json"))
        assert connection.execute(
            "SELECT base_revision_id, version, content FROM qualification_definition_drafts"
        ).fetchone() == (qualification_id, 0, projection.qualification.model_dump(mode="json"))
        assert connection.execute(
            "SELECT count(*) FROM legacy_search_configuration_publication_projections"
        ).fetchone() == (2,)
        assert connection.execute(
            "SELECT count(*) FROM acquisition_policy_publications"
        ).fetchone() == (2,)
        assert connection.execute(
            "SELECT count(*) FROM qualification_definition_publications"
        ).fetchone() == (1,)

        split_tables = (
            "acquisition_policy_publications",
            "qualification_definition_publications",
            "legacy_search_configuration_publication_projections",
            "acquisition_policy_drafts",
            "qualification_definition_drafts",
            "active_acquisition_policy",
        )
        seeded_rows = _table_contents(connection, split_tables)
        _ = connection.execute("SELECT backfill_legacy_search_configuration_publications()")
        assert _table_contents(connection, split_tables) == seeded_rows
        assert _table_contents(connection, legacy_tables) == legacy_rows

        later = second.model_copy(
            update={"search_keywords": (*second.search_keywords, "later role")}
        )
        later_revision = store_search_configuration_revision(
            connection,
            build_search_configuration_revision(
                later, created_at=now + timedelta(days=1), created_by="owner"
            ),
        )
        _ = connection.execute(
            """
            INSERT INTO search_configuration_publications (
              revision_id, prompt_release_id, published_at, published_by
            ) VALUES (%s, %s, %s, 'owner')
            """,
            (later_revision.id, prompt.id, now + timedelta(days=1)),
        )
        _ = connection.execute("SELECT backfill_legacy_search_configuration_publications()")
        assert connection.execute(
            """
            SELECT count(*) FROM legacy_search_configuration_publication_projections
            WHERE legacy_revision_id = %s
            """,
            (later_revision.id,),
        ).fetchone() == (1,)
        assert _table_contents(connection, split_tables[3:]) == {
            table: seeded_rows[table] for table in split_tables[3:]
        }

        with pytest.raises(psycopg.errors.CheckViolation):
            _ = connection.execute("UPDATE acquisition_policy_drafts SET version = version + 2")
        with pytest.raises(psycopg.errors.CheckViolation):
            _ = connection.execute(
                "UPDATE active_acquisition_policy SET generation = generation + 1"
            )


def _store_default_qualification_target(
    connection: psycopg.Connection[tuple[object, ...]], now: datetime
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
        relevance_release_id=RelevanceReleaseId(str(relevance_row[0])),
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


def test_qualification_target_requires_four_components_from_one_artifact(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 25, tzinfo=UTC)
    with _connection(authority_schema) as connection:
        _ = apply_migrations(connection)
        artifact, components, target = _store_default_qualification_target(connection, now)
        input_preparation, relevance, enrichment, deduplication = components
        target_id = qualification_target_id(target)
        resolved = load_qualification_target(connection, target_id)
        assert resolved.content == target
        assert resolved.artifact == artifact
        assert (
            resolved.input_preparation,
            resolved.relevance,
            resolved.enrichment,
            resolved.deduplication,
        ) == components
        assert connection.execute(
            "SELECT count(*) FROM qualification_targets WHERE id = %s", (target_id,)
        ).fetchone() == (1,)
        assert (
            store_qualification_target(connection, target, created_at=now, created_by="owner")
            == target_id
        )

        wrong_kind = target.model_copy(
            update={"enrichment_release_id": target.deduplication_release_id}
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            _ = store_qualification_target(
                connection, wrong_kind, created_at=now, created_by="owner"
            )

        alternate_manifest = artifact.manifest.model_copy(
            update={"runtime": artifact.manifest.runtime + " alternate"}
        )
        alternate_artifact = ImplementationArtifact(
            id=implementation_artifact_id(alternate_manifest), manifest=alternate_manifest
        )
        _ = store_implementation_artifact(
            connection, alternate_artifact, created_at=now, created_by="build"
        )
        alternate_input = input_preparation.model_copy(
            update={"artifact_id": alternate_artifact.id}
        )
        alternate_input_id = store_component_release(
            connection, alternate_input, created_at=now, created_by="owner"
        )
        with pytest.raises(ValueError, match="one implementation artifact"):
            _ = build_qualification_target(alternate_input, relevance, enrichment, deduplication)
        mixed_artifacts = target.model_copy(
            update={"input_preparation_release_id": alternate_input_id}
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            _ = store_qualification_target(
                connection, mixed_artifacts, created_at=now, created_by="owner"
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            _ = connection.execute(
                "UPDATE qualification_component_releases SET created_by = 'changed'"
            )


def test_qualification_prompt_compilation_requires_exact_published_release(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 25, tzinfo=UTC)
    artifact_path = Path(__file__).resolve().parents[1] / "implementation-artifact.json"
    with _connection(authority_schema) as connection:
        _ = apply_migrations(connection)
        artifact, _, target = _store_default_qualification_target(connection, now)
        target_id = qualification_target_id(target)
        try:
            assert write_implementation_artifact(artifact_path.parent, artifact_path) == artifact
            release_id = bind_qualification_prompt_release(
                connection, target_id, artifact_path, created_at=now, created_by="owner"
            )
            compiled = load_compiled_qualification_target(connection, target_id, artifact_path)
            assert compiled.target.id == target_id
            assert compiled.prompt_release.id == release_id
            assert (
                bind_qualification_prompt_release(
                    connection, target_id, artifact_path, created_at=now, created_by="owner"
                )
                == release_id
            )
            partial_versions = compiled.prompt_release.versions[:-1]
            partial_digest = hashlib.sha256(
                json.dumps(
                    [[version.definition.name, version.id] for version in partial_versions],
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode()
            ).hexdigest()
            partial_release = PromptRelease(
                id=PromptReleaseId(partial_digest),
                name=f"partial-{partial_digest}",
                content_digest=partial_digest,
                versions=partial_versions,
            )
            _ = store_prompt_release(
                connection, partial_release, created_at=now, created_by="owner"
            )
            with pytest.raises(psycopg.errors.CheckViolation, match="exact ordered"):
                _ = connection.execute(
                    """
                    INSERT INTO qualification_prompt_compilations (
                      target_id, artifact_id, qualification_definition_revision_id,
                      prompt_release_id, created_at, created_by
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        target_id,
                        artifact.id,
                        target.qualification_definition_revision_id,
                        partial_release.id,
                        now,
                        "owner",
                    ),
                )
            with pytest.raises(psycopg.errors.CheckViolation):
                _ = connection.execute(
                    """
                    INSERT INTO qualification_prompt_compilations (
                      target_id, artifact_id, qualification_definition_revision_id,
                      prompt_release_id, created_at, created_by
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        target_id,
                        "0" * 64,
                        target.qualification_definition_revision_id,
                        release_id,
                        now,
                        "owner",
                    ),
                )
        finally:
            artifact_path.unlink(missing_ok=True)


def test_input_preparation_fixtures_execute_shared_production_path(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 25, tzinfo=UTC)
    artifact_path = Path(__file__).resolve().parents[1] / "implementation-artifact.json"
    markdown = "Title: Backend engineer\nBuild services."
    url = "https://jobs.lever.co/acme/role-1"
    direct_evidence = AtsNotApplicable().model_dump(mode="json")
    ats_evidence = AtsAvailable(
        source="lever",
        location="Berlin",
        locations=("Berlin",),
        workplace_type="OnSite",
        country="DE",
        description="A detailed ATS description",
    ).model_dump(mode="json")
    listing: dict[str, JsonValue] = {
        "title": "Backend engineer",
        "company": "acme",
        "url": url,
        "source": "lever",
        "keywords_matched": ["backend"],
        "date_posted": None,
        "date_scraped": "2026-09-25",
        "description": markdown,
        "location": "",
        "profile": "",
    }
    ats_description = (
        "## ATS Structured Data (from lever API)\n"
        "- Primary location: Berlin\n"
        "- All listed locations: Berlin\n"
        "- Workplace type: OnSite\n"
        "- Country fallback when locations are non-geographic: DE\n"
        "---\n\nA detailed ATS description"
    )
    fixture = PhaseFixtureSet(
        phase="input_preparation",
        cases=(
            FixtureCase(
                input={
                    "markdown": markdown,
                    "url": url,
                    "keyword": "backend",
                    "scraped_on": "2026-09-25",
                    "ats_evidence": direct_evidence,
                },
                expected={
                    "listing": listing,
                    "ats_evidence": direct_evidence,
                    "body": markdown,
                    "structural_decision": {"kind": "pass"},
                },
                input_path="direct",
            ),
            FixtureCase(
                input={
                    "markdown": markdown,
                    "url": url,
                    "keyword": "backend",
                    "scraped_on": "2026-09-25",
                    "ats_evidence": ats_evidence,
                },
                expected={
                    "listing": {**listing, "description": ats_description, "location": "Berlin"},
                    "ats_evidence": ats_evidence,
                    "body": "A detailed ATS description",
                    "structural_decision": {
                        "kind": "rejected",
                        "reason": "ATS workplaceType=OnSite (Berlin)",
                    },
                },
                input_path="ats",
            ),
        ),
    )
    with _connection(authority_schema) as connection:
        _ = apply_migrations(connection)
        artifact, _, target = _store_default_qualification_target(connection, now)
        target_id = qualification_target_id(target)
        fixture_id = store_fixture_set(connection, fixture, created_at=now, created_by="owner")
        try:
            assert write_implementation_artifact(artifact_path.parent, artifact_path) == artifact
            _ = bind_qualification_prompt_release(
                connection, target_id, artifact_path, created_at=now, created_by="owner"
            )
            evidence_id = execute_input_preparation_fixture_set(
                connection,
                target_id,
                fixture_id,
                artifact_path,
                completed_at=now,
                created_by="owner",
            )
            row = connection.execute(
                "SELECT content FROM qualification_phase_evidence WHERE id = %s", (evidence_id,)
            ).fetchone()
            assert row is not None
            evidence = QualificationEvidence.model_validate(row[0])
            assert evidence.origin == "canonical"
            assert evidence.outcome == "passed"
            assert evidence.result["case_count"] == 2
            assert evidence.result["passed_count"] == 2
        finally:
            artifact_path.unlink(missing_ok=True)


def test_qualification_provider_attempts_retain_raw_model_provenance(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 25, tzinfo=UTC)
    artifact_path = Path(__file__).resolve().parents[1] / "implementation-artifact.json"
    with _connection(authority_schema) as connection:
        _ = apply_migrations(connection)
        artifact, _, target = _store_default_qualification_target(connection, now)
        target_id = qualification_target_id(target)
        fixture_id = store_fixture_set(
            connection,
            PhaseFixtureSet(
                phase="enrichment",
                cases=(FixtureCase(input={}, expected={}, input_path="direct"),),
            ),
            created_at=now,
            created_by="owner",
        )
        try:
            assert write_implementation_artifact(artifact_path.parent, artifact_path) == artifact
            release_id = bind_qualification_prompt_release(
                connection, target_id, artifact_path, created_at=now, created_by="owner"
            )
            compiled = load_compiled_qualification_target(connection, target_id, artifact_path)
            prompt = next(
                version
                for version in compiled.prompt_release.versions
                if version.definition.phase == "enrichment"
            )
            attempt = ModelCallAttempt(
                id=uuid4(),
                context=ModelCallContext(
                    processing_attempt_id=uuid4(),
                    pipeline_run_id=uuid4(),
                    prompt_release_id=release_id,
                    operation_key="benchmark:enrichment",
                    input_digest=InputDigest("a" * 64),
                ),
                request_id=ModelRequestId("b" * 64),
                attempt_number=0,
                prompt_name=prompt.definition.name,
                prompt_version_id=prompt.id,
                requested_model="fixture-model",
                response_model="observed-model",
                provider_response_id="response-1",
                status="accepted",
                parsed_output={"company": "Acme"},
                raw_response={"choices": [{"message": {"content": "Acme"}}]},
                input_tokens=12,
                output_tokens=4,
                cost_usd=Decimal("0.000001"),
                latency_ms=27,
                error=None,
                observed_at=now,
                request_messages=({"role": "user", "content": "source listing"},),
            )
            evidence = QualificationEvidence(
                target_id=target_id,
                phase="enrichment",
                component_release_id=target.enrichment_release_id,
                fixture_set_id=fixture_id,
                executor_artifact_id=artifact.id,
                origin="canonical",
                outcome="passed",
                result={"case_count": 1},
                attempts=(provider_attempt_evidence(attempt),),
                completed_at=now,
            )
            evidence_id = store_qualification_evidence(
                connection, evidence, created_at=now, created_by="owner"
            )
            store_provider_attempts(
                connection,
                evidence,
                (attempt,),
                provider="openrouter",
                created_at=now,
                created_by="owner",
            )
            row = connection.execute(
                "SELECT content FROM qualification_provider_attempts WHERE evidence_id = %s",
                (evidence_id,),
            ).fetchone()
            assert row is not None
            content = TypeAdapter(dict[str, JsonValue]).validate_python(row[0])
            assert content["request_messages"] == [{"role": "user", "content": "source listing"}]
            assert content["raw_response"] == attempt.raw_response
            assert content["input_tokens"] == 12
            synthetic_evidence = evidence.model_copy(update={"origin": "synthetic"})
            _ = store_qualification_evidence(
                connection, synthetic_evidence, created_at=now, created_by="owner"
            )
            synthetic_attempt = replace(attempt, id=uuid4(), request_id=ModelRequestId("c" * 64))
            store_provider_attempts(
                connection,
                synthetic_evidence,
                (synthetic_attempt,),
                provider="openrouter",
                created_at=now,
                created_by="owner",
            )
            with pytest.raises(ValueError, match="summaries differ"):
                store_provider_attempts(
                    connection,
                    evidence.model_copy(update={"attempts": ()}),
                    (attempt,),
                    provider="openrouter",
                    created_at=now,
                    created_by="owner",
                )
        finally:
            artifact_path.unlink(missing_ok=True)


def test_qualification_evidence_pins_target_components_and_experiment_inputs(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 25, tzinfo=UTC)
    with _connection(authority_schema) as connection:
        manifest_id, _, rates = _seed_evaluation_execution_context(connection, now)
        artifact, _, target = _store_default_qualification_target(connection, now)
        experiment = RelevanceExperimentInput(
            manifest_id=manifest_id,
            exchange_rates=rates,
            provider_settings=ProviderExperimentSettings(
                provider="openrouter", temperature=0, seed=1, retry_limit=2
            ),
            input_path="direct",
        )
        experiment_id = store_relevance_experiment_input(
            connection, experiment, created_at=now, created_by="owner"
        )
        baseline = QualificationEvidence(
            target_id=qualification_target_id(target),
            phase="relevance",
            component_release_id=target.relevance_release_id,
            experiment_input_id=experiment_id,
            executor_artifact_id=artifact.id,
            origin="synthetic",
            outcome="passed",
            result={"qualified": 1},
            completed_at=now,
        )
        candidate = baseline.model_copy(update={"result": {"qualified": 2}})
        _ = store_qualification_evidence(connection, baseline, created_at=now, created_by="owner")
        _ = store_qualification_evidence(connection, candidate, created_at=now, created_by="owner")
        comparison_id = record_relevance_comparison(
            connection, baseline, candidate, created_at=now, created_by="owner"
        )
        assert connection.execute(
            "SELECT experiment_input_id FROM qualification_relevance_comparisons WHERE id = %s",
            (comparison_id,),
        ).fetchone() == (experiment_id,)

        changed_experiment = experiment.model_copy(update={"input_path": "ats"})
        changed_id = store_relevance_experiment_input(
            connection, changed_experiment, created_at=now, created_by="owner"
        )
        changed_candidate = candidate.model_copy(update={"experiment_input_id": changed_id})
        changed_evidence_id = store_qualification_evidence(
            connection, changed_candidate, created_at=now, created_by="owner"
        )
        with pytest.raises(ValueError, match="same frozen experiment input"):
            _ = record_relevance_comparison(
                connection, baseline, changed_candidate, created_at=now, created_by="owner"
            )
        forged_comparison = {
            "experiment_input_id": experiment_id,
            "baseline_evidence_id": qualification_evidence_id(baseline),
            "candidate_evidence_id": changed_evidence_id,
        }
        forged_id = hashlib.sha256(
            json.dumps(forged_comparison, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        with pytest.raises(psycopg.errors.CheckViolation):
            _ = connection.execute(
                """
                INSERT INTO qualification_relevance_comparisons (
                  id, experiment_input_id, baseline_evidence_id,
                  candidate_evidence_id, created_at, created_by
                ) VALUES (%s, %s, %s, %s, %s, 'owner')
                """,
                (
                    forged_id,
                    experiment_id,
                    forged_comparison["baseline_evidence_id"],
                    changed_evidence_id,
                    now,
                ),
            )

        fixtures = PhaseFixtureSet(
            phase="enrichment",
            cases=(
                FixtureCase(
                    input={"title": "Engineer"},
                    expected={"title": "Engineer"},
                    input_path="direct",
                ),
            ),
        )
        fixture_id = store_fixture_set(connection, fixtures, created_at=now, created_by="owner")
        enrichment_evidence = QualificationEvidence(
            target_id=qualification_target_id(target),
            phase="enrichment",
            component_release_id=target.enrichment_release_id,
            fixture_set_id=fixture_id,
            executor_artifact_id=artifact.id,
            origin="synthetic",
            outcome="passed",
            result={"matched": 1},
            completed_at=now,
        )
        _ = store_qualification_evidence(
            connection, enrichment_evidence, created_at=now, created_by="owner"
        )
        wrong_component = enrichment_evidence.model_copy(
            update={"component_release_id": target.deduplication_release_id}
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            _ = store_qualification_evidence(
                connection, wrong_component, created_at=now, created_by="owner"
            )


def test_owner_bootstrap_is_single_winner_and_authenticates_from_postgres(
    authority_schema: str,
) -> None:
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
    service = postgres_owner_access_service(lambda: _connection(authority_schema))
    barrier = Barrier(2)

    def bootstrap(password: str) -> object:
        _ = barrier.wait()
        return service.bootstrap(password)

    passwords = ("first secure owner password", "second secure owner password")
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(bootstrap, passwords))

    assert sum(isinstance(result, OwnerBootstrapped) for result in results) == 1
    assert service.load_state().stage is OnboardingStage.PROVIDERS
    assert sum(service.authenticate(password) for password in passwords) == 1
    with _connection(authority_schema) as connection:
        with pytest.raises(psycopg.errors.CheckViolation, match="cannot be replaced or removed"):
            connection.execute(
                """
                UPDATE owner_onboarding
                SET stage = 'owner_account', password_hash = NULL
                WHERE singleton_id = 1
                """
            )
        with pytest.raises(psycopg.errors.CheckViolation, match="cannot be deleted"):
            connection.execute("DELETE FROM owner_onboarding WHERE singleton_id = 1")


def test_existing_installation_requires_and_idempotently_imports_legacy_owner(
    authority_schema: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _connection(authority_schema) as connection:
        _apply_migrations_through(connection, "0025_release_target_promotion_decisions.sql")
        _ = _seed_initial_configuration_publication(connection, datetime(2026, 9, 22, tzinfo=UTC))
        _ = connection.execute(
            """
            INSERT INTO active_search_configuration (
              singleton_id, revision_id, generation, activated_at, activated_by
            ) VALUES (1, %s, 0, %s, 'test')
            """,
            (
                INITIAL_SEARCH_CONFIGURATION_REVISION_ID,
                datetime(2026, 9, 22, tzinfo=UTC),
            ),
        )

    settings = PostgresContractSettings.from_environment()
    monkeypatch.setenv(
        "JOB_FINDER_POSTGRES_DSN",
        f"{settings.postgres_dsn}?options=-csearch_path%3D{authority_schema}",
    )
    monkeypatch.setenv("JOB_FINDER_REVIEW_PASSWORD", "legacy secure owner password")
    monkeypatch.delenv("JOB_FINDER_BOOTSTRAP_TOKEN", raising=False)
    monkeypatch.setenv("JOB_FINDER_REVIEW_SESSION_SECRET", "s" * 32)
    monkeypatch.delenv("JOB_FINDER_DAGSTER_GRAPHQL_URL", raising=False)

    _ = create_review_server()

    with _connection(authority_schema) as connection:
        imported = import_legacy_owner_password(connection, "different owner password")
        replayed = import_legacy_owner_password(connection, "different owner password")
        stored = connection.execute(
            "SELECT password_hash FROM owner_onboarding WHERE singleton_id = 1"
        ).fetchone()

    service = postgres_owner_access_service(lambda: _connection(authority_schema))
    assert imported.stage is OnboardingStage.COMPLETE
    assert replayed == imported
    assert stored is not None
    assert "legacy secure owner password" not in str(stored[0])
    assert service.authenticate("legacy secure owner password") is True
    assert service.authenticate("different owner password") is False
    with _connection(authority_schema) as connection:
        admission = admit_scheduled_execution(
            connection,
            idempotency_key="legacy-without-owner-budget",
            requested_at=datetime(2026, 9, 22, tzinfo=UTC),
        )
        assert isinstance(admission, ExecutionAdmitted)
        assert admission.max_jobs == 100


def test_provider_credentials_are_encrypted_versioned_and_gate_onboarding(
    authority_schema: str,
) -> None:
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
    owner = postgres_owner_access_service(lambda: _connection(authority_schema))
    _ = owner.bootstrap("secure owner password")
    cipher = credential_cipher(SecretStr(base64.urlsafe_b64encode(b"k" * 32).decode()))
    service = postgres_provider_setup_service(
        lambda: _connection(authority_schema),
        cipher,
        {
            provider: lambda _secret, provider=provider: ProviderValidation(
                capabilities={
                    ProviderKind.JINA: (
                        ProviderCapability.SEARCH,
                        ProviderCapability.SCRAPE,
                    ),
                    ProviderKind.OPENROUTER: (
                        ProviderCapability.STRUCTURED_GENERATION,
                        ProviderCapability.USAGE_COST,
                    ),
                    ProviderKind.TYPESAFE: (
                        ProviderCapability.RELEVANCE_EVALUATION,
                        ProviderCapability.USAGE_COST,
                    ),
                }[provider]
            )
            for provider in ProviderKind
        },
    )

    def replace_jina(_index: int) -> object:
        return service.replace(
            ProviderKind.JINA,
            SecretStr("jina-secret-value"),
            0,
            "owner",
            datetime(2026, 9, 22, tzinfo=UTC),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(replace_jina, range(2)))

    assert sum(isinstance(result, ProviderCredentialStored) for result in results) == 1
    assert sum(isinstance(result, ProviderCredentialChanged) for result in results) == 1
    for provider in (ProviderKind.OPENROUTER, ProviderKind.TYPESAFE):
        result = service.replace(
            provider,
            SecretStr(f"{provider.value}-secret-value"),
            0,
            "owner",
            datetime(2026, 9, 22, tzinfo=UTC),
        )
        assert isinstance(result, ProviderCredentialStored)

    advanced = service.advance()

    assert advanced.state.stage is OnboardingStage.PREFERENCES
    assert service.inspect().ready
    assert service.resolve(ProviderKind.JINA).get_secret_value() == "jina-secret-value"
    with _connection(authority_schema) as connection:
        runtime_credentials = resolve_execution_provider_credentials(
            connection,
            cipher=cipher,
            jina_fallback=None,
            openrouter_fallback=None,
            typesafe_fallback=None,
        )
        assert runtime_credentials.jina.get_secret_value() == "jina-secret-value"
        rows = connection.execute(
            "SELECT nonce, ciphertext FROM provider_credentials ORDER BY provider"
        ).fetchall()
        assert len(rows) == 3
        assert all(b"secret-value" not in cast(bytes, row[1]) for row in rows)
        with pytest.raises(psycopg.errors.CheckViolation, match="cannot be deleted"):
            connection.execute("DELETE FROM provider_credentials WHERE provider = 'jina'")

    with _connection(authority_schema) as connection:
        active = get_active_search_configuration(connection)
    activation = postgres_onboarding_progress_service(
        lambda: _connection(authority_schema)
    ).activate_preferences(
        ActivateConfigurationCommand(
            target_revision_id=active.active.revision.id,
            expected_active_revision_id=active.active.revision.id,
            expected_generation=active.active.generation,
            actor="owner",
            timestamp=datetime(2026, 9, 22, tzinfo=UTC),
        )
    )

    assert isinstance(activation, ConfigurationActivated)
    assert owner.load_state().stage is OnboardingStage.BUDGET

    budget = postgres_budget_setup_service(lambda: _connection(authority_schema))
    budget_result = budget.save(
        0,
        Decimal("4"),
        Decimal("2"),
        10,
        "owner",
        datetime(2026, 9, 22, tzinfo=UTC),
    )
    assert isinstance(budget_result, BudgetSaved)
    assert owner.load_state().stage is OnboardingStage.TEST_SEARCH
    with _connection(authority_schema) as connection:
        _ = connection.execute(
            """
            UPDATE owner_onboarding
            SET stage = 'complete', updated_at = CURRENT_TIMESTAMP
            WHERE singleton_id = 1
            """
        )

        _ = connection.execute(
            """
            INSERT INTO execution_budget_reservations (
              idempotency_key, policy_version, period_start, reserved_usd,
              status, max_jobs, authority_kind, created_at
            ) VALUES (
              'legacy-in-flight', 1, DATE '2026-09-01', 0.5,
              'reserved', 3, 'legacy', %s
            )
            """,
            (datetime(2026, 9, 22, tzinfo=UTC),),
        )
        legacy = admit_scheduled_execution(
            connection,
            idempotency_key="legacy-in-flight",
            requested_at=datetime(2026, 9, 22, tzinfo=UTC),
        )
        replayed_legacy = admit_scheduled_execution(
            connection,
            idempotency_key="legacy-in-flight",
            requested_at=datetime(2026, 9, 22, tzinfo=UTC),
        )
        legacy_row = connection.execute(
            """
            SELECT authority_kind, configuration_revision_id,
                   prompt_release_id, relevance_release_id
            FROM execution_budget_reservations
            WHERE idempotency_key = 'legacy-in-flight'
            """
        ).fetchone()

    assert isinstance(legacy, ExecutionAdmitted)
    assert replayed_legacy == legacy
    assert legacy.max_jobs == 3
    assert legacy.estimate.jobs_per_run == 3
    assert legacy_row == (
        "pinned",
        legacy.configuration_revision_id,
        legacy.target.prompt_release_id,
        legacy.target.relevance_release_id,
    )
    with _connection(authority_schema) as connection:
        changed_configuration = store_search_configuration_revision(
            connection,
            build_search_configuration_revision(
                DEFAULT_SEARCH_CONFIGURATION.model_copy(
                    update={"search_keywords": ("different authority",)}
                ),
                created_at=datetime(2026, 9, 22, tzinfo=UTC),
                created_by="test",
            ),
        )
        with pytest.raises(psycopg.errors.CheckViolation, match="reserved execution authority"):
            prepare_orchestration_run(
                connection,
                idempotency_key="legacy-in-flight",
                implementation_ref="test",
                configuration_revision_id=changed_configuration.id,
                target=legacy.target,
                started_at=datetime(2026, 9, 22, tzinfo=UTC),
                fetch_rates=lambda: ExchangeRateSnapshot(
                    rates={"EUR": Decimal("1.11")},
                    source="fallback",
                    observed_at=datetime(2026, 9, 22, tzinfo=UTC),
                ),
            )

    barrier = Barrier(2)

    def admit(index: int) -> object:
        _ = barrier.wait()
        with _connection(authority_schema) as connection:
            return admit_scheduled_execution(
                connection,
                idempotency_key=f"scheduled-run-{index}",
                requested_at=datetime(2026, 9, 22, tzinfo=UTC),
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        admissions = tuple(executor.map(admit, range(2)))

    assert sum(isinstance(result, ExecutionAdmitted) for result in admissions) == 1
    assert sum(isinstance(result, ExecutionBlocked) for result in admissions) == 1
    admitted_index, admitted = next(
        (index, result)
        for index, result in enumerate(admissions)
        if isinstance(result, ExecutionAdmitted)
    )
    with _connection(authority_schema) as connection:
        reservation_row = connection.execute(
            """
            SELECT idempotency_key, authority_kind, configuration_revision_id, prompt_release_id,
                   relevance_release_id, release_generation, search_queries,
                   logical_model_calls_per_job, maximum_provider_attempts
            FROM execution_budget_reservations
            WHERE idempotency_key = %s
            """,
            (f"scheduled-run-{admitted_index}",),
        ).fetchone()
        assert reservation_row is not None
        reservation_key = cast(str, reservation_row[0])
        assert reservation_row[1:] == (
            "pinned",
            admitted.configuration_revision_id,
            admitted.target.prompt_release_id,
            admitted.target.relevance_release_id,
            admitted.release_generation,
            admitted.estimate.search_queries,
            admitted.estimate.logical_model_calls_per_job,
            admitted.estimate.maximum_provider_attempts,
        )
        with pytest.raises(psycopg.errors.CheckViolation, match="authority is immutable"):
            connection.execute(
                """
                UPDATE execution_budget_reservations
                SET maximum_provider_attempts = maximum_provider_attempts + 1
                WHERE idempotency_key = %s
                """,
                (reservation_key,),
            )
        assert reserve_discovery(connection, reservation_key)
        assert not reserve_discovery(connection, reservation_key)
        assert reserve_job_capacity(connection, reservation_key) == 10
        assert reserve_job_capacity(connection, reservation_key) == 0
        _ = connection.execute(
            """
            UPDATE execution_budget_policy
            SET max_search_queries_per_run = 1, version = version + 1
            WHERE singleton_id = 1
            """
        )
        assert admit_scheduled_execution(
            connection,
            idempotency_key="configuration-over-policy",
            requested_at=datetime(2026, 9, 22, tzinfo=UTC),
        ) == ExecutionBlocked(reason="configuration_exceeds_policy")


def test_approved_release_target_activation_is_exact_cas_and_replay_safe(
    authority_schema: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    with _connection(authority_schema) as connection:
        manifest_id, _, rates = _seed_evaluation_execution_context(connection, now)
        active = get_active_release_target(connection)
        prompt_release = load_prompt_release(connection, active.target.prompt_release_id)
        candidate_relevance = store_relevance_release(
            connection,
            build_relevance_release(build_gemini_policy(prompt_release)),
            created_at=now,
            created_by="contract",
        )
        candidate_target = ReleaseTarget(
            prompt_release_id=prompt_release.id,
            relevance_release_id=candidate_relevance.id,
        )

        def expected_result(
            case: EvaluationManifestCase, _target: ReleaseTarget, _trial: int
        ) -> EvaluationResult:
            if case.expected_outcome == "qualified":
                return Qualified(reason="Expected positive.", profile_name="profile")
            return Rejected(reason="Expected negative.")

        baseline_execution = run_manifest(
            connection,
            command=EvaluateManifestCommand(
                idempotency_key="release-target:baseline",
                manifest_id=manifest_id,
                target=active.target,
                implementation_ref="baseline",
            ),
            create_exchange_rates=lambda: rates,
            create_evaluator=lambda _rates, _record: expected_result,
            now=lambda: now,
        )
        candidate_execution = run_manifest(
            connection,
            command=EvaluateManifestCommand(
                idempotency_key="release-target:candidate",
                manifest_id=manifest_id,
                target=candidate_target,
                implementation_ref="candidate",
            ),
            create_exchange_rates=lambda: rates,
            create_evaluator=lambda _rates, _record: expected_result,
            now=lambda: now,
        )
        assert isinstance(baseline_execution, CompletedEvaluationExecution)
        assert isinstance(candidate_execution, CompletedEvaluationExecution)
        comparison = preview_run_comparison(
            connection, baseline_execution.run.id, candidate_execution.run.id
        )
        decision = record_prompt_promotion_decision(
            connection,
            baseline_run_id=baseline_execution.run.id,
            candidate_run_id=candidate_execution.run.id,
            expected_comparison_id=comparison.id,
            decision="approved",
            reason="Exact target passed the frozen manifest.",
            actor="owner",
            created_at=now,
            idempotency_key="release-target:decision",
        )
        command = ActivateReleaseTargetCommand(
            idempotency_key="release-target:activate",
            promotion_decision_id=decision.id,
            expected_active_target=active.target,
            expected_generation=active.generation,
            actor="owner",
            timestamp=now + timedelta(minutes=1),
        )
        budget = postgres_budget_setup_service(lambda: _connection(authority_schema))
        estimate_before_activation = budget.inspect(10).estimate
        source_artifact_identity = relevance_releases_module.source_artifact_identity

        def drifted_source_artifact_identity(
            entrypoint: str, source_path: Path
        ) -> CodeArtifactIdentity:
            return source_artifact_identity(entrypoint, source_path).model_copy(
                update={"content_digest": "0" * 64}
            )

        with monkeypatch.context() as patch:
            patch.setattr(
                relevance_releases_module,
                "source_artifact_identity",
                drifted_source_artifact_identity,
            )
            with pytest.raises(
                ReleaseTargetLifecycleError,
                match="implementation artifacts do not match",
            ):
                activate_release_target(
                    connection,
                    command.model_copy(update={"idempotency_key": "release-target:drifted"}),
                )
        assert get_active_release_target(connection) == active
        activated = activate_release_target(connection, command)
        estimate_after_activation = budget.inspect(10).estimate
        replayed = activate_release_target(connection, command)
        with monkeypatch.context() as patch:
            patch.setattr(
                relevance_releases_module,
                "source_artifact_identity",
                drifted_source_artifact_identity,
            )
            stale = activate_release_target(
                connection,
                command.model_copy(
                    update={
                        "idempotency_key": "release-target:stale",
                        "timestamp": now + timedelta(minutes=2),
                    }
                ),
            )
        orchestration_run = prepare_orchestration_run(
            connection,
            idempotency_key="release-target:orchestration",
            implementation_ref="candidate",
            configuration_revision_id=load_published_active_search_configuration(
                connection
            ).publication.revision_id,
            target=candidate_target,
            started_at=now + timedelta(minutes=3),
            fetch_rates=lambda: rates,
        )

        assert isinstance(activated, ReleaseTargetActivated)
        assert activated.replayed is False
        assert activated.active.target == candidate_target
        assert activated.active.generation == active.generation + 1
        assert estimate_before_activation.maximum_provider_attempts == 400
        assert estimate_after_activation.maximum_provider_attempts == 640
        assert isinstance(replayed, ReleaseTargetActivated)
        assert replayed.replayed is True
        assert replayed.active == activated.active
        assert isinstance(stale, ActiveReleaseTargetChanged)
        assert stale.active == activated.active
        assert orchestration_run.target == candidate_target
        assert connection.execute(
            "SELECT count(*) FROM release_target_activation_receipts"
        ).fetchone() == (2,)
        with pytest.raises(psycopg.errors.CheckViolation, match="immutable"):
            connection.execute("UPDATE release_target_activation_receipts SET actor = 'tampered'")
        with pytest.raises(psycopg.errors.CheckViolation, match="approved activation receipt"):
            connection.execute(
                "UPDATE active_release_target SET generation = generation + 1 WHERE singleton_id = 1"
            )
        with pytest.raises(psycopg.errors.CheckViolation, match="immutable"):
            connection.execute("DELETE FROM active_release_target")


def test_run_stored_manifest_fails_sanitized_without_provider_keys_and_replays(
    authority_schema: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    rates = ExchangeRateSnapshot(rates={"EUR": Decimal("1.11")}, source="fallback", observed_at=now)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    def fixed_rates(*, observed_at: datetime) -> ExchangeRateSnapshot:
        assert observed_at.tzinfo is not None
        return rates

    monkeypatch.setattr(manifest_execution_module, "fetch_exchange_rates", fixed_rates)
    with _connection(authority_schema) as connection:
        manifest_id, target, _seeded_rates = _seed_evaluation_execution_context(connection, now)
        command = EvaluateManifestCommand(
            idempotency_key="evaluate-manifest:missing-key",
            manifest_id=manifest_id,
            target=target,
            implementation_ref="stored-manifest",
        )

        failed = run_stored_manifest(connection, command)
        replayed = run_stored_manifest(connection, command)

    assert isinstance(failed, FailedEvaluationExecution)
    assert failed.failure.code == "unexpected_exception"
    assert failed.failure.error_type == "ValueError"
    assert "TYPESAFE_API_KEY" not in failed.failure.message
    assert replayed == failed


def test_release_target_lifecycle_rejects_unapproved_and_mismatched_commands(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    with _connection(authority_schema) as connection:
        manifest_id, _, rates = _seed_evaluation_execution_context(connection, now)
        active = get_active_release_target(connection)
        prompt_release = load_prompt_release(connection, active.target.prompt_release_id)
        candidate_relevance = store_relevance_release(
            connection,
            build_relevance_release(build_jev_faithful_policy(prompt_release)),
            created_at=now,
            created_by="contract",
        )
        candidate_target = ReleaseTarget(
            prompt_release_id=prompt_release.id,
            relevance_release_id=candidate_relevance.id,
        )

        def expected_result(
            case: EvaluationManifestCase, _target: ReleaseTarget, _trial: int
        ) -> EvaluationResult:
            if case.expected_outcome == "qualified":
                return Qualified(reason="Expected positive.", profile_name="profile")
            return Rejected(reason="Expected negative.")

        baseline_execution = run_manifest(
            connection,
            command=EvaluateManifestCommand(
                idempotency_key="lifecycle-guard:baseline",
                manifest_id=manifest_id,
                target=active.target,
                implementation_ref="baseline",
            ),
            create_exchange_rates=lambda: rates,
            create_evaluator=lambda _rates, _record: expected_result,
            now=lambda: now,
        )
        candidate_execution = run_manifest(
            connection,
            command=EvaluateManifestCommand(
                idempotency_key="lifecycle-guard:candidate",
                manifest_id=manifest_id,
                target=candidate_target,
                implementation_ref="candidate",
            ),
            create_exchange_rates=lambda: rates,
            create_evaluator=lambda _rates, _record: expected_result,
            now=lambda: now,
        )
        assert isinstance(baseline_execution, CompletedEvaluationExecution)
        assert isinstance(candidate_execution, CompletedEvaluationExecution)
        comparison = preview_run_comparison(
            connection, baseline_execution.run.id, candidate_execution.run.id
        )
        approved = record_prompt_promotion_decision(
            connection,
            baseline_run_id=baseline_execution.run.id,
            candidate_run_id=candidate_execution.run.id,
            expected_comparison_id=comparison.id,
            decision="approved",
            reason="Exact target passed the frozen manifest.",
            actor="owner",
            created_at=now,
            idempotency_key="lifecycle-guard:decision-approved",
        )
        activated = activate_release_target(
            connection,
            ActivateReleaseTargetCommand(
                idempotency_key="lifecycle-guard:activate",
                promotion_decision_id=approved.id,
                expected_active_target=active.target,
                expected_generation=active.generation,
                actor="owner",
                timestamp=now + timedelta(minutes=1),
            ),
        )
        with pytest.raises(
            ReleaseTargetLifecycleError,
            match="different release-target activation",
        ):
            activate_release_target(
                connection,
                ActivateReleaseTargetCommand(
                    idempotency_key="lifecycle-guard:activate",
                    promotion_decision_id=approved.id,
                    expected_active_target=active.target,
                    expected_generation=active.generation + 5,
                    actor="owner",
                    timestamp=now + timedelta(minutes=2),
                ),
            )
        with pytest.raises(ValueError, match="already has a promotion decision"):
            record_prompt_promotion_decision(
                connection,
                baseline_run_id=baseline_execution.run.id,
                candidate_run_id=candidate_execution.run.id,
                expected_comparison_id=comparison.id,
                decision="rejected",
                reason="Second opinion.",
                actor="owner",
                created_at=now + timedelta(minutes=3),
                idempotency_key="lifecycle-guard:decision-duplicate",
            )

        second_candidate_execution = run_manifest(
            connection,
            command=EvaluateManifestCommand(
                idempotency_key="lifecycle-guard:candidate-2",
                manifest_id=manifest_id,
                target=candidate_target,
                implementation_ref="candidate",
            ),
            create_exchange_rates=lambda: rates,
            create_evaluator=lambda _rates, _record: expected_result,
            now=lambda: now,
        )
        assert isinstance(second_candidate_execution, CompletedEvaluationExecution)
        second_comparison = preview_run_comparison(
            connection, baseline_execution.run.id, second_candidate_execution.run.id
        )
        rejected = record_prompt_promotion_decision(
            connection,
            baseline_run_id=baseline_execution.run.id,
            candidate_run_id=second_candidate_execution.run.id,
            expected_comparison_id=second_comparison.id,
            decision="rejected",
            reason="Not taking this target.",
            actor="owner",
            created_at=now,
            idempotency_key="lifecycle-guard:decision-rejected",
        )
        with pytest.raises(
            ReleaseTargetLifecycleError,
            match="Approved promotion decision does not exist",
        ):
            activate_release_target(
                connection,
                ActivateReleaseTargetCommand(
                    idempotency_key="lifecycle-guard:activate-rejected",
                    promotion_decision_id=rejected.id,
                    expected_active_target=active.target,
                    expected_generation=active.generation,
                    actor="owner",
                    timestamp=now + timedelta(minutes=4),
                ),
            )

        assert isinstance(activated, ReleaseTargetActivated)
        assert get_active_release_target(connection) == activated.active


def test_release_target_migration_bootstraps_configurable_prompt_criteria(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 21, 12, tzinfo=UTC)
    criterion = DEFAULT_SEARCH_CONFIGURATION.personal_criteria[0].model_copy(
        update={"key": "custom-criterion"}
    )
    configuration = DEFAULT_SEARCH_CONFIGURATION.model_copy(
        update={
            "personal_criteria": (
                criterion,
                *DEFAULT_SEARCH_CONFIGURATION.personal_criteria[1:],
            )
        }
    )
    with _connection(authority_schema) as connection:
        _apply_migrations_through(connection, "0025_release_target_promotion_decisions.sql")
        revision = store_search_configuration_revision(
            connection,
            build_search_configuration_revision(
                configuration, created_at=now, created_by="contract"
            ),
        )
        prompt_release = store_prompt_release(
            connection,
            build_prompt_release(configuration),
            created_at=now,
            created_by="contract",
        )
        _ = connection.execute(
            """
            INSERT INTO search_configuration_publications (
              revision_id, prompt_release_id, published_at, published_by
            ) VALUES (%s, %s, %s, %s)
            """,
            (revision.id, prompt_release.id, now, "contract"),
        )
        _ = connection.execute(
            """
            INSERT INTO active_search_configuration (
              singleton_id, revision_id, generation, activated_at, activated_by
            ) VALUES (1, %s, 0, %s, %s)
            """,
            (revision.id, now, "contract"),
        )

        apply_migrations(connection)

        active = get_active_release_target(connection)
        relevance_release = load_relevance_release(connection, active.target.relevance_release_id)
        validate_release_target(active.target, prompt_release, relevance_release)
        assert isinstance(relevance_release.policy, JevFaithfulExecutionPolicy)


def test_unbounded_review_note_migration_preserves_feedback_and_accepts_long_notes(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 21, 12, tzinfo=UTC)
    run_id = uuid4()
    note = " " + "x" * 1999 + " "
    with _connection(authority_schema) as connection:
        _apply_migrations_through(connection, "0022_relevance_releases.sql")
        release = bootstrap_prompt_release(connection)
        _insert_prompt_run(connection, run_id, release.id, now)
        evaluation_id = _insert_review_decision(connection, run_id, release.id, now, 1, "qualified")
        assert enqueue_qualified_review_item(connection, evaluation_id, now.date())
        item = load_review_queue(connection).items[0]
        ordinary = ReviewSubmission(
            review_item_id=item.id,
            evaluation_id=item.evaluation_id,
            snapshot_id=item.snapshot_id,
            decision="unsure",
            note="Need more detail.",
            actor="owner",
            created_at=now,
        )
        saved_ordinary = record_review(connection, ordinary)
        assert isinstance(saved_ordinary, ReviewSaved)

        long_note = ordinary.model_copy(
            update={"note": note, "created_at": now + timedelta(seconds=1)}
        )
        with pytest.raises(psycopg.errors.CheckViolation, match="review_events_note_check"):
            record_review(connection, long_note)

        assert apply_migrations(connection)[-10:] == EXPECTED_MIGRATIONS[-10:]
        saved_long = record_review(connection, long_note)
        assert isinstance(saved_long, ReviewSaved)
        assert len(note) == 2001
        assert connection.execute(
            "SELECT id, note FROM review_events ORDER BY created_at"
        ).fetchall() == [
            (saved_ordinary.review_event_id, ordinary.note),
            (saved_long.review_event_id, note),
        ]


def test_concurrent_migration_startup_serializes_schema_writes(
    authority_schema: str,
) -> None:
    barrier = Barrier(2)

    def migrate() -> tuple[str, ...]:
        with _connection(authority_schema) as connection:
            _ = barrier.wait()
            return apply_migrations(connection)

    def migrate_for_index(_index: int) -> tuple[str, ...]:
        return migrate()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(migrate_for_index, range(2)))

    assert results[0] == results[1] == EXPECTED_MIGRATIONS


def test_search_configuration_migration_preserves_every_legacy_row(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    run_id = uuid4()
    with _connection(authority_schema) as connection:
        _apply_migrations_through(connection, "0015_manifest_idempotency.sql")
        release = bootstrap_prompt_release(connection)
        _insert_prompt_run(connection, run_id, release.id, now)
        decision_id = _insert_review_decision(connection, run_id, release.id, now, 91, "qualified")
        assert enqueue_qualified_review_item(connection, decision_id, now.date())
        review_item = load_review_queue(connection).items[0]
        saved = record_review(
            connection,
            ReviewSubmission(
                review_item_id=review_item.id,
                evaluation_id=review_item.evaluation_id,
                snapshot_id=review_item.snapshot_id,
                decision="pursue",
                target_profile="applied-ai-product-engineer",
                actor="owner",
                created_at=now,
            ),
        )
        assert isinstance(saved, ReviewSaved)
        include_review_event(
            connection,
            review_event_id=saved.review_event_id,
            critical=True,
            reason="Preserve this evidence.",
            actor="owner",
            created_at=now,
            idempotency_key="configuration-migration-preservation",
        )
        _ = create_manifest(
            connection,
            policy=ManifestPolicy(),
            created_at=now,
            created_by="owner",
            idempotency_key="configuration-migration-preservation",
        )
        legacy_tables = _public_tables(connection)
        before = _table_contents(connection, legacy_tables)

        apply_migrations(connection)

        expected = dict(before)
        expected["pipeline_runs"] = [
            {**row, "configuration_revision_id": None, "relevance_release_id": None}
            for row in cast(list[dict[str, object]], before["pipeline_runs"])
        ]
        expected["evaluation_decisions"] = [
            {
                **row,
                "relevance_release_id": None,
                "source_snapshot_id": None,
                "predecessor_decision_id": None,
                "reevaluation_request_key": None,
            }
            for row in cast(list[dict[str, object]], before["evaluation_decisions"])
        ]
        assert _table_contents(connection, legacy_tables) == expected


def test_configuration_revision_migration_backfills_only_orchestration_runs(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    orchestration_run_id = uuid4()
    processing_run_id = uuid4()
    with _connection(authority_schema) as connection:
        _apply_migrations_through(connection, "0019_configuration_publication_receipts.sql")
        release_id = _seed_initial_configuration_publication(connection, now)
        _insert_legacy_orchestration_run(connection, orchestration_run_id, release_id, now)
        _insert_prompt_run(connection, processing_run_id, release_id, now)

        apply_migrations(connection)

        rows = connection.execute(
            """
            SELECT id, configuration_revision_id
            FROM pipeline_runs
            ORDER BY id
            """
        ).fetchall()
        revisions = {UUID(str(row[0])): row[1] for row in rows}
        assert revisions == {
            orchestration_run_id: INITIAL_SEARCH_CONFIGURATION_REVISION_ID,
            processing_run_id: None,
        }


def test_configuration_revision_migration_preserves_unmapped_historical_release(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    historical_run_id = uuid4()
    with _connection(authority_schema) as connection:
        _apply_migrations_through(connection, "0019_configuration_publication_receipts.sql")
        _seed_initial_configuration_publication(connection, now)
        changed_configuration = DEFAULT_SEARCH_CONFIGURATION.model_copy(
            update={
                "personal_criteria": (
                    DEFAULT_SEARCH_CONFIGURATION.personal_criteria[0].model_copy(
                        update={"instructions": "A deliberately different criterion."}
                    ),
                    *DEFAULT_SEARCH_CONFIGURATION.personal_criteria[1:],
                )
            }
        )
        unexpected_release = store_prompt_release(
            connection,
            build_prompt_release(changed_configuration),
            created_at=now,
            created_by="test",
        )
        _insert_legacy_orchestration_run(connection, historical_run_id, unexpected_release.id, now)

        apply_migrations(connection)

        assert connection.execute(
            "SELECT configuration_revision_id FROM pipeline_runs WHERE id = %s",
            (historical_run_id,),
        ).fetchone() == (None,)
        with pytest.raises(psycopg.Error, match="cannot infer configuration revision"):
            _insert_legacy_orchestration_run(connection, uuid4(), unexpected_release.id, now)


def test_configuration_revision_migration_repairs_missing_initial_publication(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    initial_revision = build_search_configuration_revision(
        DEFAULT_SEARCH_CONFIGURATION,
        created_at=now,
        created_by="migration:0016_search_configuration_revisions.sql",
    )
    custom_revision = build_search_configuration_revision(
        DEFAULT_SEARCH_CONFIGURATION.model_copy(
            update={"search_keywords": ("custom active search",)}
        ),
        created_at=now,
        created_by="owner",
    )
    with _connection(authority_schema) as connection:
        _apply_migrations_through(connection, "0019_configuration_publication_receipts.sql")
        _ = store_search_configuration_revision(connection, initial_revision)
        _ = store_search_configuration_revision(connection, custom_revision)
        release = store_prompt_release(
            connection,
            build_prompt_release(custom_revision.configuration),
            created_at=now,
            created_by="owner",
        )
        connection.execute(
            """
            INSERT INTO search_configuration_publications (
              revision_id, prompt_release_id, published_at, published_by
            ) VALUES (%s, %s, %s, 'owner')
            """,
            (custom_revision.id, release.id, now),
        )
        connection.execute(
            """
            INSERT INTO search_configuration_drafts (
              singleton_id, base_revision_id, version, content, updated_at, updated_by
            ) VALUES (1, %s, 0, %s, %s, 'owner')
            """,
            (
                custom_revision.id,
                Jsonb(custom_revision.configuration.model_dump(mode="json")),
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO active_search_configuration (
              singleton_id, revision_id, generation, activated_at, activated_by
            ) VALUES (1, %s, 0, %s, 'owner')
            """,
            (custom_revision.id, now),
        )

        apply_migrations(connection)

        repaired = load_search_configuration_publication(connection, initial_revision.id)
        assert repaired.prompt_release_id == release.id


def test_pipeline_run_rejects_incomplete_release_target(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        publication = load_published_active_search_configuration(connection).publication
        changed_configuration = DEFAULT_SEARCH_CONFIGURATION.model_copy(
            update={
                "personal_criteria": (
                    DEFAULT_SEARCH_CONFIGURATION.personal_criteria[0].model_copy(
                        update={"instructions": "Another deliberately different criterion."}
                    ),
                    *DEFAULT_SEARCH_CONFIGURATION.personal_criteria[1:],
                )
            }
        )
        other_release = store_prompt_release(
            connection,
            build_prompt_release(changed_configuration),
            created_at=now,
            created_by="test",
        )

        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                """
                INSERT INTO pipeline_runs (
                  id, idempotency_key, kind, implementation_ref,
                  configuration_revision_id, prompt_release_id, parameters,
                  status, started_at
                ) VALUES (%s, %s, 'orchestration', 'test-ref', %s, %s,
                  '{}'::jsonb, 'running', %s)
                """,
                (
                    uuid4(),
                    "mismatched-configuration-publication",
                    publication.revision_id,
                    other_release.id,
                    now,
                ),
            )


def test_pipeline_run_configuration_shape_rejects_legacy_orchestration_writers(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    run_id = uuid4()
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        publication = load_search_configuration_publication(
            connection,
            SearchConfigurationRevisionId(INITIAL_SEARCH_CONFIGURATION_REVISION_ID),
        )

        with pytest.raises(psycopg.errors.CheckViolation):
            _insert_legacy_orchestration_run(connection, run_id, publication.prompt_release_id, now)
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                """
                INSERT INTO pipeline_runs (
                  id, idempotency_key, kind, implementation_ref,
                  configuration_revision_id, prompt_release_id, parameters,
                  status, started_at
                ) VALUES (%s, %s, 'processing', 'test-ref', %s, %s,
                  '{}'::jsonb, 'running', %s)
                """,
                (
                    uuid4(),
                    "processing-with-configuration",
                    SearchConfigurationRevisionId(INITIAL_SEARCH_CONFIGURATION_REVISION_ID),
                    publication.prompt_release_id,
                    now,
                ),
            )


def test_initial_search_configuration_is_seeded_as_draft_revision_and_active(
    authority_schema: str,
) -> None:
    with _connection(authority_schema) as connection:
        apply_migrations(connection)

        active = load_active_search_configuration(connection)
        draft = load_search_configuration_draft(connection)
        stored = load_search_configuration_revision(connection, active.revision.id)
        publication = load_search_configuration_publication(connection, active.revision.id)
        release = load_prompt_release(connection, publication.prompt_release_id)

        assert active.generation == 0
        assert active.revision.configuration == DEFAULT_SEARCH_CONFIGURATION
        assert stored == active.revision
        assert draft.base_revision_id == active.revision.id
        assert draft.version == 0
        assert draft.configuration == DEFAULT_SEARCH_CONFIGURATION
        assert publication.revision_id == active.revision.id
        assert publication.published_by == "migration:0017_search_configuration_publications.sql"
        assert release == build_prompt_release(DEFAULT_SEARCH_CONFIGURATION)


def test_configuration_revision_reads_use_stable_compound_keyset_pagination(
    authority_schema: str,
) -> None:
    base_time = datetime(2030, 1, 1, tzinfo=UTC)
    configurations = tuple(
        DEFAULT_SEARCH_CONFIGURATION.model_copy(update={"search_keywords": (f"revision-{index}",)})
        for index in range(4)
    )
    revisions = (
        build_search_configuration_revision(
            configurations[0], created_at=base_time + timedelta(hours=2), created_by="owner-0"
        ),
        build_search_configuration_revision(
            configurations[1], created_at=base_time + timedelta(hours=1), created_by="owner-1"
        ),
        build_search_configuration_revision(
            configurations[2], created_at=base_time + timedelta(hours=1), created_by="owner-2"
        ),
        build_search_configuration_revision(
            configurations[3], created_at=base_time, created_by="owner-3"
        ),
    )
    expected = sorted(revisions, key=lambda item: (item.created_at, item.id), reverse=True)

    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        for revision in revisions:
            _ = store_search_configuration_revision(connection, revision)
        _publish_configuration_revision(connection, expected[1], base_time + timedelta(hours=3))

        first = list_search_configuration_revisions(connection, limit=2)
        assert [item.revision_id for item in first.items] == [item.id for item in expected[:2]]
        assert first.next_cursor == ConfigurationRevisionCursor(
            created_at=expected[1].created_at,
            revision_id=expected[1].id,
        )
        publications = {item.revision_id: item.publication for item in first.items}
        assert publications[expected[1].id] is not None
        unpublished = next(item for item in first.items if item.revision_id != expected[1].id)
        assert unpublished.publication is None

        inserted = build_search_configuration_revision(
            DEFAULT_SEARCH_CONFIGURATION.model_copy(
                update={"search_keywords": ("inserted-between-pages",)}
            ),
            created_at=base_time + timedelta(hours=4),
            created_by="later-owner",
        )
        _ = store_search_configuration_revision(connection, inserted)

        second = list_search_configuration_revisions(
            connection,
            limit=2,
            cursor=first.next_cursor,
        )
        assert [item.revision_id for item in second.items] == [item.id for item in expected[2:]]
        assert not (
            {item.revision_id for item in first.items} & {item.revision_id for item in second.items}
        )
        assert inserted.id not in {item.revision_id for item in second.items}

        published_details = get_search_configuration_revision(connection, expected[1].id)
        assert published_details.revision == expected[1]
        assert published_details.publication is not None
        assert published_details.publication.revision_id == expected[1].id
        unpublished_details = get_search_configuration_revision(connection, revisions[3].id)
        assert unpublished_details.revision == revisions[3]
        assert unpublished_details.publication is None
        assert get_active_search_configuration(connection).active.revision.id == (
            SearchConfigurationRevisionId(INITIAL_SEARCH_CONFIGURATION_REVISION_ID)
        )
        assert get_search_configuration_draft(connection).base_revision_id == (
            SearchConfigurationRevisionId(INITIAL_SEARCH_CONFIGURATION_REVISION_ID)
        )


def test_get_search_configuration_revision_reports_a_missing_revision(
    authority_schema: str,
) -> None:
    missing_revision_id = SearchConfigurationRevisionId(token_hex(32))
    with _connection(authority_schema) as connection:
        apply_migrations(connection)

        with pytest.raises(ConfigurationRevisionNotFound):
            get_search_configuration_revision(connection, missing_revision_id)


def test_mcp_configuration_flow_pins_the_activated_revision_on_the_next_run(
    authority_schema: str,
) -> None:
    configured_profile = DEFAULT_SEARCH_CONFIGURATION.target_profiles[0].model_copy(
        update={"instructions": "Prefer roles with direct product ownership."}
    )
    configured = DEFAULT_SEARCH_CONFIGURATION.model_copy(
        update={
            "search_keywords": ("mcp-configured-search",),
            "enabled_sources": (SupportedSearchSource.LEVER,),
            "target_profiles": (
                configured_profile,
                *DEFAULT_SEARCH_CONFIGURATION.target_profiles[1:],
            ),
        }
    )
    expected_revision_id = search_configuration_revision_id(configured)

    with _connection(authority_schema) as connection:
        apply_migrations(connection)

    server = create_mcp_server(
        McpDependencies(
            connect=lambda: _connection(authority_schema),
            actor="mcp-contract-owner",
            now=lambda: datetime(2030, 1, 2, tzinfo=UTC),
        )
    )

    async def configure() -> (
        tuple[
            PublishedActiveSearchConfiguration,
            ConfigurationPreview,
            ConfigurationPublished,
        ]
    ):
        async with Client(server) as client:
            active_result = await client.call_tool("configuration_active_get", {})
            active = PublishedActiveSearchConfiguration.model_validate(
                active_result.structured_content
            )

            draft_result = await client.call_tool("configuration_draft_get", {})
            draft = SearchConfigurationDraft.model_validate(draft_result.structured_content)
            assert active.active.revision.configuration == DEFAULT_SEARCH_CONFIGURATION
            assert draft.configuration == DEFAULT_SEARCH_CONFIGURATION

            valid_result = await client.call_tool(
                "configuration_validate",
                {"candidate": configured.model_dump(mode="json")},
            )
            assert valid_result.structured_content is not None
            valid = ConfigurationValid.model_validate(valid_result.structured_content["result"])
            assert valid.configuration == configured

            preview_result = await client.call_tool(
                "configuration_preview",
                {"configuration": configured.model_dump(mode="json")},
            )
            preview = ConfigurationPreview.model_validate(preview_result.structured_content)
            assert preview.configuration_revision_id == expected_revision_id
            assert preview.total_generated_search_count == 1
            assert preview.search_samples == ("site:jobs.lever.co mcp-configured-search",)
            expected_prompt = build_prompt_release(configured)
            expected_profile_version = next(
                version
                for version in expected_prompt.versions
                if version.definition.phase == "profile"
                and version.definition.criterion == configured_profile.key
            )
            profile_summary = next(
                summary
                for summary in preview.prompt_summaries
                if summary.phase == "profile" and summary.criterion == configured_profile.key
            )
            assert preview.total_compiled_prompt_count == len(expected_prompt.versions)
            assert profile_summary.prompt_version_id == expected_profile_version.id

            saved_result = await client.call_tool(
                "configuration_draft_update",
                {
                    "expected_version": draft.version,
                    "configuration": configured.model_dump(mode="json"),
                },
            )
            assert saved_result.structured_content is not None
            saved = DraftSaved.model_validate(saved_result.structured_content["result"])

            published_result = await client.call_tool(
                "configuration_publish",
                {
                    "idempotency_key": "mcp-contract-publish",
                    "expected_draft_version": saved.draft.version,
                    "expected_configuration_revision_id": expected_revision_id,
                },
            )
            assert published_result.structured_content is not None
            published = ConfigurationPublished.model_validate(
                published_result.structured_content["result"]
            )
            assert published.publication.revision_id == expected_revision_id
            assert published.publication.prompt_release_id == preview.prompt_release_id

            listed_result = await client.call_tool("configuration_revision_list", {"limit": 100})
            listed = ConfigurationRevisionPage.model_validate(listed_result.structured_content)
            assert expected_revision_id in {item.revision_id for item in listed.items}

            details_result = await client.call_tool(
                "configuration_revision_get", {"revision_id": expected_revision_id}
            )
            details = ConfigurationRevisionDetails.model_validate(details_result.structured_content)
            assert details.revision.configuration == configured

            activated_result = await client.call_tool(
                "configuration_activate",
                {
                    "target_revision_id": expected_revision_id,
                    "expected_active_revision_id": active.active.revision.id,
                    "expected_generation": active.active.generation,
                },
            )
            assert activated_result.structured_content is not None
            activated = ConfigurationActivated.model_validate(
                activated_result.structured_content["result"]
            )
            assert activated.active_configuration.active.revision.id == expected_revision_id
            return active, preview, published

    initial_active, preview, published = asyncio.run(configure())

    searches: list[tuple[str, str]] = []

    def search(keyword: str, domain: str) -> SearchSucceeded:
        searches.append((keyword, domain))
        return SearchSucceeded(urls=())

    with _connection(authority_schema) as connection:
        active_target = get_active_release_target(connection)
        run = prepare_orchestration_run(
            connection,
            idempotency_key="mcp-configured-run",
            implementation_ref="mcp-contract",
            configuration_revision_id=expected_revision_id,
            target=active_target.target,
            started_at=datetime(2030, 1, 3, tzinfo=UTC),
            fetch_rates=lambda: ExchangeRateSnapshot(
                rates={"EUR": Decimal("1.11")},
                source="frankfurter",
                observed_at=datetime(2030, 1, 3, tzinfo=UTC),
            ),
        )
        discovery = discover_jobs(
            connection,
            run,
            PipelineBoundaries(
                search=search,
                scrape=lambda _url: pytest.fail("scrape was called"),
                fetch_ats=lambda _url, _title: pytest.fail("ATS was called"),
            ),
            discovered_at=datetime(2030, 1, 3, tzinfo=UTC),
            max_workers=1,
        )
        stored_revision = load_search_configuration_revision(
            connection, run.configuration_revision_id
        )
        published_prompt = load_prompt_release(connection, published.publication.prompt_release_id)

        assert run.configuration_revision_id == expected_revision_id
        assert run.target == active_target.target
        assert run.prompt_release_id == initial_active.publication.prompt_release_id
        assert run.prompt_release_id != published.publication.prompt_release_id
        assert stored_revision.configuration == configured
        assert published_prompt == build_prompt_release(configured)
        assert preview.prompt_release_id == published_prompt.id
        assert discovery.query_count == 1
        assert searches == [("mcp-configured-search", "jobs.lever.co")]


def test_configuration_editor_inspects_the_live_draft_active_and_saved_revisions(
    authority_schema: str,
) -> None:
    with _connection(authority_schema) as connection:
        apply_migrations(connection)

    service = postgres_configuration_editor_service(connect=lambda: _connection(authority_schema))
    state = service.inspect()

    assert state.draft.version == 0
    assert state.draft.base_revision_id == SearchConfigurationRevisionId(
        INITIAL_SEARCH_CONFIGURATION_REVISION_ID
    )
    assert state.draft.configuration == DEFAULT_SEARCH_CONFIGURATION
    assert state.active.active.generation == 0
    assert state.active.active.revision.id == SearchConfigurationRevisionId(
        INITIAL_SEARCH_CONFIGURATION_REVISION_ID
    )
    assert state.active.publication.revision_id == SearchConfigurationRevisionId(
        INITIAL_SEARCH_CONFIGURATION_REVISION_ID
    )
    assert state.content_revision_id == SearchConfigurationRevisionId(
        INITIAL_SEARCH_CONFIGURATION_REVISION_ID
    )
    assert state.saved_revision is not None
    assert state.saved_revision.revision.id == SearchConfigurationRevisionId(
        INITIAL_SEARCH_CONFIGURATION_REVISION_ID
    )
    assert state.saved_revision.publication is not None


def test_mcp_configuration_conflicts_surface_as_structured_results(
    authority_schema: str,
) -> None:
    unpublished_configuration = DEFAULT_SEARCH_CONFIGURATION.model_copy(
        update={"search_keywords": ("mcp-never-published",)}
    )
    unpublished_revision = build_search_configuration_revision(
        unpublished_configuration,
        created_at=datetime(2030, 1, 2, tzinfo=UTC),
        created_by="mcp-contract-owner",
    )
    first_publication_configuration = DEFAULT_SEARCH_CONFIGURATION.model_copy(
        update={"search_keywords": ("mcp-conflict-first",)}
    )
    second_publication_configuration = DEFAULT_SEARCH_CONFIGURATION.model_copy(
        update={"search_keywords": ("mcp-conflict-second",)}
    )
    idempotency_key = "mcp-contract-conflict-publish"

    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        _ = store_search_configuration_revision(connection, unpublished_revision)

    server = create_mcp_server(
        McpDependencies(
            connect=lambda: _connection(authority_schema),
            actor="mcp-contract-owner",
            now=lambda: datetime(2030, 1, 2, tzinfo=UTC),
        )
    )

    async def conflict_flows() -> None:
        async with Client(server) as client:
            draft_result = await client.call_tool("configuration_draft_get", {})
            draft = SearchConfigurationDraft.model_validate(draft_result.structured_content)

            stale_result = await client.call_tool(
                "configuration_draft_update",
                {
                    "expected_version": draft.version + 1,
                    "configuration": first_publication_configuration.model_dump(mode="json"),
                },
            )
            assert stale_result.structured_content is not None
            stale = DraftChanged.model_validate(stale_result.structured_content["result"])
            assert stale.current_draft == draft

            saved_result = await client.call_tool(
                "configuration_draft_update",
                {
                    "expected_version": draft.version,
                    "configuration": first_publication_configuration.model_dump(mode="json"),
                },
            )
            assert saved_result.structured_content is not None
            saved = DraftSaved.model_validate(saved_result.structured_content["result"])
            assert saved.draft.version == draft.version + 1

            published_result = await client.call_tool(
                "configuration_publish",
                {
                    "idempotency_key": idempotency_key,
                    "expected_draft_version": saved.draft.version,
                    "expected_configuration_revision_id": search_configuration_revision_id(
                        first_publication_configuration
                    ),
                },
            )
            assert published_result.structured_content is not None
            published = ConfigurationPublished.model_validate(
                published_result.structured_content["result"]
            )
            assert published.replayed is False
            assert published.publication.revision_id == search_configuration_revision_id(
                first_publication_configuration
            )

            rebased_result = await client.call_tool("configuration_draft_get", {})
            rebased = SearchConfigurationDraft.model_validate(rebased_result.structured_content)
            assert rebased.version == saved.draft.version + 1

            resaved_result = await client.call_tool(
                "configuration_draft_update",
                {
                    "expected_version": rebased.version,
                    "configuration": second_publication_configuration.model_dump(mode="json"),
                },
            )
            assert resaved_result.structured_content is not None
            resaved = DraftSaved.model_validate(resaved_result.structured_content["result"])

            conflict_result = await client.call_tool(
                "configuration_publish",
                {
                    "idempotency_key": idempotency_key,
                    "expected_draft_version": resaved.draft.version,
                    "expected_configuration_revision_id": search_configuration_revision_id(
                        second_publication_configuration
                    ),
                },
            )
            assert conflict_result.structured_content is not None
            conflict = PublicationIdempotencyKeyConflict.model_validate(
                conflict_result.structured_content["result"]
            )
            assert conflict.idempotency_key == idempotency_key

            active_result = await client.call_tool("configuration_active_get", {})
            active = PublishedActiveSearchConfiguration.model_validate(
                active_result.structured_content
            )

            unpublished_target_result = await client.call_tool(
                "configuration_activate",
                {
                    "target_revision_id": unpublished_revision.id,
                    "expected_active_revision_id": active.active.revision.id,
                    "expected_generation": active.active.generation,
                },
            )
            assert unpublished_target_result.structured_content is not None
            unpublished_target = ActivationTargetUnpublished.model_validate(
                unpublished_target_result.structured_content["result"]
            )
            assert unpublished_target.target_revision_id == unpublished_revision.id

    asyncio.run(conflict_flows())

    with _connection(authority_schema) as connection:
        assert connection.execute(
            """
            SELECT outcome FROM search_configuration_publication_receipts
            WHERE idempotency_key = %s
            """,
            (idempotency_key,),
        ).fetchall() == [("published",)]
        assert connection.execute(
            "SELECT revision_id FROM active_search_configuration WHERE singleton_id = 1"
        ).fetchone() == (INITIAL_SEARCH_CONFIGURATION_REVISION_ID,)


def test_configuration_service_saves_a_draft_and_preserves_its_base(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    changed = DEFAULT_SEARCH_CONFIGURATION.model_copy(update={"search_keywords": ("changed",)})
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        initial = load_search_configuration_draft(connection)

        result = save_search_configuration_draft(
            connection,
            SaveDraftCommand(
                expected_version=initial.version,
                configuration=changed,
                actor="owner",
                timestamp=now,
            ),
        )

        assert isinstance(result, DraftSaved)
        assert result.draft.version == initial.version + 1
        assert result.draft.base_revision_id == initial.base_revision_id
        assert result.draft.configuration == changed
        assert result.draft.updated_by == "owner"
        assert result.draft.updated_at == now


def test_configuration_service_returns_current_draft_for_a_stale_save(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    first_change = DEFAULT_SEARCH_CONFIGURATION.model_copy(update={"search_keywords": ("first",)})
    stale_change = DEFAULT_SEARCH_CONFIGURATION.model_copy(update={"search_keywords": ("stale",)})
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        saved = save_search_configuration_draft(
            connection,
            SaveDraftCommand(
                expected_version=0,
                configuration=first_change,
                actor="first-owner",
                timestamp=now,
            ),
        )
        assert isinstance(saved, DraftSaved)

        result = save_search_configuration_draft(
            connection,
            SaveDraftCommand(
                expected_version=0,
                configuration=stale_change,
                actor="stale-owner",
                timestamp=now + timedelta(seconds=1),
            ),
        )

        assert isinstance(result, DraftChanged)
        assert result.current_draft == saved.draft


def test_configuration_service_loses_to_a_publication_style_draft_rebase(
    authority_schema: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    published_configuration = DEFAULT_SEARCH_CONFIGURATION.model_copy(
        update={"search_keywords": ("published",)}
    )
    requested_configuration = DEFAULT_SEARCH_CONFIGURATION.model_copy(
        update={"search_keywords": ("requested",)}
    )
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        published_revision = build_search_configuration_revision(
            published_configuration,
            created_at=now,
            created_by="publisher",
        )
        _ = store_search_configuration_revision(connection, published_revision)
        _publish_configuration_revision(connection, published_revision, now)
        load_count = 0

        def load_then_rebase(
            service_connection: psycopg.Connection[tuple[object, ...]],
        ) -> SearchConfigurationDraft:
            nonlocal load_count
            draft = load_search_configuration_draft(service_connection)
            load_count += 1
            if load_count == 1:
                service_connection.execute(
                    """
                    UPDATE search_configuration_drafts
                    SET base_revision_id = %s, content = %s, version = version + 1,
                        updated_at = %s, updated_by = 'publisher'
                    WHERE singleton_id = 1
                    """,
                    (
                        published_revision.id,
                        Jsonb(published_configuration.model_dump(mode="json")),
                        now,
                    ),
                )
            return draft

        monkeypatch.setattr(
            configuration_service_module,
            "load_search_configuration_draft",
            load_then_rebase,
        )

        result = save_search_configuration_draft(
            connection,
            SaveDraftCommand(
                expected_version=0,
                configuration=requested_configuration,
                actor="owner",
                timestamp=now + timedelta(seconds=1),
            ),
        )

        assert isinstance(result, DraftChanged)
        assert result.current_draft.version == 1
        assert result.current_draft.base_revision_id == published_revision.id
        assert result.current_draft.configuration == published_configuration


def test_publication_migration_backfills_custom_active_and_draft_revisions(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    draft_revision = build_search_configuration_revision(
        DEFAULT_SEARCH_CONFIGURATION.model_copy(update={"search_keywords": ("draft-only",)}),
        created_at=now,
        created_by="owner",
    )
    active_revision = build_search_configuration_revision(
        DEFAULT_SEARCH_CONFIGURATION.model_copy(update={"search_keywords": ("active-only",)}),
        created_at=now,
        created_by="owner",
    )
    with _connection(authority_schema) as connection:
        _apply_migrations_through(connection, "0016_search_configuration_revisions.sql")
        initial_revision = build_search_configuration_revision(
            DEFAULT_SEARCH_CONFIGURATION,
            created_at=now,
            created_by="migration:0016_search_configuration_revisions.sql",
        )
        _ = store_search_configuration_revision(connection, initial_revision)
        _ = store_search_configuration_revision(connection, draft_revision)
        _ = store_search_configuration_revision(connection, active_revision)
        connection.execute(
            """
            INSERT INTO search_configuration_drafts (
              singleton_id, base_revision_id, version, content, updated_at, updated_by
            ) VALUES (1, %s, 0, %s, %s, 'owner')
            """,
            (draft_revision.id, Jsonb(draft_revision.configuration.model_dump(mode="json")), now),
        )
        connection.execute(
            """
            INSERT INTO active_search_configuration (
              singleton_id, revision_id, generation, activated_at, activated_by
            ) VALUES (1, %s, 0, %s, 'owner')
            """,
            (active_revision.id, now),
        )

        apply_migrations(connection)

        draft_publication = load_search_configuration_publication(connection, draft_revision.id)
        active_publication = load_search_configuration_publication(connection, active_revision.id)
        initial_publication = load_search_configuration_publication(
            connection,
            SearchConfigurationRevisionId(INITIAL_SEARCH_CONFIGURATION_REVISION_ID),
        )
        assert draft_publication.prompt_release_id == active_publication.prompt_release_id
        assert initial_publication.prompt_release_id == active_publication.prompt_release_id
        assert connection.execute(
            "SELECT count(*) FROM search_configuration_publications"
        ).fetchone() == (3,)
        assert connection.execute("SELECT count(*) FROM prompt_releases").fetchone() == (1,)
        assert load_search_configuration_draft(connection).base_revision_id == draft_revision.id
        assert load_active_search_configuration(connection).revision.id == active_revision.id


def test_configuration_publication_is_atomic_rebases_once_and_replays(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    changed = DEFAULT_SEARCH_CONFIGURATION.model_copy(update={"search_keywords": ("principal",)})
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        initial_active = load_active_search_configuration(connection)
        saved = replace_search_configuration_draft(
            connection,
            expected_version=0,
            base_revision_id=initial_active.revision.id,
            configuration=changed,
            updated_at=now,
            updated_by="owner",
        )
        assert saved is not None
        command = _publication_command(saved, "publish:first", now)

        first = publish_search_configuration(connection, command)
        retry = publish_search_configuration(
            connection,
            command.model_copy(update={"timestamp": now + timedelta(hours=1)}),
        )

        assert isinstance(first, ConfigurationPublished)
        assert isinstance(retry, ConfigurationPublished)
        assert not first.replayed
        assert retry.replayed
        assert retry.publication == first.publication
        assert retry.rebased_draft == first.rebased_draft
        assert first.rebased_draft.version == 2
        assert load_search_configuration_draft(connection) == first.rebased_draft
        assert load_active_search_configuration(connection) == initial_active
        assert (
            first.publication.prompt_release_id
            == load_search_configuration_publication(
                connection, initial_active.revision.id
            ).prompt_release_id
        )
        assert load_prompt_release(
            connection, first.publication.prompt_release_id
        ) == build_prompt_release(changed)
        assert connection.execute(
            "SELECT count(*) FROM search_configuration_publication_receipts"
        ).fetchone() == (1,)

        conflict = publish_search_configuration(
            connection,
            command.model_copy(update={"actor": "other"}),
        )
        assert isinstance(conflict, PublicationIdempotencyKeyConflict)
        assert load_search_configuration_draft(connection).version == 2

        repeated_content = publish_search_configuration(
            connection,
            PublishConfigurationCommand(
                idempotency_key="publish:second",
                expected_draft_version=2,
                expected_configuration_revision_id=first.publication.revision_id,
                actor="owner",
                timestamp=now + timedelta(hours=2),
            ),
        )
        assert isinstance(repeated_content, ConfigurationPublished)
        assert repeated_content.publication == first.publication
        assert repeated_content.rebased_draft.version == 3
        assert connection.execute("SELECT count(*) FROM prompt_releases").fetchone() == (1,)


def test_configuration_publication_durably_replays_stale_revision_and_version(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        draft = load_search_configuration_draft(connection)
        wrong_revision = SearchConfigurationRevisionId("f" * 64)
        command = PublishConfigurationCommand(
            idempotency_key="publish:stale",
            expected_draft_version=draft.version,
            expected_configuration_revision_id=wrong_revision,
            actor="owner",
            timestamp=now,
        )

        first = publish_search_configuration(connection, command)
        assert isinstance(first, PublishDraftChanged)
        assert first.expected_configuration_revision_id == wrong_revision
        assert first.observed_configuration_revision_id == search_configuration_revision_id(
            draft.configuration
        )
        assert not first.replayed

        changed = replace_search_configuration_draft(
            connection,
            expected_version=draft.version,
            base_revision_id=draft.base_revision_id,
            configuration=draft.configuration.model_copy(update={"search_keywords": ("later",)}),
            updated_at=now + timedelta(minutes=1),
            updated_by="owner",
        )
        assert changed is not None
        replay = publish_search_configuration(connection, command)
        assert isinstance(replay, PublishDraftChanged)
        assert replay.replayed
        assert replay.model_copy(update={"replayed": False}) == first


def test_configuration_publication_rolls_back_every_write(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    changed = DEFAULT_SEARCH_CONFIGURATION.model_copy(
        update={
            "search_keywords": ("rollback",),
            "target_profiles": (
                DEFAULT_SEARCH_CONFIGURATION.target_profiles[0].model_copy(
                    update={"instructions": "A profile that must roll back."}
                ),
                *DEFAULT_SEARCH_CONFIGURATION.target_profiles[1:],
            ),
        }
    )
    release = build_prompt_release(changed)
    default_release = build_prompt_release(DEFAULT_SEARCH_CONFIGURATION)
    new_prompt_version_ids = {version.id for version in release.versions} - {
        version.id for version in default_release.versions
    }
    assert new_prompt_version_ids
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        draft = load_search_configuration_draft(connection)
        saved = replace_search_configuration_draft(
            connection,
            expected_version=draft.version,
            base_revision_id=draft.base_revision_id,
            configuration=changed,
            updated_at=now,
            updated_by="owner",
        )
        assert saved is not None
        revision_id = search_configuration_revision_id(changed)
        connection.execute(
            """
            CREATE FUNCTION fail_configuration_publication_receipt()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
              RAISE EXCEPTION 'publication receipt insertion failed';
            END;
            $$
            """
        )
        connection.execute(
            """
            CREATE TRIGGER fail_configuration_publication_receipt
            AFTER INSERT ON search_configuration_publication_receipts
            FOR EACH ROW EXECUTE FUNCTION fail_configuration_publication_receipt()
            """
        )
        with pytest.raises(psycopg.errors.RaiseException, match="receipt insertion failed"):
            publish_search_configuration(
                connection,
                _publication_command(saved, "publish:rollback", now),
            )

        assert load_search_configuration_draft(connection) == saved
        assert connection.execute(
            "SELECT count(*) FROM search_configuration_revisions WHERE id = %s", (revision_id,)
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM prompt_releases WHERE id = %s", (release.id,)
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM prompt_versions WHERE id = ANY(%s)",
            (list(new_prompt_version_ids),),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM search_configuration_publications WHERE revision_id = %s",
            (revision_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM search_configuration_publication_receipts"
        ).fetchone() == (0,)


def test_concurrent_configuration_publications_serialize_retries_and_drafts(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        draft = load_search_configuration_draft(connection)
    command = _publication_command(draft, "publish:concurrent", now)
    start = Barrier(2)

    def publish_same(_index: int) -> ConfigurationPublished:
        with _connection(authority_schema) as connection:
            _ = start.wait()
            result = publish_search_configuration(connection, command)
            assert isinstance(result, ConfigurationPublished)
            return result

    with ThreadPoolExecutor(max_workers=2) as executor:
        same_key = tuple(executor.map(publish_same, range(2)))
    assert sorted(result.replayed for result in same_key) == [False, True]
    assert same_key[0].rebased_draft == same_key[1].rebased_draft

    with _connection(authority_schema) as connection:
        current = load_search_configuration_draft(connection)
    start = Barrier(2)

    def publish_different(index: int) -> ConfigurationPublished | PublishDraftChanged:
        with _connection(authority_schema) as connection:
            _ = start.wait()
            result = publish_search_configuration(
                connection,
                _publication_command(current, f"publish:different:{index}", now),
            )
            assert isinstance(result, (ConfigurationPublished, PublishDraftChanged))
            return result

    with ThreadPoolExecutor(max_workers=2) as executor:
        different_keys = tuple(executor.map(publish_different, range(2)))
    assert sum(isinstance(result, ConfigurationPublished) for result in different_keys) == 1
    assert sum(isinstance(result, PublishDraftChanged) for result in different_keys) == 1


def test_configuration_activation_returns_published_state_and_strict_cas_results(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        initial = load_published_active_search_configuration(connection)
        draft = load_search_configuration_draft(connection)
        changed_draft = replace_search_configuration_draft(
            connection,
            expected_version=draft.version,
            base_revision_id=draft.base_revision_id,
            configuration=draft.configuration.model_copy(update={"search_keywords": ("activate",)}),
            updated_at=now,
            updated_by="owner",
        )
        assert changed_draft is not None
        published = publish_search_configuration(
            connection,
            _publication_command(changed_draft, "publish:activate", now),
        )
        assert isinstance(published, ConfigurationPublished)

        unpublished = activate_search_configuration(
            connection,
            ActivateConfigurationCommand(
                target_revision_id=SearchConfigurationRevisionId("f" * 64),
                expected_active_revision_id=initial.active.revision.id,
                expected_generation=initial.active.generation,
                actor="owner",
                timestamp=now,
            ),
        )
        assert isinstance(unpublished, ActivationTargetUnpublished)

        activated = activate_search_configuration(
            connection,
            ActivateConfigurationCommand(
                target_revision_id=published.publication.revision_id,
                expected_active_revision_id=initial.active.revision.id,
                expected_generation=initial.active.generation,
                actor="owner",
                timestamp=now,
            ),
        )
        assert isinstance(activated, ConfigurationActivated)
        assert activated.active_configuration.publication == published.publication

        stale = activate_search_configuration(
            connection,
            ActivateConfigurationCommand(
                target_revision_id=initial.active.revision.id,
                expected_active_revision_id=initial.active.revision.id,
                expected_generation=initial.active.generation,
                actor="owner",
                timestamp=now,
            ),
        )
        assert isinstance(stale, ActiveConfigurationChanged)
        assert stale.active_configuration == activated.active_configuration


def test_publications_are_immutable_and_pointers_require_publication(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        initial = load_active_search_configuration(connection)
        unpublished = build_search_configuration_revision(
            DEFAULT_SEARCH_CONFIGURATION.model_copy(update={"search_keywords": ("unpublished",)}),
            created_at=now,
            created_by="owner",
        )
        _ = store_search_configuration_revision(connection, unpublished)

        with pytest.raises(
            psycopg.errors.ForeignKeyViolation,
            match="search_configuration_drafts_base_revision_publication_fk",
        ):
            connection.execute(
                """
                UPDATE search_configuration_drafts
                SET base_revision_id = %s, version = version + 1
                WHERE singleton_id = 1
                """,
                (unpublished.id,),
            )
        with pytest.raises(
            psycopg.errors.ForeignKeyViolation,
            match="active_search_configuration_revision_publication_fk",
        ):
            connection.execute(
                """
                UPDATE active_search_configuration
                SET revision_id = %s, generation = generation + 1
                WHERE singleton_id = 1
                """,
                (unpublished.id,),
            )
        with pytest.raises(psycopg.errors.CheckViolation, match="immutable"):
            connection.execute(
                """
                UPDATE search_configuration_publications
                SET published_by = 'other'
                WHERE revision_id = %s
                """,
                (initial.revision.id,),
            )


def test_search_configuration_revision_and_pointer_invariants(authority_schema: str) -> None:
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        initial = load_active_search_configuration(connection)
        changed_configuration = DEFAULT_SEARCH_CONFIGURATION.model_copy(
            update={
                "search_keywords": (*DEFAULT_SEARCH_CONFIGURATION.search_keywords, "cto café"),
                "personal_criteria": (
                    DEFAULT_SEARCH_CONFIGURATION.personal_criteria[0].model_copy(
                        update={"name": "Éligibilité géographique"}
                    ),
                    *DEFAULT_SEARCH_CONFIGURATION.personal_criteria[1:],
                ),
            }
        )
        changed = build_search_configuration_revision(
            changed_configuration,
            created_at=now,
            created_by="owner",
        )
        assert store_search_configuration_revision(connection, changed) == changed
        assert store_search_configuration_revision(connection, changed) == changed
        _publish_configuration_revision(connection, changed, now)

        with pytest.raises(psycopg.errors.CheckViolation, match="immutable"):
            connection.execute(
                "UPDATE search_configuration_revisions SET created_by = 'other' WHERE id = %s",
                (changed.id,),
            )

        draft = replace_search_configuration_draft(
            connection,
            expected_version=0,
            base_revision_id=initial.revision.id,
            configuration=changed_configuration,
            updated_at=now,
            updated_by="owner",
        )
        assert draft is not None
        assert draft.version == 1
        assert (
            replace_search_configuration_draft(
                connection,
                expected_version=0,
                base_revision_id=initial.revision.id,
                configuration=DEFAULT_SEARCH_CONFIGURATION,
                updated_at=now,
                updated_by="stale-owner",
            )
            is None
        )

        activated = compare_and_swap_active_search_configuration(
            connection,
            expected_revision_id=initial.revision.id,
            expected_generation=0,
            revision_id=changed.id,
            activated_at=now,
            activated_by="owner",
        )
        assert activated is not None
        assert activated.generation == 1
        assert activated.revision == changed
        restored = compare_and_swap_active_search_configuration(
            connection,
            expected_revision_id=changed.id,
            expected_generation=1,
            revision_id=initial.revision.id,
            activated_at=now + timedelta(seconds=1),
            activated_by="owner",
        )
        assert restored is not None
        assert restored.generation == 2
        assert (
            compare_and_swap_active_search_configuration(
                connection,
                expected_revision_id=initial.revision.id,
                expected_generation=0,
                revision_id=changed.id,
                activated_at=now + timedelta(seconds=2),
                activated_by="stale-owner",
            )
            is None
        )


def test_search_configuration_database_rejects_malformed_content(
    authority_schema: str,
) -> None:
    with _connection(authority_schema) as connection:
        apply_migrations(connection)

        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                """
                INSERT INTO search_configuration_revisions (
                  id, content, created_at, created_by
                ) VALUES (%s, '{"schema_version": 1}'::jsonb, CURRENT_TIMESTAMP, 'owner')
                """,
                ("f" * 64,),
            )

        with pytest.raises(
            psycopg.errors.CheckViolation,
            match="search_configuration_revision_matches_content",
        ):
            connection.execute(
                """
                INSERT INTO search_configuration_revisions (
                  id, content, created_at, created_by
                )
                SELECT %s, content, CURRENT_TIMESTAMP, 'owner'
                FROM search_configuration_revisions
                LIMIT 1
                """,
                ("f" * 64,),
            )


def test_concurrent_search_configuration_activation_has_one_winner(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        initial = load_active_search_configuration(connection)
        revisions = tuple(
            build_search_configuration_revision(
                DEFAULT_SEARCH_CONFIGURATION.model_copy(
                    update={
                        "search_keywords": (
                            *DEFAULT_SEARCH_CONFIGURATION.search_keywords,
                            keyword,
                        )
                    }
                ),
                created_at=now,
                created_by="owner",
            )
            for keyword in ("cto", "vp engineering")
        )
        for revision in revisions:
            _ = store_search_configuration_revision(connection, revision)
            _publish_configuration_revision(connection, revision, now)

    activation_start = Barrier(2)

    def activate(revision_index: int) -> bool:
        with _connection(authority_schema) as connection:
            _ = activation_start.wait()
            return isinstance(
                activate_search_configuration(
                    connection,
                    ActivateConfigurationCommand(
                        target_revision_id=revisions[revision_index].id,
                        expected_active_revision_id=initial.revision.id,
                        expected_generation=0,
                        actor=f"owner-{revision_index}",
                        timestamp=now,
                    ),
                ),
                ConfigurationActivated,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(activate, range(2)))

    assert sorted(results) == [False, True]
    with _connection(authority_schema) as connection:
        active = load_active_search_configuration(connection)
    assert active.generation == 1
    assert active.revision.id in {revision.id for revision in revisions}


def test_transaction_rolls_back_receipt_when_projection_fails(authority_schema: str) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    run_id = uuid4()
    job_id = uuid4()
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        _insert_run_and_job(connection, run_id, job_id, now)

        with pytest.raises(psycopg.IntegrityError):
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO pipeline_receipts (
                      id, idempotency_key, pipeline_run_id, job_id, operation_key,
                      input_digest, output_digest, output, implementation_ref, completed_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, '{}'::jsonb, %s, %s)
                    """,
                    (
                        "a" * 64,
                        "process:one",
                        run_id,
                        job_id,
                        "process_job",
                        "b" * 64,
                        "c" * 64,
                        "test-ref",
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO langfuse_projection_items (
                      id, kind, source_id, payload_digest, payload, state, created_at
                    ) VALUES (%s, 'trace', %s, %s, '{}'::jsonb, 'invalid', %s)
                    """,
                    ("d" * 64, str(job_id), "e" * 64, now),
                )

        assert connection.execute("SELECT count(*) FROM pipeline_receipts").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM langfuse_projection_items").fetchone() == (
            0,
        )


def test_immutable_review_target_rejects_rewrites(authority_schema: str) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    run_id = uuid4()
    job_id = uuid4()
    snapshot_id = "1" * 64
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        _insert_run_and_job(connection, run_id, job_id, now)
        connection.execute(
            """
            INSERT INTO job_snapshots (
              id, job_id, content_digest, title, company, normalized_company,
              normalized_title, source, raw_url, description, location, keywords, observed_at
            ) VALUES (%s, %s, %s, 'Engineer', 'Example', 'example', 'engineer', 'other',
              'https://example.com/job', 'Description', 'Remote', '[]'::jsonb, %s)
            """,
            (snapshot_id, job_id, "2" * 64, now),
        )

        with pytest.raises(psycopg.errors.CheckViolation, match="immutable"):
            connection.execute(
                "UPDATE job_snapshots SET title = 'Changed' WHERE id = %s", (snapshot_id,)
            )


def test_run_attempt_identity_treats_a_missing_job_as_equal(authority_schema: str) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    run_id = uuid4()
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        _insert_run(connection, run_id, now)
        values = (uuid4(), run_id, "discover", "3" * 64, now, now)
        connection.execute(
            """
            INSERT INTO processing_attempts (
              id, pipeline_run_id, operation_key, attempt_number, input_digest,
              status, started_at, completed_at
            ) VALUES (%s, %s, %s, 0, %s, 'completed', %s, %s)
            """,
            values,
        )

        with pytest.raises(psycopg.errors.UniqueViolation):
            connection.execute(
                """
                INSERT INTO processing_attempts (
                  id, pipeline_run_id, operation_key, attempt_number, input_digest,
                  status, started_at, completed_at
                ) VALUES (%s, %s, %s, 0, %s, 'completed', %s, %s)
                """,
                (uuid4(), *values[1:]),
            )


def test_prompt_release_requires_the_declared_members(authority_schema: str) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        connection.execute(
            """
            INSERT INTO prompt_versions (
              id, prompt_name, criterion, phase, content_digest, messages,
              input_schema, output_schema, model, parameters, created_at
            ) VALUES (%s, 'profile', 'profile-criterion', 'profile', %s, '[]'::jsonb, '{}'::jsonb, '{}'::jsonb,
              'test-model', '{}'::jsonb, %s)
            """,
            ("4" * 64, "5" * 64, now),
        )
        connection.execute(
            """
            INSERT INTO prompt_versions (
              id, prompt_name, criterion, phase, content_digest, messages,
              input_schema, output_schema, model, parameters, created_at
            ) VALUES (%s, 'other', 'other-criterion', 'profile', %s, '[]'::jsonb, '{}'::jsonb, '{}'::jsonb,
              'test-model', '{}'::jsonb, %s)
            """,
            ("8" * 64, "9" * 64, now),
        )

        with pytest.raises(psycopg.errors.CheckViolation, match="requires 1 members"):
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO prompt_releases (
                      id, name, content_digest, expected_member_count, created_at, created_by
                    ) VALUES (%s, 'release-1', %s, 1, %s, 'test')
                    """,
                    ("6" * 64, "7" * 64, now),
                )

        with connection.transaction():
            connection.execute(
                """
                INSERT INTO prompt_releases (
                  id, name, content_digest, expected_member_count, created_at, created_by
                ) VALUES (%s, 'release-1', %s, 1, %s, 'test')
                """,
                ("6" * 64, "7" * 64, now),
            )
            connection.execute(
                """
                INSERT INTO prompt_release_members (
                  release_id, prompt_name, prompt_version_id, position
                ) VALUES (%s, 'profile', %s, 0)
                """,
                ("6" * 64, "4" * 64),
            )

        with pytest.raises(psycopg.errors.CheckViolation, match="requires 1 members"):
            connection.execute(
                """
                INSERT INTO prompt_release_members (
                  release_id, prompt_name, prompt_version_id, position
                ) VALUES (%s, 'other', %s, 1)
                """,
                ("6" * 64, "8" * 64),
            )


def test_bootstraps_the_complete_prompt_release_idempotently(
    authority_schema: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _connection(authority_schema) as connection:
        apply_migrations(connection)

        first = bootstrap_prompt_release(connection)
        second = bootstrap_prompt_release(connection)

        assert second == first
        monkeypatch.setattr("job_finder.evaluation.prompt_releases.PROMPTS", ())
        assert load_prompt_release(connection, first.id) == first
        assert connection.execute("SELECT count(*) FROM prompt_versions").fetchone() == (8,)
        assert connection.execute("SELECT count(*) FROM prompt_releases").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM prompt_release_members").fetchone() == (8,)


def test_stores_a_custom_prompt_release_exactly_and_idempotently(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    configuration = DEFAULT_SEARCH_CONFIGURATION.model_copy(
        update={
            "personal_criteria": (
                DEFAULT_SEARCH_CONFIGURATION.personal_criteria[0].model_copy(
                    update={"instructions": "Only roles open to candidates in Europe."}
                ),
                *DEFAULT_SEARCH_CONFIGURATION.personal_criteria[1:],
            )
        }
    )
    release = build_prompt_release(configuration)
    with _connection(authority_schema) as connection:
        apply_migrations(connection)

        first = store_prompt_release(connection, release, created_at=now, created_by="contract")
        second = store_prompt_release(
            connection, release, created_at=now + timedelta(minutes=1), created_by="retry"
        )

        assert first == release
        assert second == release
        assert load_prompt_release(connection, release.id) == release
        assert connection.execute(
            "SELECT count(*) FROM prompt_release_members WHERE release_id = %s",
            (release.id,),
        ).fetchone() == (len(release.versions),)
        assert connection.execute(
            "SELECT count(*) FROM prompt_releases WHERE id = %s", (release.id,)
        ).fetchone() == (1,)


def test_stores_an_immutable_relevance_release_exactly_and_idempotently(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 21, 12, tzinfo=UTC)
    prompt_release = build_prompt_release()
    release = build_relevance_release(build_jev_faithful_policy(prompt_release))
    with _connection(authority_schema) as connection:
        apply_migrations(connection)

        first = store_relevance_release(connection, release, created_at=now, created_by="contract")
        second = store_relevance_release(
            connection,
            release,
            created_at=now + timedelta(minutes=1),
            created_by="retry",
        )

        assert first == release
        assert second == release
        assert load_relevance_release(connection, release.id) == release
        assert connection.execute(
            "SELECT created_at, created_by FROM relevance_releases WHERE id = %s",
            (release.id,),
        ).fetchone() == (now, "contract")
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                """
                INSERT INTO relevance_releases (
                  id, content_digest, content, created_at, created_by
                ) VALUES (%s, %s, '{}'::jsonb, %s, 'contract')
                """,
                ("1" * 64, "1" * 64, now),
            )
        with pytest.raises(psycopg.errors.CheckViolation, match="immutable"):
            connection.execute(
                "UPDATE relevance_releases SET created_by = 'other' WHERE id = %s",
                (release.id,),
            )


def test_relevance_release_load_rejects_corrupt_content(authority_schema: str) -> None:
    now = datetime(2026, 9, 21, 12, tzinfo=UTC)
    release = build_relevance_release(build_gemini_policy(build_prompt_release()))
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        store_relevance_release(connection, release, created_at=now, created_by="contract")
        connection.execute(
            "ALTER TABLE relevance_releases DISABLE TRIGGER relevance_releases_are_immutable"
        )
        connection.execute(
            "ALTER TABLE relevance_releases DROP CONSTRAINT relevance_release_digest_matches_content"
        )
        connection.execute(
            'UPDATE relevance_releases SET content = content || \'{"model": "corrupt"}\'::jsonb WHERE id = %s',
            (release.id,),
        )

        with pytest.raises(RelevanceReleaseError, match="identity is corrupt"):
            load_relevance_release(connection, release.id)


def test_stores_a_prompt_release_inside_a_committed_outer_transaction(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    configuration = DEFAULT_SEARCH_CONFIGURATION.model_copy(
        update={
            "target_profiles": (
                DEFAULT_SEARCH_CONFIGURATION.target_profiles[0].model_copy(
                    update={"instructions": "A profile committed by publication."}
                ),
                *DEFAULT_SEARCH_CONFIGURATION.target_profiles[1:],
            )
        }
    )
    release = build_prompt_release(configuration)
    with _connection(authority_schema) as connection:
        apply_migrations(connection)

        with connection.transaction():
            stored = store_prompt_release(
                connection,
                release,
                created_at=now,
                created_by="contract",
            )

        assert stored == release
        assert load_prompt_release(connection, release.id) == release


def test_outer_transaction_rollback_removes_a_stored_prompt_release(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    configuration = DEFAULT_SEARCH_CONFIGURATION.model_copy(
        update={
            "target_profiles": (
                DEFAULT_SEARCH_CONFIGURATION.target_profiles[0].model_copy(
                    update={"instructions": "A deliberately rolled-back profile."}
                ),
                *DEFAULT_SEARCH_CONFIGURATION.target_profiles[1:],
            )
        }
    )
    release = build_prompt_release(configuration)
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        before_versions = connection.execute("SELECT count(*) FROM prompt_versions").fetchone()

        with pytest.raises(RuntimeError, match="rollback publication"):
            with connection.transaction():
                assert (
                    store_prompt_release(
                        connection,
                        release,
                        created_at=now,
                        created_by="contract",
                    )
                    == release
                )
                raise RuntimeError("rollback publication")

        with pytest.raises(PromptReleaseError, match="Prompt release not found"):
            load_prompt_release(connection, release.id)
        assert (
            connection.execute("SELECT count(*) FROM prompt_versions").fetchone() == before_versions
        )
        assert connection.execute(
            "SELECT count(*) FROM prompt_release_members WHERE release_id = %s",
            (release.id,),
        ).fetchone() == (0,)


def test_bootstrap_fails_loudly_when_a_release_name_is_reused(
    authority_schema: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        first = bootstrap_prompt_release(connection)
        weakened_prompts = tuple(
            replace(prompt, model=None) if prompt.name == ENRICHMENT.name else prompt
            for prompt in PROMPTS
        )
        monkeypatch.setattr("job_finder.evaluation.prompt_releases.PROMPTS", weakened_prompts)

        with pytest.raises(psycopg.errors.UniqueViolation, match="prompt_releases_name_key"):
            bootstrap_prompt_release(connection)

        assert load_prompt_release(connection, first.id) == first


def test_resumes_usage_lookup_then_reuses_an_accepted_model_call(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    run_id = uuid4()
    processing_attempt_id = uuid4()
    input_digest = prompt_input_digest({"job": "job body"})
    calls = 0
    generation_responses = iter(
        (
            HttpResponse(404, '{"error":{"message":"not ready"}}'),
            HttpResponse(
                200,
                '{"data":{"tokens_prompt":12,"tokens_completion":4,"total_cost":0.00012}}',
            ),
        )
    )
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        release = bootstrap_prompt_release(connection)
        connection.execute(
            """
            INSERT INTO pipeline_runs (
              id, idempotency_key, kind, implementation_ref, prompt_release_id, parameters,
              status, started_at, completed_at
            ) VALUES (%s, %s, 'evaluation', 'test-ref', %s, '{}'::jsonb,
              'completed', %s, %s)
            """,
            (run_id, f"evaluation:{run_id}", release.id, now, now),
        )
        connection.execute(
            """
            INSERT INTO processing_attempts (
              id, pipeline_run_id, operation_key, attempt_number, input_digest,
              status, started_at, completed_at
            ) VALUES (%s, %s, 'evaluate_job', 0, %s, 'completed', %s, %s)
            """,
            (processing_attempt_id, run_id, input_digest, now, now),
        )
        context = ModelCallContext(
            processing_attempt_id=processing_attempt_id,
            pipeline_run_id=run_id,
            prompt_release_id=release.id,
            operation_key="evaluate_job",
            input_digest=input_digest,
        )

        def send(
            _url: str,
            _headers: Mapping[str, str],
            _body: dict[str, object],
            _timeout: float,
        ) -> HttpResponse:
            nonlocal calls
            calls += 1
            return HttpResponse(
                status_code=200,
                body=json.dumps(
                    {
                        "id": "generation-1",
                        "model": "google/gemini-2.5-flash-001",
                        "choices": [
                            {
                                "message": {
                                    "tool_calls": [
                                        {
                                            "type": "function",
                                            "function": {
                                                "name": "evaluate_job",
                                                "arguments": json.dumps(
                                                    {"pass": True, "reason": "matched"}
                                                ),
                                            },
                                        }
                                    ]
                                }
                            }
                        ],
                    }
                ),
            )

        def lookup_generation(
            _url: str, _headers: Mapping[str, str], _id: str, _timeout: float
        ) -> HttpResponse:
            return next(generation_responses)

        persistence = postgres_model_call_persistence(connection)
        first = evaluate_prompt(
            release.versions[0],
            {"job": "job body"},
            context,
            persistence,
            api_key="secret",
            sender=send,
            generation_sender=lookup_generation,
            retry_policy=RetryPolicy(max_attempts=1, base_delay_seconds=0),
            sleep=lambda _delay: None,
            now=lambda: now,
        )
        second = evaluate_prompt(
            release.versions[0],
            {"job": "job body"},
            context,
            persistence,
            api_key="secret",
            sender=send,
            generation_sender=lookup_generation,
            retry_policy=RetryPolicy(max_attempts=1, base_delay_seconds=0),
            sleep=lambda _delay: None,
            now=lambda: now,
        )
        third = evaluate_prompt(
            release.versions[0],
            {"job": "job body"},
            context,
            persistence,
            api_key="secret",
            sender=send,
            generation_sender=lookup_generation,
            retry_policy=RetryPolicy(max_attempts=1, base_delay_seconds=0),
            sleep=lambda _delay: None,
            now=lambda: now,
        )

        assert isinstance(first, RetryableOperationalError)
        assert second == CriterionAccepted(
            prompt_name=release.versions[0].definition.name,
            passed=True,
            reason="matched",
        )
        assert third == second
        assert calls == 1
        assert connection.execute(
            "SELECT status FROM model_call_attempts ORDER BY attempt_number"
        ).fetchall() == [("retryable_error",), ("accepted",)]
        assert connection.execute(
            """
            SELECT status, response_model, provider_response_id, input_tokens,
                   output_tokens, cost_usd, parsed_output, request_messages
            FROM model_call_attempts
            WHERE status = 'accepted'
            """
        ).fetchone() == (
            "accepted",
            "google/gemini-2.5-flash-001",
            "generation-1",
            12,
            4,
            Decimal("0.00012000"),
            {"pass": True, "reason": "matched"},
            [
                {"role": "system", "content": release.versions[0].messages[0]["content"]},
                {"role": "user", "content": "job body"},
            ],
        )
        assert connection.execute(
            """
            SELECT kind, payload ->> 'requested_model', payload ->> 'status'
            FROM langfuse_projection_items
            WHERE kind = 'model_call'
              AND payload ->> 'status' = 'accepted'
            """
        ).fetchone() == (
            "model_call",
            "google/gemini-2.5-flash",
            "accepted",
        )
        terminal_processing_attempt_id = uuid4()
        terminal_input_digest = prompt_input_digest({"job": "another job body"})
        connection.execute(
            """
            INSERT INTO processing_attempts (
              id, pipeline_run_id, operation_key, attempt_number, input_digest,
              status, started_at, completed_at
            ) VALUES (%s, %s, 'evaluate_terminal_usage', 0, %s, 'completed', %s, %s)
            """,
            (terminal_processing_attempt_id, run_id, terminal_input_digest, now, now),
        )
        terminal = evaluate_prompt(
            release.versions[0],
            {"job": "another job body"},
            ModelCallContext(
                processing_attempt_id=terminal_processing_attempt_id,
                pipeline_run_id=run_id,
                prompt_release_id=release.id,
                operation_key="evaluate_terminal_usage",
                input_digest=terminal_input_digest,
            ),
            persistence,
            api_key="secret",
            sender=send,
            generation_sender=lambda _url, _headers, _id, _timeout: HttpResponse(
                401, '{"error":{"message":"unauthorized"}}'
            ),
            retry_policy=RetryPolicy(max_attempts=1, base_delay_seconds=0),
            sleep=lambda _delay: None,
            now=lambda: now,
        )

        assert isinstance(terminal, TerminalOperationalError)
        assert connection.execute(
            """
            SELECT status, response_model
            FROM model_call_attempts
            WHERE processing_attempt_id = %s
            """,
            (terminal_processing_attempt_id,),
        ).fetchone() == ("terminal_error", "google/gemini-2.5-flash-001")


def test_records_and_reuses_a_terminal_model_error(authority_schema: str) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    run_id = uuid4()
    processing_attempt_id = uuid4()
    input_digest = prompt_input_digest({"job": "job body"})
    calls = 0
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        release = bootstrap_prompt_release(connection)
        connection.execute(
            """
            INSERT INTO pipeline_runs (
              id, idempotency_key, kind, implementation_ref, prompt_release_id, parameters,
              status, started_at, completed_at
            ) VALUES (%s, %s, 'evaluation', 'test-ref', %s, '{}'::jsonb,
              'completed', %s, %s)
            """,
            (run_id, f"evaluation:{run_id}", release.id, now, now),
        )
        connection.execute(
            """
            INSERT INTO processing_attempts (
              id, pipeline_run_id, operation_key, attempt_number, input_digest,
              status, started_at
            ) VALUES (%s, %s, 'evaluate_job', 0, %s, 'running', %s)
            """,
            (processing_attempt_id, run_id, input_digest, now),
        )
        context = ModelCallContext(
            processing_attempt_id=processing_attempt_id,
            pipeline_run_id=run_id,
            prompt_release_id=release.id,
            operation_key="evaluate_job",
            input_digest=input_digest,
        )

        def send(
            _url: str,
            _headers: Mapping[str, str],
            _body: dict[str, object],
            _timeout: float,
        ) -> HttpResponse:
            nonlocal calls
            calls += 1
            return HttpResponse(400, '{"error":{"message":"bad request"}}')

        persistence = postgres_model_call_persistence(connection)
        first = evaluate_prompt(
            release.versions[0],
            {"job": "job body"},
            context,
            persistence,
            api_key="secret",
            sender=send,
            now=lambda: now,
        )
        second = evaluate_prompt(
            release.versions[0],
            {"job": "job body"},
            context,
            persistence,
            api_key="secret",
            sender=send,
            now=lambda: now,
        )

    assert isinstance(first, TerminalOperationalError)
    assert second == first
    assert calls == 1


def test_accepted_model_call_attempts_require_provider_provenance(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 21, 12, tzinfo=UTC)
    run_id = uuid4()
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        release = bootstrap_prompt_release(connection)
        version = release.versions[0]
        connection.execute(
            """
            INSERT INTO pipeline_runs (
              id, idempotency_key, kind, implementation_ref, prompt_release_id, parameters,
              status, started_at, completed_at
            ) VALUES (%s, %s, 'evaluation', 'provenance-ref', %s, '{}'::jsonb,
              'completed', %s, %s)
            """,
            (run_id, f"evaluation:{run_id}", release.id, now, now),
        )

        def insert_model_call_attempt(
            *,
            provider: str,
            status: str,
            provider_response_id: str | None,
            raw_response: object,
            parsed_output: object,
            response_model: str | None,
            error: object,
            input_tokens: int | None,
            output_tokens: int | None,
            cost_usd: Decimal | None,
        ) -> str:
            request_id = token_hex(32)
            processing_attempt_id = uuid4()
            input_digest = token_hex(32)
            connection.execute(
                """
                INSERT INTO processing_attempts (
                  id, pipeline_run_id, operation_key, attempt_number, input_digest,
                  status, started_at, completed_at
                ) VALUES (%s, %s, %s, 0, %s, 'completed', %s, %s)
                """,
                (processing_attempt_id, run_id, request_id, input_digest, now, now),
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
                  %s, %s, %s, %s, %s, 0, %s, %s, %s, %s,
                  'provenance-model', %s, %s, %s, %s, %s, %s, %s, %s, 1, %s,
                  '[]'::jsonb, %s, %s
                )
                """,
                (
                    uuid4(),
                    processing_attempt_id,
                    run_id,
                    release.id,
                    request_id,
                    request_id,
                    version.definition.name,
                    version.id,
                    input_digest,
                    provider,
                    provider_response_id,
                    status,
                    None if parsed_output is None else Jsonb(parsed_output),
                    None if raw_response is None else Jsonb(raw_response),
                    input_tokens,
                    output_tokens,
                    cost_usd,
                    now,
                    response_model,
                    None if error is None else Jsonb(error),
                ),
            )
            return request_id

        with pytest.raises(psycopg.errors.CheckViolation) as missing_raw_response:
            insert_model_call_attempt(
                provider="typesafe",
                status="accepted",
                provider_response_id=None,
                raw_response=None,
                parsed_output={"pass": True},
                response_model="provenance-model",
                error=None,
                input_tokens=1,
                output_tokens=1,
                cost_usd=Decimal("0.00000001"),
            )
        assert (
            missing_raw_response.value.diag.constraint_name
            == "model_call_attempts_accepted_provenance"
        )

        with pytest.raises(psycopg.errors.CheckViolation) as missing_provider_response:
            insert_model_call_attempt(
                provider="openrouter",
                status="accepted",
                provider_response_id=None,
                raw_response={"id": "generation-1"},
                parsed_output={"pass": True},
                response_model="provenance-model",
                error=None,
                input_tokens=1,
                output_tokens=1,
                cost_usd=Decimal("0.00000001"),
            )
        assert (
            missing_provider_response.value.diag.constraint_name
            == "model_call_attempts_accepted_provenance"
        )

        with pytest.raises(psycopg.errors.CheckViolation) as unsupported_provider:
            insert_model_call_attempt(
                provider="anthropic",
                status="terminal_error",
                provider_response_id=None,
                raw_response=None,
                parsed_output=None,
                response_model=None,
                error={"code": "unauthorized"},
                input_tokens=None,
                output_tokens=None,
                cost_usd=None,
            )
        assert (
            unsupported_provider.value.diag.constraint_name == "model_call_attempts_provider_check"
        )

        relaxed_request_id = insert_model_call_attempt(
            provider="typesafe",
            status="accepted",
            provider_response_id=None,
            raw_response={"choices": []},
            parsed_output={"pass": True},
            response_model="provenance-model",
            error=None,
            input_tokens=1,
            output_tokens=1,
            cost_usd=Decimal("0.00000001"),
        )
        assert connection.execute(
            """
            SELECT provider, status, provider_response_id, raw_response IS NOT NULL
            FROM model_call_attempts
            WHERE request_id = %s
            """,
            (relaxed_request_id,),
        ).fetchone() == ("typesafe", "accepted", None, True)


def test_persists_a_terminal_decision_atomically_and_idempotently(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    run_id = uuid4()
    calls = {"enrichment": 0, "deduplication": 0}
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        release = bootstrap_prompt_release(connection)
        _insert_prompt_run(connection, run_id, release.id, now)
        store = postgres_decision_store(connection)
        listing = _decision_listing()
        context = DecisionContext(
            pipeline_run_id=run_id,
            prompt_release_id=release.id,
            policy_version="policy-1",
            implementation_ref="test-ref",
            observed_at=now,
        )

        def enrich(_listing: JobListing) -> PromptAccepted[EnrichedJob]:
            calls["enrichment"] += 1
            return PromptAccepted(
                prompt_name="job-finder-enrichment", output=_decision_enrichment()
            )

        def deduplicate(
            _title: str, existing_titles: tuple[str, ...]
        ) -> PromptAccepted[TitleDuplicate]:
            calls["deduplication"] += 1
            assert existing_titles == ()
            return PromptAccepted(
                prompt_name="job-finder-title-deduplication",
                output=TitleDuplicate(isDuplicate=False),
            )

        first = process_qualified_job(
            listing,
            Qualified(reason="Matches", profile_name="applied-ai"),
            context,
            store,
            enrich,
            deduplicate,
        )
        second = process_qualified_job(
            listing,
            Qualified(reason="Matches", profile_name="applied-ai"),
            context,
            store,
            enrich,
            deduplicate,
        )

        assert isinstance(first, PersistedDecision)
        assert second == first
        assert first.outcome == "qualified"
        assert first.job == _decision_enrichment()
        assert calls == {"enrichment": 1, "deduplication": 1}
        assert connection.execute("SELECT count(*) FROM jobs").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM job_snapshots").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM evaluation_decisions").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM pipeline_receipts").fetchone() == (1,)


def test_persists_structured_compensation_on_the_snapshot(authority_schema: str) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    run_id = uuid4()
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        release = bootstrap_prompt_release(connection)
        _insert_prompt_run(connection, run_id, release.id, now)
        store = postgres_decision_store(connection)
        listing = _decision_listing()
        context = DecisionContext(
            pipeline_run_id=run_id,
            prompt_release_id=release.id,
            policy_version="policy-1",
            implementation_ref="test-ref",
            observed_at=now,
        )
        result = process_qualified_job(
            listing,
            Qualified(reason="Matches", profile_name="applied-ai"),
            context,
            store,
            lambda _listing: PromptAccepted(
                prompt_name="job-finder-enrichment", output=_decision_enrichment()
            ),
            lambda _title, _existing: PromptAccepted(
                prompt_name="job-finder-title-deduplication",
                output=TitleDuplicate(isDuplicate=False),
            ),
            ats_evidence={
                "kind": "available",
                "source": "ashby",
                "location": "Berlin/Remote",
                "locations": ["Berlin/Remote"],
                "workplace_type": "Remote",
                "country": None,
                "description": None,
                "compensation": {
                    "minimum": 80000,
                    "maximum": 100000,
                    "currency": "EUR",
                    "period": "year",
                },
            },
        )

        assert isinstance(result, PersistedDecision)
        row = connection.execute(
            """
            SELECT compensation_min, compensation_max, compensation_currency,
                   compensation_period, compensation_source
            FROM job_snapshots
            WHERE raw_url = %s
            """,
            (listing.url,),
        ).fetchone()

    assert row == (Decimal("80000"), Decimal("100000"), "EUR", "year", "ats")


def test_persists_llm_extracted_compensation_when_the_ats_has_none(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    run_id = uuid4()
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        release = bootstrap_prompt_release(connection)
        _insert_prompt_run(connection, run_id, release.id, now)
        store = postgres_decision_store(connection)
        listing = _decision_listing()
        context = DecisionContext(
            pipeline_run_id=run_id,
            prompt_release_id=release.id,
            policy_version="policy-1",
            implementation_ref="test-ref",
            observed_at=now,
        )
        result = process_qualified_job(
            listing,
            Qualified(reason="Matches", profile_name="applied-ai"),
            context,
            store,
            lambda _listing: PromptAccepted(
                prompt_name="job-finder-enrichment",
                output=_decision_enrichment().model_copy(
                    update={
                        "compensation": CompensationObservation(
                            minimum=70000, maximum=90000, currency="USD", period="year"
                        )
                    }
                ),
            ),
            lambda _title, _existing: PromptAccepted(
                prompt_name="job-finder-title-deduplication",
                output=TitleDuplicate(isDuplicate=False),
            ),
        )

        assert isinstance(result, PersistedDecision)
        row = connection.execute(
            """
            SELECT compensation_min, compensation_max, compensation_currency,
                   compensation_period, compensation_source
            FROM job_snapshots
            WHERE raw_url = %s
            """,
            (listing.url,),
        ).fetchone()

    assert row == (Decimal("70000"), Decimal("90000"), "USD", "year", "llm")


def test_rolls_back_every_terminal_row_when_the_decision_is_invalid(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        release = bootstrap_prompt_release(connection)
        store = postgres_decision_store(connection)

        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            process_qualified_job(
                _decision_listing(),
                Qualified(reason="Matches", profile_name="applied-ai"),
                DecisionContext(
                    pipeline_run_id=uuid4(),
                    prompt_release_id=release.id,
                    policy_version="policy-1",
                    implementation_ref="test-ref",
                    observed_at=now,
                ),
                store,
                lambda _listing: PromptAccepted(
                    prompt_name="job-finder-enrichment", output=_decision_enrichment()
                ),
                lambda _title, _existing: PromptAccepted(
                    prompt_name="job-finder-title-deduplication",
                    output=TitleDuplicate(isDuplicate=False),
                ),
            )

        assert connection.execute("SELECT count(*) FROM jobs").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM job_snapshots").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM evaluation_decisions").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM pipeline_receipts").fetchone() == (0,)


def test_enqueues_a_stable_review_queue_with_every_qualified_job(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    review_day = now.date()
    run_id = uuid4()
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        release = bootstrap_prompt_release(connection)
        _insert_prompt_run(connection, run_id, release.id, now)
        qualified_ids = tuple(
            _insert_review_decision(connection, run_id, release.id, now, value, "qualified")
            for value in range(1, 5)
        )
        rejected_ids = tuple(
            _insert_review_decision(connection, run_id, release.id, now, value, "rejected")
            for value in range(10, 18)
        )

        enqueued = tuple(
            enqueue_qualified_review_item(connection, evaluation_id, review_day)
            for evaluation_id in qualified_ids
        )
        late_qualified_id = _insert_review_decision(
            connection, run_id, release.id, now, 99, "qualified"
        )
        late_enqueued = enqueue_qualified_review_item(connection, late_qualified_id, review_day)
        repeated = tuple(
            enqueue_qualified_review_item(connection, evaluation_id, review_day)
            for evaluation_id in qualified_ids
        )
        sampled = enqueue_rejected_audit_sample(connection, review_day)
        resampled = enqueue_rejected_audit_sample(connection, review_day)
        queue = load_review_queue(connection)
        rows = connection.execute(
            """
            SELECT evaluation_id, lane, position
            FROM review_items
            ORDER BY lane, position, created_at
            """
        ).fetchall()

        first_sample = deterministic_rejected_sample(review_day, rejected_ids)
        remaining = tuple(sorted(set(rejected_ids) - set(first_sample)))
        second_sample = deterministic_rejected_sample(review_day, remaining)
        audit_rows = [row[0] for row in rows if row[1] == "rejected_audit"]
        audit_positions = sorted(int(str(row[2])) for row in rows if row[1] == "rejected_audit")
        assert all(enqueued)
        assert late_enqueued
        assert not any(repeated)
        assert sampled == 3
        assert resampled == 3
        assert set(audit_rows) == set(first_sample) | set(second_sample)
        assert audit_positions == [0, 0, 1, 1, 2, 2]
        assert set(first_sample).isdisjoint(second_sample)
        assert {item.evaluation_id for item in queue.items} == {
            *qualified_ids,
            late_qualified_id,
            *first_sample,
            *second_sample,
        }
        assert queue.reviewed_counts == {}
        with pytest.raises(psycopg.errors.CheckViolation, match="immutable"):
            connection.execute(
                "UPDATE review_items SET position = 99 WHERE id = %s",
                (queue.items[0].id,),
            )


def test_records_feedback_and_company_block_in_one_exact_transaction(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    run_id = uuid4()
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        release = bootstrap_prompt_release(connection)
        _insert_prompt_run(connection, run_id, release.id, now)
        evaluation_id = _insert_review_decision(connection, run_id, release.id, now, 1, "qualified")
        assert enqueue_qualified_review_item(connection, evaluation_id, now.date())
        item = load_review_queue(connection).items[0]
        assert item is not None
        submission = ReviewSubmission(
            review_item_id=item.id,
            evaluation_id=item.evaluation_id,
            snapshot_id=item.snapshot_id,
            decision="pursue",
            note="Strong fit.",
            block_company=True,
            actor="owner",
            created_at=now,
        )

        first = record_review(connection, submission)
        revision = record_review(
            connection,
            submission.model_copy(
                update={
                    "decision": "reject",
                    "note": "Ukraine-based team.",
                    "created_at": now + timedelta(minutes=5),
                }
            ),
        )

        assert isinstance(first, ReviewSaved)
        assert isinstance(revision, ReviewSaved)
        assert connection.execute("SELECT count(*) FROM review_events").fetchone() == (2,)
        assert connection.execute("SELECT policy FROM company_policies").fetchone() == ("blocked",)
        stored = connection.execute(
            """
            SELECT e.decision, e.target_profile, e.primary_reason, i.id, d.id, s.id
            FROM review_events e
            JOIN review_items i ON i.id = e.review_item_id
            JOIN evaluation_decisions d ON d.id = i.evaluation_id
            JOIN job_snapshots s ON s.id = d.snapshot_id
            WHERE e.created_at = %s
            """,
            (now + timedelta(minutes=5),),
        ).fetchone()
        assert stored == (
            "reject",
            "applied-ai-product-engineer",
            None,
            item.id,
            item.evaluation_id,
            item.snapshot_id,
        )
        queue = load_review_queue(connection)
        assert queue.items == ()
        assert queue.reviewed_counts == {now.date(): 1}


def test_an_identical_revision_is_a_stored_no_op(authority_schema: str) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    run_id = uuid4()
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        release = bootstrap_prompt_release(connection)
        _insert_prompt_run(connection, run_id, release.id, now)
        evaluation_id = _insert_review_decision(connection, run_id, release.id, now, 1, "qualified")
        assert enqueue_qualified_review_item(connection, evaluation_id, now.date())
        item = load_review_queue(connection).items[0]
        assert item is not None
        submission = ReviewSubmission(
            review_item_id=item.id,
            evaluation_id=item.evaluation_id,
            snapshot_id=item.snapshot_id,
            decision="unsure",
            note="Need more detail.",
            actor="owner",
            created_at=now,
        )

        first = record_review(connection, submission)
        repeat = record_review(connection, submission)

        assert isinstance(first, ReviewSaved)
        assert isinstance(repeat, ReviewSaved)
        assert first.review_event_id == repeat.review_event_id
        assert connection.execute("SELECT count(*) FROM review_events").fetchone() == (1,)

        middle = record_review(
            connection,
            submission.model_copy(
                update={"decision": "reject", "created_at": now + timedelta(minutes=1)}
            ),
        )
        restored = record_review(
            connection,
            submission.model_copy(update={"created_at": now + timedelta(minutes=2)}),
        )
        assert isinstance(middle, ReviewSaved)
        assert isinstance(restored, ReviewSaved)
        assert len({first.review_event_id, middle.review_event_id, restored.review_event_id}) == 3
        assert connection.execute("SELECT count(*) FROM review_events").fetchone() == (3,)
        assert load_review_queue(connection).reviewed_items[0].decision == "unsure"


def test_rolls_back_feedback_when_company_policy_fails(authority_schema: str) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    run_id = uuid4()
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        release = bootstrap_prompt_release(connection)
        _insert_prompt_run(connection, run_id, release.id, now)
        evaluation_id = _insert_review_decision(connection, run_id, release.id, now, 1, "qualified")
        assert enqueue_qualified_review_item(connection, evaluation_id, now.date())
        item = load_review_queue(connection).items[0]
        assert item is not None
        connection.execute(
            """
            CREATE FUNCTION reject_company_policy_for_contract() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'policy unavailable'; END; $$
            """
        )
        connection.execute(
            """
            CREATE TRIGGER reject_company_policy_for_contract
            BEFORE INSERT OR UPDATE ON company_policies
            FOR EACH ROW EXECUTE FUNCTION reject_company_policy_for_contract()
            """
        )

        with pytest.raises(psycopg.Error, match="policy unavailable"):
            record_review(
                connection,
                ReviewSubmission(
                    review_item_id=item.id,
                    evaluation_id=item.evaluation_id,
                    snapshot_id=item.snapshot_id,
                    decision="pursue",
                    target_profile="neither",
                    primary_reason="company-quality",
                    block_company=True,
                    actor="owner",
                    created_at=now,
                ),
            )

        assert connection.execute("SELECT count(*) FROM review_events").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM application_events").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM company_policies").fetchone() == (0,)


def test_a_pursue_records_the_application_and_cooldowns_the_company(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    run_id = uuid4()
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        release = bootstrap_prompt_release(connection)
        _insert_prompt_run(connection, run_id, release.id, now)
        first = _insert_review_decision(connection, run_id, release.id, now, 1, "qualified")
        second = _insert_review_decision(connection, run_id, release.id, now, 2, "qualified")
        assert enqueue_qualified_review_item(connection, first, now.date())
        assert enqueue_qualified_review_item(connection, second, now.date())
        first_item, second_item = load_review_queue(connection).items

        saved = record_review(
            connection,
            ReviewSubmission(
                review_item_id=first_item.id,
                evaluation_id=first_item.evaluation_id,
                snapshot_id=first_item.snapshot_id,
                decision="pursue",
                note="Strong fit.",
                actor="owner",
                created_at=now,
            ),
        )

        assert isinstance(saved, ReviewSaved)
        application = connection.execute(
            """
            SELECT job_id, kind, source_review_event_id, actor, occurred_at
            FROM application_events
            """
        ).fetchone()
        assert application == (
            UUID(int=1),
            "applied",
            saved.review_event_id,
            "owner",
            now,
        )
        policy = connection.execute(
            """
            SELECT policy, effective_at, expires_at, source_review_event_id
            FROM company_policies
            """
        ).fetchone()
        assert policy == (
            "recent_application",
            now,
            now + timedelta(days=180),
            saved.review_event_id,
        )

        queue = load_review_queue(connection)
        assert queue.items == ()
        assert len(queue.reviewed_items) == 1

        resaved = record_review(
            connection,
            ReviewSubmission(
                review_item_id=second_item.id,
                evaluation_id=second_item.evaluation_id,
                snapshot_id=second_item.snapshot_id,
                decision="pursue",
                note="Even stronger fit.",
                actor="owner",
                created_at=now + timedelta(days=30),
            ),
        )

        assert isinstance(resaved, ReviewSaved)
        assert connection.execute("SELECT count(*) FROM application_events").fetchone() == (2,)
        policy = connection.execute(
            "SELECT policy, effective_at, expires_at FROM company_policies"
        ).fetchone()
        assert policy == (
            "recent_application",
            now + timedelta(days=30),
            now + timedelta(days=30) + timedelta(days=180),
        )


def test_a_blocked_company_is_not_downgraded_by_a_pursue(authority_schema: str) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    run_id = uuid4()
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        release = bootstrap_prompt_release(connection)
        _insert_prompt_run(connection, run_id, release.id, now)
        first = _insert_review_decision(connection, run_id, release.id, now, 1, "qualified")
        second = _insert_review_decision(connection, run_id, release.id, now, 2, "qualified")
        assert enqueue_qualified_review_item(connection, first, now.date())
        assert enqueue_qualified_review_item(connection, second, now.date())
        first_item, second_item = load_review_queue(connection).items

        blocked = record_review(
            connection,
            ReviewSubmission(
                review_item_id=first_item.id,
                evaluation_id=first_item.evaluation_id,
                snapshot_id=first_item.snapshot_id,
                decision="pursue",
                block_company=True,
                actor="owner",
                created_at=now,
            ),
        )
        pursued = record_review(
            connection,
            ReviewSubmission(
                review_item_id=second_item.id,
                evaluation_id=second_item.evaluation_id,
                snapshot_id=second_item.snapshot_id,
                decision="pursue",
                actor="owner",
                created_at=now + timedelta(minutes=5),
            ),
        )

        assert isinstance(blocked, ReviewSaved)
        assert isinstance(pursued, ReviewSaved)
        policy = connection.execute(
            "SELECT policy, effective_at, expires_at FROM company_policies"
        ).fetchone()
        assert policy == ("blocked", now, None)
        assert connection.execute("SELECT count(*) FROM application_events").fetchone() == (2,)


def test_a_snapshot_correction_replaces_the_broken_body_and_adds_compensation(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    run_id = uuid4()
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        release = bootstrap_prompt_release(connection)
        _insert_prompt_run(connection, run_id, release.id, now)
        _ = _insert_review_decision(connection, run_id, release.id, now, 1, "qualified")
        assert enqueue_qualified_review_item(connection, f"{1001:064x}", now.date())
        thin = load_review_queue(connection).items[0]
        assert len(thin.job.description) < 500
        _ = connection.execute(
            """
            INSERT INTO snapshot_corrections (
              snapshot_id, description, compensation_min, compensation_max,
              compensation_currency, compensation_period, compensation_source,
              reason, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                thin.snapshot_id,
                "## Your mission\n" + "Own features end to end. " * 40,
                Decimal("80000"),
                Decimal("100000"),
                "EUR",
                "year",
                "ats",
                "ats backfill",
                now,
            ),
        )

        queue = load_review_queue(connection)

    assert queue.items[0].job.description.startswith("## Your mission")
    compensation = queue.items[0].job.compensation
    assert compensation is not None
    assert compensation.minimum == Decimal("80000")
    assert compensation.maximum == Decimal("100000")
    assert compensation.currency == "EUR"
    assert compensation.source == "ats"


def test_curates_immutable_feedback_into_a_repeated_trial_manifest(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    run_id = uuid4()
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        release = bootstrap_prompt_release(connection)
        _insert_prompt_run(connection, run_id, release.id, now)
        qualified_decision = _insert_review_decision(
            connection, run_id, release.id, now, 21, "qualified"
        )
        _insert_review_decision(connection, run_id, release.id, now, 22, "rejected")
        assert enqueue_qualified_review_item(connection, qualified_decision, now.date())
        assert enqueue_rejected_audit_sample(connection, now.date()) == 1
        qualified, rejected = load_review_queue(connection).items[:2]
        rejected_feedback = record_review(
            connection,
            ReviewSubmission(
                review_item_id=qualified.id,
                evaluation_id=qualified.evaluation_id,
                snapshot_id=qualified.snapshot_id,
                decision="reject",
                target_profile="neither",
                primary_reason="role-scope",
                actor="owner",
                created_at=now,
            ),
        )
        qualified_feedback = record_review(
            connection,
            ReviewSubmission(
                review_item_id=rejected.id,
                evaluation_id=rejected.evaluation_id,
                snapshot_id=rejected.snapshot_id,
                decision="pursue",
                target_profile="applied-ai-product-engineer",
                primary_reason="technology-fit",
                actor="owner",
                created_at=now,
            ),
        )
        assert isinstance(rejected_feedback, ReviewSaved)
        assert isinstance(qualified_feedback, ReviewSaved)
        uncurated = list_review_feedback(connection, curation="uncurated")
        assert {item.review_event_id for item in uncurated.items} == {
            rejected_feedback.review_event_id,
            qualified_feedback.review_event_id,
        }

        curation_start = Barrier(2)

        def include_negative(_attempt: int) -> UUID:
            with _connection(authority_schema) as concurrent_connection:
                _ = curation_start.wait()
                return include_review_event(
                    concurrent_connection,
                    review_event_id=rejected_feedback.review_event_id,
                    critical=True,
                    reason="False positives are costly.",
                    actor="owner",
                    created_at=now,
                    idempotency_key="curate:negative",
                ).id

        with ThreadPoolExecutor(max_workers=2) as executor:
            curation_ids = tuple(executor.map(include_negative, range(2)))
        assert curation_ids[0] == curation_ids[1]
        include_review_event(
            connection,
            review_event_id=qualified_feedback.review_event_id,
            critical=False,
            reason="Known positive control.",
            actor="owner",
            created_at=now,
            idempotency_key="curate:positive",
        )
        connection.execute(
            """
            INSERT INTO snapshot_corrections (
              snapshot_id, description, reason, created_at
            ) VALUES (%s, %s, %s, %s)
            """,
            (qualified.snapshot_id, "Corrected job description.", "ATS repair", now),
        )

        preview = preview_manifest(connection, ManifestPolicy())
        assert preview.id == "0" * 64
        assert preview.case_count == 2
        assert preview.critical_count == 1
        assert preview.trial_count == 4

        first = create_manifest(
            connection,
            policy=ManifestPolicy(),
            created_at=now,
            created_by="owner",
            idempotency_key="manifest:first",
        )
        assert (
            next(
                case
                for case in first.cases
                if case.review_event_id == rejected_feedback.review_event_id
            ).input.description
            == "Corrected job description."
        )
        revised_feedback = record_review(
            connection,
            ReviewSubmission(
                review_item_id=qualified.id,
                evaluation_id=qualified.evaluation_id,
                snapshot_id=qualified.snapshot_id,
                decision="pursue",
                target_profile="applied-ai-product-engineer",
                primary_reason="technology-fit",
                actor="owner",
                created_at=now + timedelta(seconds=1),
            ),
        )
        assert isinstance(revised_feedback, ReviewSaved)
        include_review_event(
            connection,
            review_event_id=revised_feedback.review_event_id,
            critical=False,
            reason="Revised positive control.",
            actor="owner",
            created_at=now + timedelta(seconds=1),
            idempotency_key="curate:revised",
        )
        retried_first = create_manifest(
            connection,
            policy=ManifestPolicy(),
            created_at=now + timedelta(seconds=1),
            created_by="owner",
            idempotency_key="manifest:first",
        )
        assert retried_first == first
        revised_manifest = create_manifest(
            connection,
            policy=ManifestPolicy(),
            created_at=now + timedelta(seconds=1),
            created_by="owner",
            idempotency_key="manifest:revised",
        )
        assert rejected_feedback.review_event_id not in {
            case.review_event_id for case in revised_manifest.cases
        }
        exclude_review_event(
            connection,
            review_event_id=qualified_feedback.review_event_id,
            reason="Temporarily disputed.",
            actor="owner",
            created_at=now + timedelta(seconds=2),
            idempotency_key="exclude:positive",
        )
        second = create_manifest(
            connection,
            policy=ManifestPolicy(),
            created_at=now + timedelta(seconds=2),
            created_by="owner",
            idempotency_key="manifest:second",
        )

        assert len(first.cases) == 2
        assert sorted(case.trial_count for case in first.cases) == [1, 3]
        assert len(second.cases) == 1
        manifests = list_manifests(connection)
        assert tuple(item.id for item in manifests.items) == (
            second.id,
            revised_manifest.id,
            first.id,
        )
        assert manifests.items[0].case_count == 1
        excluded = list_review_feedback(connection, curation="excluded")
        assert tuple(item.review_event_id for item in excluded.items) == (
            qualified_feedback.review_event_id,
        )
        frozen_feedback = load_review_feedback(connection, qualified_feedback.review_event_id)
        assert frozen_feedback.frozen_manifest_count == 2
        assert frozen_feedback.curation is not None
        assert frozen_feedback.curation.action == "exclude"
        projection_status = load_projection_queue_status(connection)
        assert projection_status.pending_count >= 3
        assert projection_status.failed_count == 0
        assert connection.execute("SELECT count(*) FROM review_events").fetchone() == (3,)
        assert connection.execute(
            "SELECT count(*) FROM langfuse_projection_items WHERE kind = 'evaluation_manifest'"
        ).fetchone() == (3,)
        include_review_event(
            connection,
            review_event_id=qualified_feedback.review_event_id,
            critical=False,
            reason="Dispute resolved.",
            actor="owner",
            created_at=now + timedelta(seconds=3),
            idempotency_key="reinclude:positive",
        )
        connection.execute(
            """
            CREATE FUNCTION reject_manifest_projection_for_contract() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'projection unavailable'; END; $$
            """
        )
        connection.execute(
            """
            CREATE TRIGGER reject_manifest_projection_for_contract
            BEFORE INSERT ON langfuse_projection_items
            FOR EACH ROW WHEN (NEW.kind = 'evaluation_manifest')
            EXECUTE FUNCTION reject_manifest_projection_for_contract()
            """
        )
        with pytest.raises(psycopg.Error, match="projection unavailable"):
            create_manifest(
                connection,
                policy=ManifestPolicy(),
                created_at=now + timedelta(seconds=3),
                created_by="owner",
                idempotency_key="manifest:third",
            )
        assert connection.execute("SELECT count(*) FROM evaluation_manifests").fetchone() == (3,)
        assert connection.execute("SELECT count(*) FROM evaluation_manifest_cases").fetchone() == (
            5,
        )
        with pytest.raises(psycopg.errors.CheckViolation, match="immutable"):
            connection.execute(
                "UPDATE evaluation_manifests SET created_by = 'other' WHERE id = %s",
                (first.id,),
            )


def test_concurrent_manifest_execution_calls_provider_once(authority_schema: str) -> None:
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    with _connection(authority_schema) as connection:
        manifest_id, target, rates = _seed_evaluation_execution_context(connection, now)
    command = EvaluateManifestCommand(
        idempotency_key="evaluation:concurrent",
        manifest_id=manifest_id,
        target=target,
        implementation_ref="concurrency-test",
    )
    evaluator_entered = Barrier(2)
    second_connected = Event()
    second_backend_pid: list[int] = []
    allow_first_to_finish = Event()
    factory_calls: list[int] = []
    evaluator_calls: list[int] = []

    def execute(worker: int) -> object:
        with _connection(authority_schema) as connection:
            if worker == 2:
                second_backend_pid.append(connection.info.backend_pid)
                second_connected.set()

            def create_evaluator(
                stored_rates: ExchangeRateSnapshot,
                _record: Callable[[ProviderRequestObservation], None],
            ) -> Callable[[EvaluationManifestCase, ReleaseTarget, int], EvaluationResult]:
                factory_calls.append(worker)
                assert stored_rates == rates

                def evaluate(
                    _case: EvaluationManifestCase,
                    case_target: ReleaseTarget,
                    trial: int,
                ) -> EvaluationResult:
                    evaluator_calls.append(worker)
                    assert case_target == target
                    assert trial == 0
                    if len(evaluator_calls) == 1:
                        evaluator_entered.wait(timeout=5)
                        assert allow_first_to_finish.wait(timeout=5)
                    return Qualified(reason="Expected positive.", profile_name="profile")

                return evaluate

            return run_manifest(
                connection,
                command=command,
                create_exchange_rates=lambda: rates,
                create_evaluator=create_evaluator,
                now=lambda: now,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(execute, 1)
        evaluator_entered.wait(timeout=5)
        second = executor.submit(execute, 2)
        assert second_connected.wait(timeout=5)
        deadline = time.monotonic() + 5
        with _connection(authority_schema) as observer:
            while True:
                wait_state = observer.execute(
                    "SELECT wait_event_type, wait_event FROM pg_stat_activity WHERE pid = %s",
                    (second_backend_pid[0],),
                ).fetchone()
                if wait_state == ("Lock", "advisory"):
                    break
                if time.monotonic() >= deadline:
                    pytest.fail("second evaluator did not contend on the advisory lock")
                time.sleep(0.01)
        allow_first_to_finish.set()
        first_execution = first.result(timeout=5)
        second_execution = second.result(timeout=5)

    assert isinstance(first_execution, CompletedEvaluationExecution)
    assert second_execution == first_execution
    assert factory_calls == [1]
    assert evaluator_calls == [1]


def test_same_connection_manifest_execution_is_rejected_before_database_work(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    with _connection(authority_schema) as connection:
        manifest_id, target, rates = _seed_evaluation_execution_context(connection, now)
        command = EvaluateManifestCommand(
            idempotency_key="evaluation:same-connection",
            manifest_id=manifest_id,
            target=target,
            implementation_ref="same-connection-test",
        )
        evaluator_entered = Event()
        allow_first_to_finish = Event()

        def create_evaluator(
            _rates: ExchangeRateSnapshot,
            _record: Callable[[ProviderRequestObservation], None],
        ) -> Callable[[EvaluationManifestCase, ReleaseTarget, int], EvaluationResult]:
            def evaluate(
                _case: EvaluationManifestCase,
                _target: ReleaseTarget,
                _trial: int,
            ) -> EvaluationResult:
                evaluator_entered.set()
                assert allow_first_to_finish.wait(timeout=5)
                return Qualified(reason="Expected positive.", profile_name="profile")

            return evaluate

        with ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(
                run_manifest,
                connection,
                command=command,
                create_exchange_rates=lambda: rates,
                create_evaluator=create_evaluator,
                now=lambda: now,
            )
            assert evaluator_entered.wait(timeout=5)
            side_effects: list[str] = []
            try:
                with pytest.raises(RuntimeError, match="already active on this connection"):
                    run_manifest(
                        connection,
                        command=command,
                        create_exchange_rates=lambda: side_effects.append("rates") or rates,
                        create_evaluator=lambda _rates, _record: side_effects.append("evaluator")
                        or (lambda _case, _target, _trial: Rejected(reason="unexpected")),
                        now=lambda: now,
                    )
            finally:
                allow_first_to_finish.set()
            assert isinstance(first.result(timeout=5), CompletedEvaluationExecution)
        assert side_effects == []


def test_terminal_execution_retries_and_mismatches_have_no_side_effects(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    with _connection(authority_schema) as connection:
        manifest_id, target, rates = _seed_evaluation_execution_context(connection, now)
        command = EvaluateManifestCommand(
            idempotency_key="evaluation:terminal",
            manifest_id=manifest_id,
            target=target,
            implementation_ref="terminal-test",
        )
        completed = run_manifest(
            connection,
            command=command,
            create_exchange_rates=lambda: rates,
            create_evaluator=lambda _rates, record: (
                lambda _case, _target, _trial: Qualified(
                    reason="Expected positive.", profile_name="profile"
                )
            ),
            now=lambda: now,
        )
        side_effects: list[str] = []
        repeated = run_manifest(
            connection,
            command=command,
            create_exchange_rates=lambda: side_effects.append("rates") or rates,
            create_evaluator=lambda _rates, _record: side_effects.append("evaluator")
            or (lambda _case, _target, _trial: Rejected(reason="unexpected")),
            now=lambda: now,
        )

        assert repeated == completed
        assert side_effects == []
        with pytest.raises(ValueError, match="different evaluation execution"):
            run_manifest(
                connection,
                command=command.model_copy(update={"implementation_ref": "different"}),
                create_exchange_rates=lambda: side_effects.append("rates") or rates,
                create_evaluator=lambda _rates, _record: side_effects.append("evaluator")
                or (lambda _case, _target, _trial: Rejected(reason="unexpected")),
                now=lambda: now,
            )
        assert side_effects == []


def test_interrupted_and_exceptional_executions_fail_without_replay(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    with _connection(authority_schema) as connection:
        manifest_id, target, rates = _seed_evaluation_execution_context(connection, now)
        interrupted_command = EvaluateManifestCommand(
            idempotency_key="evaluation:interrupted",
            manifest_id=manifest_id,
            target=target,
            implementation_ref="interrupted-test",
        )
        interrupted_id = hashlib.sha256(
            f"evaluation_execution:{interrupted_command.idempotency_key}".encode()
        ).hexdigest()
        connection.execute(
            """
            INSERT INTO evaluation_run_executions (
              id, idempotency_key, manifest_id, prompt_release_id,
              relevance_release_id, implementation_ref, state,
              exchange_rate_snapshot, exchange_rate_digest, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, 'running', %s, %s, %s)
            """,
            (
                interrupted_id,
                interrupted_command.idempotency_key,
                manifest_id,
                target.prompt_release_id,
                target.relevance_release_id,
                interrupted_command.implementation_ref,
                Jsonb(rates.model_dump(mode="json")),
                exchange_rate_snapshot_digest(rates),
                now,
            ),
        )
        side_effects: list[str] = []
        interrupted = run_manifest(
            connection,
            command=interrupted_command,
            create_exchange_rates=lambda: side_effects.append("rates") or rates,
            create_evaluator=lambda _rates, _record: side_effects.append("evaluator")
            or (lambda _case, _target, _trial: Rejected(reason="unexpected")),
            now=lambda: now,
        )
        assert isinstance(interrupted, FailedEvaluationExecution)
        assert interrupted.failure.code == "interrupted_execution"
        assert interrupted.telemetry is None
        assert side_effects == []

        exception_command = interrupted_command.model_copy(
            update={"idempotency_key": "evaluation:exception"}
        )

        def exceptional_factory(
            _rates: ExchangeRateSnapshot,
            record: Callable[[ProviderRequestObservation], None],
        ) -> Callable[[EvaluationManifestCase, ReleaseTarget, int], EvaluationResult]:
            def evaluate(
                _case: EvaluationManifestCase,
                _target: ReleaseTarget,
                _trial: int,
            ) -> EvaluationResult:
                record(
                    ProviderRequestObservation(
                        input_tokens=7,
                        output_tokens=2,
                        cost_usd=Decimal("0.03"),
                        latency_ms=50,
                    )
                )
                raise RuntimeError("provider response processing failed")

            return evaluate

        failed = run_manifest(
            connection,
            command=exception_command,
            create_exchange_rates=lambda: rates,
            create_evaluator=exceptional_factory,
            now=lambda: now,
        )
        assert isinstance(failed, FailedEvaluationExecution)
        assert failed.failure.code == "unexpected_exception"
        assert failed.failure.error_type == "RuntimeError"
        assert failed.telemetry is not None
        assert failed.telemetry.request_count == 1
        assert failed.telemetry.input_tokens == 7
        assert "provider response processing failed" not in failed.failure.message

        retry_side_effects: list[str] = []
        assert (
            run_manifest(
                connection,
                command=exception_command,
                create_exchange_rates=lambda: retry_side_effects.append("rates") or rates,
                create_evaluator=lambda _rates, _record: retry_side_effects.append("evaluator")
                or (lambda _case, _target, _trial: Rejected(reason="unexpected")),
                now=lambda: now,
            )
            == failed
        )
        assert retry_side_effects == []


def test_evaluation_execution_sql_rejects_invalid_digest_and_transitions(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    with _connection(authority_schema) as connection:
        manifest_id, target, rates = _seed_evaluation_execution_context(connection, now)
        values = (
            "d" * 64,
            "evaluation:invalid-sql",
            manifest_id,
            target.prompt_release_id,
            target.relevance_release_id,
            "sql-test",
            Jsonb(rates.model_dump(mode="json")),
            "0" * 64,
            now,
        )
        with pytest.raises(
            psycopg.errors.CheckViolation,
            match="evaluation_execution_rate_digest_matches",
        ):
            connection.execute(
                """
                INSERT INTO evaluation_run_executions (
                  id, idempotency_key, manifest_id, prompt_release_id,
                  relevance_release_id, implementation_ref, state,
                  exchange_rate_snapshot, exchange_rate_digest, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, 'running', %s, %s, %s)
                """,
                values,
            )

        valid_values = (*values[:-2], exchange_rate_snapshot_digest(rates), now)
        connection.execute(
            """
            INSERT INTO evaluation_run_executions (
              id, idempotency_key, manifest_id, prompt_release_id,
              relevance_release_id, implementation_ref, state,
              exchange_rate_snapshot, exchange_rate_digest, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, 'running', %s, %s, %s)
            """,
            valid_values,
        )
        with pytest.raises(psycopg.errors.IntegrityConstraintViolation, match="must become"):
            connection.execute(
                "UPDATE evaluation_run_executions SET created_at = created_at WHERE id = %s",
                (values[0],),
            )
        with pytest.raises(
            psycopg.errors.CheckViolation,
            match="evaluation_execution_latency_percentiles_ordered",
        ):
            connection.execute(
                """
                UPDATE evaluation_run_executions
                SET state = 'failed', request_count = 1, input_tokens = 0,
                    output_tokens = 0, cost_usd = 0, usage_complete = TRUE,
                    p50_latency_ms = 2, p95_latency_ms = 1,
                    failure = '{"code":"failed","message":"failed"}', terminal_at = %s
                WHERE id = %s
                """,
                (now, values[0]),
            )
        with pytest.raises(
            psycopg.errors.CheckViolation,
            match="evaluation_execution_terminal_time_ordered",
        ):
            connection.execute(
                """
                UPDATE evaluation_run_executions
                SET state = 'failed', request_count = 0, input_tokens = 0,
                    output_tokens = 0, cost_usd = 0, usage_complete = TRUE,
                    failure = '{"code":"failed","message":"failed"}', terminal_at = %s
                WHERE id = %s
                """,
                (now - timedelta(seconds=1), values[0]),
            )


def test_evaluation_execution_sql_enforces_insert_linkage_and_json_shapes(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    with _connection(authority_schema) as connection:
        manifest_id, target, rates = _seed_evaluation_execution_context(connection, now)
        command = EvaluateManifestCommand(
            idempotency_key="evaluation:link-source",
            manifest_id=manifest_id,
            target=target,
            implementation_ref="link-source-ref",
        )
        completed = run_manifest(
            connection,
            command=command,
            create_exchange_rates=lambda: rates,
            create_evaluator=lambda _rates, _record: (
                lambda _case, _target, _trial: Qualified(
                    reason="Expected positive.", profile_name="profile"
                )
            ),
            now=lambda: now,
        )
        assert isinstance(completed, CompletedEvaluationExecution)
        unlinked_run_id = "9" * 64
        with connection.transaction():
            connection.execute(
                """
                INSERT INTO evaluation_runs (
                  id, idempotency_key, manifest_id, prompt_release_id,
                  relevance_release_id, expected_result_count, result_count,
                  false_positive_count, false_negative_count,
                  operational_failure_count, critical_false_positive_count,
                  false_positive_rate, false_negative_rate, implementation_ref,
                  completed_at
                )
                SELECT %s, 'evaluation:unlinked-run', manifest_id, prompt_release_id,
                       relevance_release_id, expected_result_count, result_count,
                       false_positive_count, false_negative_count,
                       operational_failure_count, critical_false_positive_count,
                       false_positive_rate, false_negative_rate, 'unlinked-ref', completed_at
                FROM evaluation_runs WHERE id = %s
                """,
                (unlinked_run_id, completed.run.id),
            )
            connection.execute(
                """
                INSERT INTO evaluation_case_results (
                  id, run_id, manifest_id, prompt_release_id,
                  relevance_release_id, case_position, trial_index,
                  expected_outcome, actual_outcome, failure_kind, reason
                )
                SELECT encode(sha256(convert_to(%s || ':' || id, 'UTF8')), 'hex'),
                       %s, manifest_id, prompt_release_id, relevance_release_id,
                       case_position, trial_index, expected_outcome, actual_outcome,
                       failure_kind, reason
                FROM evaluation_case_results WHERE run_id = %s
                """,
                (unlinked_run_id, unlinked_run_id, completed.run.id),
            )

        running_values = (
            "e" * 64,
            "evaluation:forged-link",
            manifest_id,
            target.prompt_release_id,
            target.relevance_release_id,
            "forged-ref",
            Jsonb(rates.model_dump(mode="json")),
            exchange_rate_snapshot_digest(rates),
            now,
        )
        connection.execute(
            """
            INSERT INTO evaluation_run_executions (
              id, idempotency_key, manifest_id, prompt_release_id,
              relevance_release_id, implementation_ref, state,
              exchange_rate_snapshot, exchange_rate_digest, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, 'running', %s, %s, %s)
            """,
            running_values,
        )
        with pytest.raises(
            psycopg.errors.ForeignKeyViolation,
            match="evaluation_execution_exact_completed_run",
        ):
            connection.execute(
                """
                UPDATE evaluation_run_executions
                SET state = 'completed', request_count = 0, input_tokens = 0,
                    output_tokens = 0, cost_usd = 0, usage_complete = TRUE,
                    run_id = %s, terminal_at = %s
                WHERE id = %s
                """,
                (unlinked_run_id, now, running_values[0]),
            )

        for index, state in enumerate(("completed", "failed")):
            with pytest.raises(
                psycopg.errors.IntegrityConstraintViolation,
                match="must start running",
            ):
                connection.execute(
                    """
                    INSERT INTO evaluation_run_executions (
                      id, idempotency_key, manifest_id, prompt_release_id,
                      relevance_release_id, implementation_ref, state, created_at
                    ) VALUES (%s, %s, %s, %s, %s, 'direct-ref', %s, %s)
                    """,
                    (
                        f"{index + 30:064x}",
                        f"evaluation:direct-{state}",
                        manifest_id,
                        target.prompt_release_id,
                        target.relevance_release_id,
                        state,
                        now,
                    ),
                )

        invalid_snapshots: tuple[dict[str, object], ...] = (
            {"rates": [], "source": "fallback", "observed_at": now.isoformat()},
            {"rates": {"EUR": True}, "source": "fallback", "observed_at": now.isoformat()},
            {"rates": {"EUR": "1.1"}, "source": "other", "observed_at": now.isoformat()},
            {"rates": {"EUR": "1.1"}, "source": "fallback", "observed_at": "nope"},
            {
                "rates": {"EUR": "1.1"},
                "source": "fallback",
                "observed_at": now.isoformat(),
                "extra": True,
            },
        )
        for index, snapshot in enumerate(invalid_snapshots):
            with pytest.raises(
                psycopg.errors.CheckViolation,
                match="evaluation_execution_rate_snapshot_shape",
            ):
                connection.execute(
                    """
                    INSERT INTO evaluation_run_executions (
                      id, idempotency_key, manifest_id, prompt_release_id,
                      relevance_release_id, implementation_ref, state,
                      exchange_rate_snapshot, exchange_rate_digest, created_at
                    ) VALUES (%s, %s, %s, %s, %s, 'shape-test', 'running', %s,
                      encode(sha256(convert_to(canonical_job_finder_json(%s), 'UTF8')), 'hex'), %s)
                    """,
                    (
                        f"{index + 1:064x}",
                        f"evaluation:invalid-shape:{index}",
                        manifest_id,
                        target.prompt_release_id,
                        target.relevance_release_id,
                        Jsonb(snapshot),
                        Jsonb(snapshot),
                        now,
                    ),
                )

        invalid_failures: tuple[dict[str, object], ...] = (
            {},
            {"code": "", "message": "message"},
            {"code": "code", "message": ""},
            {"code": "code", "message": "message", "error_type": 1},
            {"code": "code", "message": "message", "extra": True},
        )
        for index, failure in enumerate(invalid_failures):
            execution_id = f"{index + 10:064x}"
            idempotency_key = f"evaluation:invalid-failure:{index}"
            connection.execute(
                """
                INSERT INTO evaluation_run_executions (
                  id, idempotency_key, manifest_id, prompt_release_id,
                  relevance_release_id, implementation_ref, state,
                  exchange_rate_snapshot, exchange_rate_digest, created_at
                ) VALUES (%s, %s, %s, %s, %s, 'failure-test', 'running', %s, %s, %s)
                """,
                (
                    execution_id,
                    idempotency_key,
                    manifest_id,
                    target.prompt_release_id,
                    target.relevance_release_id,
                    Jsonb(rates.model_dump(mode="json")),
                    exchange_rate_snapshot_digest(rates),
                    now,
                ),
            )
            with pytest.raises(
                psycopg.errors.CheckViolation,
                match="evaluation_execution_failure_shape",
            ):
                connection.execute(
                    """
                    UPDATE evaluation_run_executions
                    SET state = 'failed', request_count = 0, input_tokens = 0,
                        output_tokens = 0, cost_usd = 0, usage_complete = TRUE,
                        failure = %s, terminal_at = %s
                    WHERE id = %s
                    """,
                    (Jsonb(failure), now, execution_id),
                )


def test_exchange_rate_snapshot_digest_matches_postgres_for_unicode_keys(
    authority_schema: str,
) -> None:
    snapshot = ExchangeRateSnapshot(
        rates={"EURO-€": Decimal("1.10")},
        source="fallback",
        observed_at=datetime(2026, 9, 21, 12, 0, tzinfo=UTC),
    )
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        row = connection.execute(
            """
            SELECT encode(
              sha256(convert_to(canonical_job_finder_json(%s), 'UTF8')),
              'hex'
            )
            """,
            (Jsonb(snapshot.model_dump(mode="json")),),
        ).fetchone()
    assert row == (exchange_rate_snapshot_digest(snapshot),)


def test_runs_trials_rejects_operational_failures_and_retries_projection(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    run_id = uuid4()
    with _connection(authority_schema) as connection:
        _apply_migrations_through(connection, "0021_typesafe_model_provider.sql")
        baseline_release = bootstrap_prompt_release(connection)
        _insert_prompt_run(connection, run_id, baseline_release.id, now)
        qualified_decision = _insert_review_decision(
            connection, run_id, baseline_release.id, now, 31, "qualified"
        )
        _insert_review_decision(connection, run_id, baseline_release.id, now, 32, "rejected")
        assert enqueue_qualified_review_item(connection, qualified_decision, now.date())
        assert enqueue_rejected_audit_sample(connection, now.date()) == 1
        qualified, rejected = load_review_queue(connection).items[:2]
        negative_feedback = record_review(
            connection,
            ReviewSubmission(
                review_item_id=qualified.id,
                evaluation_id=qualified.evaluation_id,
                snapshot_id=qualified.snapshot_id,
                decision="reject",
                target_profile="neither",
                primary_reason="role-scope",
                actor="owner",
                created_at=now,
            ),
        )
        positive_feedback = record_review(
            connection,
            ReviewSubmission(
                review_item_id=rejected.id,
                evaluation_id=rejected.evaluation_id,
                snapshot_id=rejected.snapshot_id,
                decision="pursue",
                target_profile="applied-ai-product-engineer",
                primary_reason="technology-fit",
                actor="owner",
                created_at=now,
            ),
        )
        assert isinstance(negative_feedback, ReviewSaved)
        assert isinstance(positive_feedback, ReviewSaved)
        include_review_event(
            connection,
            review_event_id=negative_feedback.review_event_id,
            critical=True,
            reason="Critical negative control.",
            actor="owner",
            created_at=now,
            idempotency_key="run:negative",
        )
        include_review_event(
            connection,
            review_event_id=positive_feedback.review_event_id,
            critical=False,
            reason="Positive control.",
            actor="owner",
            created_at=now,
            idempotency_key="run:positive",
        )
        manifest = create_manifest(
            connection,
            policy=ManifestPolicy(),
            created_at=now,
            created_by="owner",
            idempotency_key="manifest:run",
        )
        historical_run_id = "f" * 64
        historical_result_count = sum(case.trial_count for case in manifest.cases)
        with connection.transaction():
            connection.execute(
                """
                INSERT INTO evaluation_runs (
                  id, idempotency_key, manifest_id, prompt_release_id,
                  expected_result_count, result_count, false_positive_count,
                  false_negative_count, operational_failure_count,
                  critical_false_positive_count, false_positive_rate,
                  false_negative_rate, implementation_ref, completed_at
                ) VALUES (%s, 'evaluation:historical', %s, %s, %s, %s, 0, 0, 0, 0, 0, 0,
                          'historical-ref', %s)
                """,
                (
                    historical_run_id,
                    manifest.id,
                    baseline_release.id,
                    historical_result_count,
                    historical_result_count,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO evaluation_case_results (
                  id, run_id, manifest_id, prompt_release_id, case_position,
                  trial_index, expected_outcome, actual_outcome, failure_kind, reason
                )
                SELECT encode(sha256(convert_to(
                         %s::TEXT || ':' || c.position || ':' || trial.index, 'UTF8'
                       )), 'hex'),
                       %s, c.manifest_id, %s, c.position, trial.index,
                       c.expected_outcome, c.expected_outcome, NULL, 'Historical result.'
                FROM evaluation_manifest_cases c
                CROSS JOIN LATERAL generate_series(0, c.trial_count - 1) AS trial(index)
                WHERE c.manifest_id = %s
                """,
                (
                    historical_run_id,
                    historical_run_id,
                    baseline_release.id,
                    manifest.id,
                ),
            )

        apply_migrations(connection)
        historical = load_run(connection, historical_run_id)
        assert historical.target is None
        historical_execution = load_evaluation_execution_by_key(connection, "evaluation:historical")
        assert isinstance(historical_execution, CompletedEvaluationExecution)
        assert historical_execution.state == "completed"
        assert historical_execution.exchange_rates is None
        assert historical_execution.telemetry is None
        assert historical_execution.run == historical
        assert connection.execute(
            "SELECT bool_and(relevance_release_id IS NULL) FROM evaluation_case_results WHERE run_id = %s",
            (historical_run_id,),
        ).fetchone() == (True,)
        with pytest.raises(
            psycopg.errors.CheckViolation,
            match="evaluation_runs_require_relevance_release",
        ):
            connection.execute(
                """
                INSERT INTO evaluation_runs (
                  id, idempotency_key, manifest_id, prompt_release_id,
                  relevance_release_id, expected_result_count, result_count,
                  false_positive_count, false_negative_count,
                  operational_failure_count, critical_false_positive_count,
                  false_positive_rate, false_negative_rate, implementation_ref,
                  completed_at
                )
                SELECT %s, 'evaluation:new-null', manifest_id, prompt_release_id,
                       NULL, expected_result_count, result_count,
                       false_positive_count, false_negative_count,
                       operational_failure_count, critical_false_positive_count,
                       false_positive_rate, false_negative_rate, implementation_ref,
                       completed_at
                FROM evaluation_runs WHERE id = %s
                """,
                ("0" * 64, historical_run_id),
            )
        with pytest.raises(
            psycopg.errors.CheckViolation,
            match="evaluation_case_results_require_relevance_release",
        ):
            connection.execute(
                """
                INSERT INTO evaluation_case_results (
                  id, run_id, manifest_id, prompt_release_id,
                  relevance_release_id, case_position, trial_index,
                  expected_outcome, actual_outcome, failure_kind, reason
                )
                SELECT %s, run_id, manifest_id, prompt_release_id, NULL,
                       case_position, trial_index + 100, expected_outcome,
                       actual_outcome, failure_kind, reason
                FROM evaluation_case_results WHERE run_id = %s LIMIT 1
                """,
                ("1" * 64, historical_run_id),
            )

        relevance_release = store_relevance_release(
            connection,
            build_relevance_release(build_gemini_policy(baseline_release)),
            created_at=now,
            created_by="contract",
        )
        faithful_release = store_relevance_release(
            connection,
            build_relevance_release(build_jev_faithful_policy(baseline_release)),
            created_at=now,
            created_by="contract",
        )
        baseline_target = ReleaseTarget(
            prompt_release_id=baseline_release.id,
            relevance_release_id=relevance_release.id,
        )
        candidate_target = ReleaseTarget(
            prompt_release_id=baseline_release.id,
            relevance_release_id=faithful_release.id,
        )
        baseline_calls = 0

        def baseline_evaluator(
            case: EvaluationManifestCase, target: ReleaseTarget, trial: int
        ) -> EvaluationResult:
            nonlocal baseline_calls
            baseline_calls += 1
            assert target == baseline_target
            assert trial >= 0
            if case.expected_outcome == "qualified":
                return Qualified(reason="Expected positive.", profile_name="profile")
            return Rejected(reason="Expected negative.")

        rates = ExchangeRateSnapshot(
            rates={"EUR": Decimal("1.10")}, source="fallback", observed_at=now
        )
        baseline_execution = run_manifest(
            connection,
            command=EvaluateManifestCommand(
                manifest_id=manifest.id,
                target=baseline_target,
                implementation_ref="baseline-ref",
                idempotency_key="evaluation:baseline",
            ),
            create_exchange_rates=lambda: rates,
            create_evaluator=lambda _rates, _record: baseline_evaluator,
            now=lambda: now,
        )
        repeated = run_manifest(
            connection,
            command=EvaluateManifestCommand(
                manifest_id=manifest.id,
                target=baseline_target,
                implementation_ref="baseline-ref",
                idempotency_key="evaluation:baseline",
            ),
            create_exchange_rates=lambda: rates,
            create_evaluator=lambda _rates, _record: baseline_evaluator,
            now=lambda: now,
        )
        assert isinstance(baseline_execution, CompletedEvaluationExecution)
        baseline = baseline_execution.run
        assert repeated == baseline_execution
        assert baseline.target == baseline_target
        assert baseline_calls == 4
        assert connection.execute(
            """
            SELECT r.relevance_release_id,
                   bool_and(c.relevance_release_id = r.relevance_release_id)
            FROM evaluation_runs r
            JOIN evaluation_case_results c ON c.run_id = r.id
            WHERE r.id = %s
            GROUP BY r.relevance_release_id
            """,
            (baseline.id,),
        ).fetchone() == (relevance_release.id, True)
        with pytest.raises(
            psycopg.errors.ForeignKeyViolation,
            match="evaluation_case_results_exact_release_target",
        ):
            connection.execute(
                """
                INSERT INTO evaluation_case_results (
                  id, run_id, manifest_id, prompt_release_id,
                  relevance_release_id, case_position, trial_index,
                  expected_outcome, actual_outcome, failure_kind, reason
                )
                SELECT %s, run_id, manifest_id, prompt_release_id, %s,
                       case_position, trial_index + 100, expected_outcome,
                       actual_outcome, failure_kind, reason
                FROM evaluation_case_results WHERE run_id = %s LIMIT 1
                """,
                ("2" * 64, faithful_release.id, baseline.id),
            )

        with pytest.raises(ValueError, match="different evaluation execution"):
            run_manifest(
                connection,
                command=EvaluateManifestCommand(
                    manifest_id=manifest.id,
                    target=ReleaseTarget(
                        prompt_release_id=baseline_release.id,
                        relevance_release_id=faithful_release.id,
                    ),
                    implementation_ref="baseline-ref",
                    idempotency_key="evaluation:baseline",
                ),
                create_exchange_rates=lambda: rates,
                create_evaluator=lambda _rates, _record: baseline_evaluator,
                now=lambda: now,
            )

        def candidate_evaluator(
            case: EvaluationManifestCase, target: ReleaseTarget, trial: int
        ) -> EvaluationResult:
            assert target == candidate_target
            if case.expected_outcome == "qualified":
                return RetryableOperationalError(
                    prompt_name="profile",
                    error_code="timeout",
                    reason="Provider timed out.",
                )
            if trial == 0:
                return Qualified(reason="Incorrect pass.", profile_name="profile")
            return Rejected(reason="Expected negative.")

        candidate_execution = run_manifest(
            connection,
            command=EvaluateManifestCommand(
                manifest_id=manifest.id,
                target=candidate_target,
                implementation_ref="candidate-ref",
                idempotency_key="evaluation:candidate",
            ),
            create_exchange_rates=lambda: rates,
            create_evaluator=lambda _rates, _record: candidate_evaluator,
            now=lambda: now,
        )
        assert isinstance(candidate_execution, CompletedEvaluationExecution)
        candidate = candidate_execution.run
        comparison = preview_run_comparison(connection, baseline.id, candidate.id)
        promotion = record_prompt_promotion_decision(
            connection,
            baseline_run_id=baseline.id,
            candidate_run_id=candidate.id,
            expected_comparison_id=comparison.id,
            decision="rejected",
            reason="Operational and critical regressions require rejection.",
            actor="owner",
            created_at=now,
            idempotency_key="promotion:candidate",
        )

        assert candidate.metrics.false_positive_count == 1
        assert candidate.metrics.false_negative_count == 0
        assert candidate.metrics.operational_failure_count == 1
        assert candidate.metrics.critical_false_positive_count == 1
        assert not comparison.eligible
        assert comparison.regression_count == 2
        assert promotion.decision == "rejected"
        assert promotion.baseline_target == baseline_target
        assert promotion.candidate_target == candidate_target
        assert promotion.comparison_id == comparison.id
        assert (
            record_prompt_promotion_decision(
                connection,
                baseline_run_id=baseline.id,
                candidate_run_id=candidate.id,
                expected_comparison_id=comparison.id,
                decision="rejected",
                reason="Operational and critical regressions require rejection.",
                actor="owner",
                created_at=now,
                idempotency_key="promotion:candidate",
            )
            == promotion
        )
        with pytest.raises(ValueError, match="stale"):
            record_prompt_promotion_decision(
                connection,
                baseline_run_id=baseline.id,
                candidate_run_id=candidate.id,
                expected_comparison_id="0" * 64,
                decision="rejected",
                reason="Different evidence.",
                actor="owner",
                created_at=now,
                idempotency_key="promotion:stale",
            )
        with pytest.raises(ValueError, match="cannot be approved"):
            record_prompt_promotion_decision(
                connection,
                baseline_run_id=baseline.id,
                candidate_run_id=candidate.id,
                expected_comparison_id=comparison.id,
                decision="approved",
                reason="Approve anyway.",
                actor="owner",
                created_at=now,
                idempotency_key="promotion:invalid-approval",
            )
        authoritative_counts = connection.execute(
            """
            SELECT (SELECT count(*) FROM evaluation_manifests),
                   (SELECT count(*) FROM evaluation_runs),
                   (SELECT count(*) FROM prompt_promotion_decisions)
            """
        ).fetchone()

        attempted_ids: list[str] = []

        def unavailable_sender(projection: LangfuseProjection) -> object:
            attempted_ids.append(projection.idempotency_key)
            raise LangfuseUnavailable("Langfuse is down")

        failed = deliver_next_projection(
            connection,
            sender=unavailable_sender,
            owner_token=uuid4(),
            now=now,
            lease_for=timedelta(minutes=1),
            retry_after=timedelta(minutes=5),
        )
        assert isinstance(failed, ProjectionFailed)
        assert (
            connection.execute(
                """
            SELECT (SELECT count(*) FROM evaluation_manifests),
                   (SELECT count(*) FROM evaluation_runs),
                   (SELECT count(*) FROM prompt_promotion_decisions)
            """
            ).fetchone()
            == authoritative_counts
        )

        delivered = deliver_next_projection(
            connection,
            sender=lambda projection: {"remote_id": f"langfuse-{projection.idempotency_key}"},
            owner_token=uuid4(),
            now=now + timedelta(minutes=5),
            lease_for=timedelta(minutes=1),
            retry_after=timedelta(minutes=5),
        )
        assert isinstance(delivered, ProjectionDelivered)
        assert delivered.projection_id == attempted_ids[0]
        assert connection.execute(
            "SELECT attempt_count FROM langfuse_projection_items WHERE id = %s",
            (delivered.projection_id,),
        ).fetchone() == (2,)


def test_rebuilds_langfuse_projections_idempotently(authority_schema: str) -> None:
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    with _connection(authority_schema) as connection:
        manifest_id, target, rates = _seed_evaluation_execution_context(connection, now)
        prompt_release = load_prompt_release(connection, target.prompt_release_id)
        faithful_release = store_relevance_release(
            connection,
            build_relevance_release(build_jev_faithful_policy(prompt_release)),
            created_at=now,
            created_by="contract",
        )
        candidate_target = ReleaseTarget(
            prompt_release_id=target.prompt_release_id,
            relevance_release_id=faithful_release.id,
        )

        def evaluator(
            case: EvaluationManifestCase,
            _case_target: ReleaseTarget,
            _trial: int,
        ) -> EvaluationResult:
            if case.expected_outcome == "qualified":
                return Qualified(reason="Expected positive.", profile_name="profile")
            return Rejected(reason="Expected negative.")

        def execute(
            idempotency_key: str, run_target: ReleaseTarget, implementation_ref: str
        ) -> str:
            execution = run_manifest(
                connection,
                command=EvaluateManifestCommand(
                    manifest_id=manifest_id,
                    target=run_target,
                    implementation_ref=implementation_ref,
                    idempotency_key=idempotency_key,
                ),
                create_exchange_rates=lambda: rates,
                create_evaluator=lambda _rates, _record: evaluator,
                now=lambda: now,
            )
            assert isinstance(execution, CompletedEvaluationExecution)
            return execution.run.id

        baseline_run_id = execute("evaluation:baseline", target, "rebuild-baseline")
        candidate_run_id = execute("evaluation:candidate", candidate_target, "rebuild-candidate")
        comparison = preview_run_comparison(connection, baseline_run_id, candidate_run_id)
        promotion = record_prompt_promotion_decision(
            connection,
            baseline_run_id=baseline_run_id,
            candidate_run_id=candidate_run_id,
            expected_comparison_id=comparison.id,
            decision="rejected",
            reason="Owner declined promotion.",
            actor="owner",
            created_at=now,
            idempotency_key="promotion:rebuild",
        )
        assert promotion.decision == "rejected"
        _insert_accepted_model_call_attempt(connection, prompt_release, now)

        delivered = deliver_next_projection(
            connection,
            sender=lambda projection: {"remote_id": f"langfuse-{projection.idempotency_key}"},
            owner_token=uuid4(),
            now=now,
            lease_for=timedelta(minutes=1),
            retry_after=timedelta(minutes=5),
        )
        assert isinstance(delivered, ProjectionDelivered)

        first_counts = rebuild_langfuse_projections(connection)
        assert first_counts == {"model calls": 1, "manifests": 1, "runs": 2, "promotions": 1}
        rows_after_first = connection.execute(
            "SELECT id, state FROM langfuse_projection_items ORDER BY id"
        ).fetchall()

        second_counts = rebuild_langfuse_projections(connection)
        assert second_counts == first_counts
        assert (
            connection.execute(
                "SELECT id, state FROM langfuse_projection_items ORDER BY id"
            ).fetchall()
            == rows_after_first
        )
        assert connection.execute(
            """
            SELECT id, remote_id FROM langfuse_projection_items WHERE state = 'completed'
            """
        ).fetchall() == [(delivered.projection_id, f"langfuse-{delivered.projection_id}")]


def test_rebuilds_model_call_payloads_matching_live_enqueue(authority_schema: str) -> None:
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        release = bootstrap_prompt_release(connection)
        attempt = _insert_accepted_model_call_attempt(connection, release, now)
        enqueue_model_call_projection(connection, attempt)
        live_payload = connection.execute(
            """
            SELECT payload, payload_digest FROM langfuse_projection_items
            WHERE kind = 'model_call' AND source_id = %s
            """,
            (str(attempt.id),),
        ).fetchone()
        assert live_payload is not None
        connection.execute(
            "DELETE FROM langfuse_projection_items WHERE kind = 'model_call' AND source_id = %s",
            (str(attempt.id),),
        )

        counts = rebuild_langfuse_projections(connection)
        assert counts["model calls"] == 1
        rebuilt_payload = connection.execute(
            """
            SELECT payload, payload_digest FROM langfuse_projection_items
            WHERE kind = 'model_call' AND source_id = %s
            """,
            (str(attempt.id),),
        ).fetchone()
        assert rebuilt_payload == live_payload


def test_reclaims_expired_projection_leases_and_reports_loss(authority_schema: str) -> None:
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    with _connection(authority_schema) as connection:
        _seed_evaluation_execution_context(connection, now)
        stealing_owner = uuid4()

        def stealing_sender(projection: LangfuseProjection) -> object:
            connection.execute(
                """
                UPDATE langfuse_projection_items
                SET owner_token = %s, lease_expires_at = %s
                WHERE id = %s
                """,
                (stealing_owner, now + timedelta(minutes=5), projection.id),
            )
            return {"remote_id": "stolen-delivery"}

        lost = deliver_next_projection(
            connection,
            sender=stealing_sender,
            owner_token=uuid4(),
            now=now,
            lease_for=timedelta(minutes=1),
            retry_after=timedelta(minutes=5),
        )
        assert isinstance(lost, ProjectionLeaseLost)
        assert connection.execute(
            """
            SELECT state, owner_token, attempt_count, remote_id, completed_at
            FROM langfuse_projection_items WHERE id = %s
            """,
            (lost.projection_id,),
        ).fetchone() == ("leased", stealing_owner, 1, None, None)

        connection.execute(
            "UPDATE langfuse_projection_items SET lease_expires_at = %s WHERE id = %s",
            (now - timedelta(seconds=1), lost.projection_id),
        )
        reclaimed = deliver_next_projection(
            connection,
            sender=lambda projection: {"remote_id": f"langfuse-{projection.idempotency_key}"},
            owner_token=uuid4(),
            now=now,
            lease_for=timedelta(minutes=1),
            retry_after=timedelta(minutes=5),
        )
        assert isinstance(reclaimed, ProjectionDelivered)
        assert reclaimed.projection_id == lost.projection_id
        assert connection.execute(
            """
            SELECT state, attempt_count, remote_id FROM langfuse_projection_items
            WHERE id = %s
            """,
            (lost.projection_id,),
        ).fetchone() == (
            "completed",
            2,
            f"langfuse-{lost.projection_id}",
        )


def test_records_invalid_projection_responses_and_reports_idle_queue(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    with _connection(authority_schema) as connection:
        _seed_evaluation_execution_context(connection, now)

        def empty_sender(_projection: LangfuseProjection) -> object:
            return {}

        failed = deliver_next_projection(
            connection,
            sender=empty_sender,
            owner_token=uuid4(),
            now=now,
            lease_for=timedelta(minutes=1),
            retry_after=timedelta(hours=1),
        )
        assert isinstance(failed, ProjectionFailed)
        assert failed.error_code == "invalid_response"
        status = load_projection_queue_status(connection)
        assert status.pending_count == 0
        assert status.failed_count == 1
        assert [summary.error_code for summary in status.failures] == ["invalid_response"]
        reason = connection.execute(
            "SELECT last_error ->> 'reason' FROM langfuse_projection_items WHERE id = %s",
            (failed.projection_id,),
        ).fetchone()
        assert reason is not None
        assert "remote_id" in str(reason[0])
        assert "Field required" in str(reason[0])

        idle = deliver_next_projection(
            connection,
            sender=empty_sender,
            owner_token=uuid4(),
            now=now,
            lease_for=timedelta(minutes=1),
            retry_after=timedelta(hours=1),
        )
        assert isinstance(idle, ProjectionIdle)


def test_pages_review_feedback_across_the_default_limit(authority_schema: str) -> None:
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    run_id = uuid4()
    feedback_count = 55
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        release = bootstrap_prompt_release(connection)
        _insert_prompt_run(connection, run_id, release.id, now)
        moments: list[datetime] = []
        for index in range(feedback_count):
            evaluation_id = _insert_review_decision(
                connection, run_id, release.id, now, 400 + index, "qualified"
            )
            assert enqueue_qualified_review_item(connection, evaluation_id, now.date())
            review_item = connection.execute(
                "SELECT id FROM review_items WHERE evaluation_id = %s", (evaluation_id,)
            ).fetchone()
            assert review_item is not None
            moment = now + timedelta(seconds=index)
            feedback = record_review(
                connection,
                ReviewSubmission(
                    review_item_id=UUID(str(review_item[0])),
                    evaluation_id=evaluation_id,
                    snapshot_id=f"{500 + index:064x}",
                    decision="pursue",
                    target_profile="applied-ai-product-engineer",
                    primary_reason="technology-fit",
                    actor="owner",
                    created_at=moment,
                ),
            )
            assert isinstance(feedback, ReviewSaved)
            moments.append(moment)
        newest_first = sorted(moments, reverse=True)

        first_page = list_review_feedback(connection)
        assert len(first_page.items) == 50
        assert first_page.next_offset == 50
        assert [item.created_at for item in first_page.items] == newest_first[:50]

        second_page = list_review_feedback(connection, offset=50)
        assert len(second_page.items) == feedback_count - 50
        assert second_page.next_offset is None
        assert [item.created_at for item in second_page.items] == newest_first[50:]


def _seed_evaluation_execution_context(
    connection: psycopg.Connection[tuple[object, ...]],
    now: datetime,
) -> tuple[str, ReleaseTarget, ExchangeRateSnapshot]:
    apply_migrations(connection)
    prompt_release = bootstrap_prompt_release(connection)
    pipeline_run_id = uuid4()
    _insert_prompt_run(connection, pipeline_run_id, prompt_release.id, now)
    decision_id = _insert_review_decision(
        connection, pipeline_run_id, prompt_release.id, now, 91, "qualified"
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
) -> str:
    job_id = UUID(int=value)
    snapshot_id = f"{value + 100:064x}"
    evaluation_id = f"{value + 1000:064x}"
    raw_url = f"https://example.com/jobs/review-{value}"
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
          date_posted, observed_at
        ) VALUES (%s, %s, %s, %s, 'Acme', 'acme', %s, 'other', %s,
          'Build useful tools.', 'Remote', '["python"]'::jsonb, %s, %s)
        """,
        (
            snapshot_id,
            job_id,
            f"{value + 200:064x}",
            f"Engineer {value}",
            f"engineer {value}",
            raw_url,
            now.date(),
            now,
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
