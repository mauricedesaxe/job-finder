from __future__ import annotations

from collections.abc import Generator, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql

from job_finder.config import PostgresContractSettings
from job_finder.database import apply_migrations
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
    load_prompt_release,
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
    load_adjacent_days,
    load_daily_review,
    prepare_daily_review,
    record_review,
)
from job_finder.evaluation.openrouter import (
    HttpResponse,
    RetryPolicy,
    evaluate_prompt,
    postgres_model_call_persistence,
    prompt_input_digest,
)
from job_finder.evaluation.prompts import ENRICHMENT, PROMPTS


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
        )
        assert second == first
        assert connection.execute(
            "SELECT count(*) FROM job_finder_schema_migrations"
        ).fetchone() == (10,)


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
    assert results[0][-1] == "0010_review_event_revisions.sql"


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


def test_builds_immutable_daily_membership_with_every_qualified_job(
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

        prepare_daily_review(connection, review_day, created_at=now)
        first_rows = connection.execute(
            "SELECT id, evaluation_id, lane, position FROM review_items ORDER BY lane, position"
        ).fetchall()
        late_qualified_id = _insert_review_decision(
            connection, run_id, release.id, now, 99, "qualified"
        )
        prepare_daily_review(connection, review_day, created_at=now)
        review = load_daily_review(connection, review_day)
        second_rows = connection.execute(
            "SELECT id, evaluation_id, lane, position FROM review_items ORDER BY lane, position"
        ).fetchall()

        assert second_rows == first_rows
        assert review.qualified.total == 4
        assert {item.evaluation_id for item in review.qualified.pending} == set(qualified_ids)
        assert late_qualified_id not in {item.evaluation_id for item in review.qualified.pending}
        assert review.rejected_audit.total == 3
        assert {item.evaluation_id for item in review.rejected_audit.pending} == set(
            deterministic_rejected_sample(review_day, rejected_ids)
        )
        with pytest.raises(psycopg.errors.CheckViolation, match="immutable"):
            connection.execute(
                "UPDATE review_items SET position = 99 WHERE id = %s",
                (review.qualified.pending[0].id,),
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
        _insert_review_decision(connection, run_id, release.id, now, 1, "qualified")
        prepare_daily_review(connection, now.date(), created_at=now)
        item = load_daily_review(connection, now.date()).current
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
        review = load_daily_review(connection, now.date())
        assert review.qualified.reviewed_items[0].decision == "reject"
        assert review.qualified.reviewed_items[0].note == "Ukraine-based team."


def test_an_identical_revision_is_a_stored_no_op(authority_schema: str) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    run_id = uuid4()
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        release = bootstrap_prompt_release(connection)
        _insert_prompt_run(connection, run_id, release.id, now)
        _insert_review_decision(connection, run_id, release.id, now, 1, "qualified")
        prepare_daily_review(connection, now.date(), created_at=now)
        item = load_daily_review(connection, now.date()).current
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


def test_adjacent_review_days_skip_days_without_frozen_items(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    earlier = now - timedelta(days=1)
    later = now + timedelta(days=1)
    run_id = uuid4()
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        release = bootstrap_prompt_release(connection)
        _insert_prompt_run(connection, run_id, release.id, earlier)
        _insert_review_decision(connection, run_id, release.id, earlier, 1, "qualified")
        _insert_review_decision(connection, run_id, release.id, later, 2, "qualified")
        prepare_daily_review(connection, earlier.date(), created_at=earlier)
        prepare_daily_review(connection, later.date(), created_at=later)
        prepare_daily_review(connection, now.date(), created_at=now)

        assert load_adjacent_days(connection, earlier.date()) == (None, later.date())
        assert load_adjacent_days(connection, later.date()) == (earlier.date(), None)
        assert load_adjacent_days(connection, now.date()) == (earlier.date(), later.date())


def test_rolls_back_feedback_when_company_block_fails(authority_schema: str) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    run_id = uuid4()
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        release = bootstrap_prompt_release(connection)
        _insert_prompt_run(connection, run_id, release.id, now)
        _insert_review_decision(connection, run_id, release.id, now, 1, "qualified")
        prepare_daily_review(connection, now.date(), created_at=now)
        item = load_daily_review(connection, now.date()).current
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


def test_curates_immutable_feedback_into_a_repeated_trial_manifest(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    run_id = uuid4()
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        release = bootstrap_prompt_release(connection)
        _insert_prompt_run(connection, run_id, release.id, now)
        _insert_review_decision(connection, run_id, release.id, now, 21, "qualified")
        _insert_review_decision(connection, run_id, release.id, now, 22, "rejected")
        prepare_daily_review(connection, now.date(), created_at=now)
        review = load_daily_review(connection, now.date())
        qualified = review.qualified.pending[0]
        rejected = review.rejected_audit.pending[0]
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
        include_review_event(
            connection,
            review_event_id=rejected_feedback.review_event_id,
            critical=True,
            reason="False positives are costly.",
            actor="owner",
            created_at=now,
            idempotency_key="curate:negative",
        )
        include_review_event(
            connection,
            review_event_id=qualified_feedback.review_event_id,
            critical=False,
            reason="Known positive control.",
            actor="owner",
            created_at=now,
            idempotency_key="curate:positive",
        )

        first = create_manifest(
            connection,
            policy=ManifestPolicy(),
            created_at=now,
            created_by="owner",
        )
        exclude_review_event(
            connection,
            review_event_id=qualified_feedback.review_event_id,
            reason="Temporarily disputed.",
            actor="owner",
            created_at=now + timedelta(seconds=1),
            idempotency_key="exclude:positive",
        )
        second = create_manifest(
            connection,
            policy=ManifestPolicy(),
            created_at=now + timedelta(seconds=1),
            created_by="owner",
        )

        assert len(first.cases) == 2
        assert sorted(case.trial_count for case in first.cases) == [1, 3]
        assert len(second.cases) == 1
        assert connection.execute("SELECT count(*) FROM review_events").fetchone() == (2,)
        assert connection.execute(
            "SELECT count(*) FROM langfuse_projection_items WHERE kind = 'evaluation_manifest'"
        ).fetchone() == (2,)
        include_review_event(
            connection,
            review_event_id=qualified_feedback.review_event_id,
            critical=False,
            reason="Dispute resolved.",
            actor="owner",
            created_at=now + timedelta(seconds=2),
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
                created_at=now + timedelta(seconds=2),
                created_by="owner",
            )
        assert connection.execute("SELECT count(*) FROM evaluation_manifests").fetchone() == (2,)
        assert connection.execute("SELECT count(*) FROM evaluation_manifest_cases").fetchone() == (
            3,
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
        _insert_review_decision(connection, run_id, baseline_release.id, now, 31, "qualified")
        _insert_review_decision(connection, run_id, baseline_release.id, now, 32, "rejected")
        prepare_daily_review(connection, now.date(), created_at=now)
        review = load_daily_review(connection, now.date())
        qualified = review.qualified.pending[0]
        rejected = review.rejected_audit.pending[0]
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


@contextmanager
def _connection(
    schema_name: str,
) -> Generator[psycopg.Connection[tuple[object, ...]], None, None]:
    settings = PostgresContractSettings.from_environment()
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema_name)))
        yield connection


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
