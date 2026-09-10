from __future__ import annotations

from collections.abc import Generator, Iterator, Mapping
from contextlib import contextmanager
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql

from job_finder.config import PostgresContractSettings
from job_finder.database import apply_migrations
from job_finder.evaluation import (
    bootstrap_prompt_release,
    load_prompt_release,
)
from job_finder.evaluation.models import (
    CriterionAccepted,
    ModelCallContext,
    PromptAccepted,
    Qualified,
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
from job_finder.evaluation.openrouter import (
    HttpResponse,
    evaluate_prompt,
    postgres_model_call_persistence,
    prompt_input_digest,
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
        )
        assert second == first
        assert connection.execute(
            "SELECT count(*) FROM job_finder_schema_migrations"
        ).fetchone() == (2,)


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
              id, prompt_name, content_digest, messages, input_schema, output_schema,
              model, parameters, created_at
            ) VALUES (%s, 'profile', %s, '[]'::jsonb, '{}'::jsonb, '{}'::jsonb,
              'test-model', '{}'::jsonb, %s)
            """,
            ("4" * 64, "5" * 64, now),
        )
        connection.execute(
            """
            INSERT INTO prompt_versions (
              id, prompt_name, content_digest, messages, input_schema, output_schema,
              model, parameters, created_at
            ) VALUES (%s, 'other', %s, '[]'::jsonb, '{}'::jsonb, '{}'::jsonb,
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
                INSERT INTO prompt_release_members (release_id, prompt_name, prompt_version_id)
                VALUES (%s, 'profile', %s)
                """,
                ("6" * 64, "4" * 64),
            )

        with pytest.raises(psycopg.errors.CheckViolation, match="requires 1 members"):
            connection.execute(
                """
                INSERT INTO prompt_release_members (release_id, prompt_name, prompt_version_id)
                VALUES (%s, 'other', %s)
                """,
                ("6" * 64, "8" * 64),
            )


def test_bootstraps_the_complete_prompt_release_idempotently(authority_schema: str) -> None:
    with _connection(authority_schema) as connection:
        apply_migrations(connection)

        first = bootstrap_prompt_release(connection)
        second = bootstrap_prompt_release(connection)

        assert second == first
        assert load_prompt_release(connection, first.id) == first
        assert connection.execute("SELECT count(*) FROM prompt_versions").fetchone() == (8,)
        assert connection.execute("SELECT count(*) FROM prompt_releases").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM prompt_release_members").fetchone() == (8,)


def test_records_and_reuses_an_accepted_model_call(authority_schema: str) -> None:
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
                        "usage": {
                            "prompt_tokens": 12,
                            "completion_tokens": 4,
                            "cost": 0.00012,
                        },
                    }
                ),
            )

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

        assert first == CriterionAccepted(
            prompt_name=release.versions[0].definition.name,
            passed=True,
            reason="matched",
        )
        assert second == first
        assert calls == 1
        assert connection.execute(
            """
            SELECT status, response_model, provider_response_id, input_tokens,
                   output_tokens, cost_usd, parsed_output
            FROM model_call_attempts
            """
        ).fetchone() == (
            "accepted",
            "google/gemini-2.5-flash-001",
            "generation-1",
            12,
            4,
            Decimal("0.00012000"),
            {"pass": True, "reason": "matched"},
        )


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
