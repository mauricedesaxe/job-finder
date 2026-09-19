from __future__ import annotations

from collections.abc import Generator, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from threading import Barrier
from typing import LiteralString, cast
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

import job_finder.configuration_service as configuration_service_module
from job_finder.ats.models import CompensationObservation
from job_finder.config import PostgresContractSettings
from job_finder.configuration_service import (
    ActivationTargetUnpublished,
    ActiveConfigurationChanged,
    ActivateConfigurationCommand,
    ConfigurationActivated,
    ConfigurationPublished,
    DraftChanged,
    DraftSaved,
    PublicationIdempotencyKeyConflict,
    PublishConfigurationCommand,
    PublishDraftChanged,
    SaveDraftCommand,
    activate_search_configuration,
    load_published_active_search_configuration,
    publish_search_configuration,
    save_search_configuration_draft,
)
from job_finder.database import MIGRATIONS_PATH, apply_migrations
from job_finder.evaluation import (
    EvaluationManifestCase,
    LangfuseProjection,
    LangfuseUnavailable,
    ManifestPolicy,
    ProjectionDelivered,
    ProjectionFailed,
    bootstrap_prompt_release,
    create_manifest,
    decide_prompt_promotion,
    deliver_next_projection,
    exclude_review_event,
    include_review_event,
    list_manifests,
    load_projection_queue_status,
    load_prompt_release,
    preview_manifest,
    run_manifest,
)
from job_finder.evaluation.models import (
    CriterionAccepted,
    EvaluationResult,
    RetryableOperationalError,
    TerminalOperationalError,
    ModelCallContext,
    PromptAccepted,
    PromptReleaseId,
    Qualified,
    Rejected,
)
from job_finder.jobs.decision_pipeline import (
    DecisionContext,
    PersistedDecision,
    postgres_decision_store,
    process_qualified_job,
)
from job_finder.jobs.enrichment import EnrichedJob
from job_finder.jobs.models import JobListing
from job_finder.jobs.title_deduplication import TitleDuplicate
from job_finder.review.models import ReviewSaved, ReviewSubmission
from job_finder.review.postgres import (
    deterministic_rejected_sample,
    enqueue_qualified_review_item,
    enqueue_rejected_audit_sample,
    list_review_feedback,
    load_review_feedback,
    load_review_queue,
    record_review,
)
from job_finder.search_configuration import (
    DEFAULT_SEARCH_CONFIGURATION,
    SearchConfigurationDraft,
    SearchConfigurationRevision,
    SearchConfigurationRevisionId,
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


def test_migrations_are_repeatable(authority_schema: str) -> None:
    with _connection(authority_schema) as connection:
        first = apply_migrations(connection)
        second = apply_migrations(connection)

        assert first == (
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
        )
        assert second == first
        assert connection.execute(
            "SELECT count(*) FROM job_finder_schema_migrations"
        ).fetchone() == (19,)


def test_concurrent_migration_startup_serializes_schema_writes(
    authority_schema: str,
) -> None:
    def migrate() -> tuple[str, ...]:
        with _connection(authority_schema) as connection:
            return apply_migrations(connection)

    def migrate_for_index(_index: int) -> tuple[str, ...]:
        return migrate()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(migrate_for_index, range(2)))

    assert results[0] == results[1]
    assert results[0][-1] == "0019_configuration_publication_receipts.sql"


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

        migrations = apply_migrations(connection)

        assert migrations[-1] == "0019_configuration_publication_receipts.sql"
        assert _table_contents(connection, legacy_tables) == before


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
        assert draft_publication.prompt_release_id == active_publication.prompt_release_id
        assert connection.execute(
            "SELECT count(*) FROM search_configuration_publications"
        ).fetchone() == (2,)
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
        assert connection.execute("SELECT count(*) FROM prompt_versions").fetchone() == (
            len(release.versions),
        )
        assert connection.execute("SELECT count(*) FROM prompt_releases").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM prompt_release_members").fetchone() == (
            len(release.versions),
        )


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
        assert connection.execute("SELECT count(*) FROM prompt_versions").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM prompt_release_members").fetchone() == (0,)


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


def test_rolls_back_feedback_when_company_block_fails(authority_schema: str) -> None:
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
                    decision="reject",
                    target_profile="neither",
                    primary_reason="company-quality",
                    block_company=True,
                    actor="owner",
                    created_at=now,
                ),
            )

        assert connection.execute("SELECT count(*) FROM review_events").fetchone() == (0,)
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


def test_runs_trials_rejects_operational_failures_and_retries_projection(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    run_id = uuid4()
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        baseline_release = bootstrap_prompt_release(connection)
        candidate_release_id = _insert_candidate_release(connection, baseline_release.id, now)
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
        baseline_calls = 0

        def baseline_evaluator(
            case: EvaluationManifestCase, release_id: PromptReleaseId, trial: int
        ) -> EvaluationResult:
            nonlocal baseline_calls
            baseline_calls += 1
            assert release_id == baseline_release.id
            assert trial >= 0
            if case.expected_outcome == "qualified":
                return Qualified(reason="Expected positive.", profile_name="profile")
            return Rejected(reason="Expected negative.")

        baseline = run_manifest(
            connection,
            manifest_id=manifest.id,
            prompt_release_id=baseline_release.id,
            evaluator=baseline_evaluator,
            implementation_ref="baseline-ref",
            completed_at=now,
            idempotency_key="evaluation:baseline",
        )
        repeated = run_manifest(
            connection,
            manifest_id=manifest.id,
            prompt_release_id=baseline_release.id,
            evaluator=baseline_evaluator,
            implementation_ref="baseline-ref",
            completed_at=now,
            idempotency_key="evaluation:baseline",
        )
        assert repeated == baseline
        assert baseline_calls == 4

        def candidate_evaluator(
            case: EvaluationManifestCase, release_id: PromptReleaseId, trial: int
        ) -> EvaluationResult:
            assert release_id == candidate_release_id
            if case.expected_outcome == "qualified":
                return RetryableOperationalError(
                    prompt_name="profile",
                    error_code="timeout",
                    reason="Provider timed out.",
                )
            if trial == 0:
                return Qualified(reason="Incorrect pass.", profile_name="profile")
            return Rejected(reason="Expected negative.")

        candidate = run_manifest(
            connection,
            manifest_id=manifest.id,
            prompt_release_id=PromptReleaseId(candidate_release_id),
            evaluator=candidate_evaluator,
            implementation_ref="candidate-ref",
            completed_at=now,
            idempotency_key="evaluation:candidate",
        )
        promotion = decide_prompt_promotion(
            connection,
            baseline_run_id=baseline.id,
            candidate_run_id=candidate.id,
            actor="owner",
            created_at=now,
            idempotency_key="promotion:candidate",
        )

        assert candidate.metrics.false_positive_count == 1
        assert candidate.metrics.false_negative_count == 0
        assert candidate.metrics.operational_failure_count == 1
        assert candidate.metrics.critical_false_positive_count == 1
        assert promotion.decision == "rejected"
        assert promotion.baseline_prompt_release_id == baseline_release.id
        assert promotion.candidate_prompt_release_id == candidate_release_id
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


def _insert_candidate_release(
    connection: psycopg.Connection[tuple[object, ...]],
    baseline_release_id: str,
    now: datetime,
) -> str:
    candidate_release_id = "f" * 64
    with connection.transaction():
        connection.execute(
            """
            INSERT INTO prompt_releases (
              id, name, content_digest, expected_member_count, created_at, created_by
            )
            SELECT %s, 'candidate', %s, expected_member_count, %s, 'contract'
            FROM prompt_releases WHERE id = %s
            """,
            (candidate_release_id, "e" * 64, now, baseline_release_id),
        )
        connection.execute(
            """
            INSERT INTO prompt_release_members (
              release_id, prompt_name, prompt_version_id, position
            )
            SELECT %s, prompt_name, prompt_version_id, position
            FROM prompt_release_members WHERE release_id = %s
            """,
            (candidate_release_id, baseline_release_id),
        )
    return candidate_release_id


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
