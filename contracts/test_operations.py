from __future__ import annotations

from collections.abc import Generator, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest

from job_finder.config import PostgresContractSettings
from job_finder.database import apply_migrations
from job_finder.evaluation.prompt_releases import bootstrap_prompt_release
from job_finder.review.operations import OperationsHealth, load_operations_snapshot


@pytest.fixture
def authority_schema() -> Iterator[str]:
    settings = PostgresContractSettings.from_environment()
    schema_name = f"job_finder_operations_contract_{uuid4().hex}"
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
        try:
            yield schema_name
        finally:
            connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema_name))
            )


def test_operations_snapshot_reads_authoritative_postgres_state(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 21, 12, tzinfo=UTC)
    run_id = uuid4()
    processing_attempt_id = uuid4()
    terminal_job_id = uuid4()
    pending_job_id = uuid4()

    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        release = bootstrap_prompt_release(connection)
        prompt_name, prompt_version_id = connection.execute(
            """
            SELECT prompt_name, prompt_version_id
            FROM prompt_release_members
            WHERE release_id = %s
            ORDER BY prompt_name
            LIMIT 1
            """,
            (release.id,),
        ).fetchone() or pytest.fail("bootstrap release has no members")
        connection.execute(
            """
            INSERT INTO pipeline_runs (
              id, idempotency_key, kind, implementation_ref, prompt_release_id,
              parameters, status, started_at, completed_at
            ) VALUES (%s, %s, 'processing', 'contract-ref', %s, '{}'::jsonb,
              'completed', %s, %s)
            """,
            (run_id, f"operations:{run_id}", release.id, now - timedelta(minutes=5), now),
        )
        connection.execute(
            """
            INSERT INTO processing_attempts (
              id, pipeline_run_id, operation_key, attempt_number, input_digest,
              status, started_at, completed_at
            ) VALUES (%s, %s, 'operations-test', 0, %s, 'completed', %s, %s)
            """,
            (processing_attempt_id, run_id, "a" * 64, now - timedelta(minutes=4), now),
        )
        connection.execute(
            """
            INSERT INTO model_call_attempts (
              id, processing_attempt_id, pipeline_run_id, prompt_release_id,
              request_id, attempt_number, operation_key, prompt_name,
              prompt_version_id, input_digest, requested_model, provider,
              status, parsed_output, raw_response, input_tokens, output_tokens,
              cost_usd, latency_ms, observed_at, response_model, request_messages
            ) VALUES (
              %s, %s, %s, %s, %s, 0, 'operations-test', %s, %s, %s,
              'test-model', 'typesafe', 'accepted', '{}'::jsonb, '{}'::jsonb,
              10, 5, 1.25000000, 25, %s, 'test-model', '[]'::jsonb
            )
            """,
            (
                uuid4(),
                processing_attempt_id,
                run_id,
                release.id,
                "b" * 64,
                prompt_name,
                prompt_version_id,
                "a" * 64,
                now - timedelta(minutes=3),
            ),
        )
        connection.execute(
            """
            INSERT INTO model_call_attempts (
              id, processing_attempt_id, pipeline_run_id, prompt_release_id,
              request_id, attempt_number, operation_key, prompt_name,
              prompt_version_id, input_digest, requested_model, provider,
              status, latency_ms, error, observed_at, request_messages
            ) VALUES (
              %s, %s, %s, %s, %s, 0, 'operations-test', %s, %s, %s,
              'test-model', 'typesafe', 'retryable_error', 50,
              '{"code":"provider_timeout","reason":"Provider did not respond"}'::jsonb,
              %s, '[]'::jsonb
            )
            """,
            (
                uuid4(),
                processing_attempt_id,
                run_id,
                release.id,
                "c" * 64,
                prompt_name,
                prompt_version_id,
                "a" * 64,
                now - timedelta(minutes=2),
            ),
        )
        connection.execute(
            """
            INSERT INTO jobs (id, raw_url, first_discovered_at, last_discovered_at)
            VALUES (%s, 'https://example.com/terminal', %s, %s),
                   (%s, 'https://example.com/pending', %s, %s)
            """,
            (terminal_job_id, now, now, pending_job_id, now, now),
        )
        connection.execute(
            """
            INSERT INTO job_work_items (
              job_id, discovery_run_id, keyword, state, last_error, created_at, completed_at
            ) VALUES (
              %s, %s, 'python', 'terminal_error',
              '{"retryability":"terminal","code":"invalid_job","reason":"Job is invalid"}'::jsonb,
              %s, %s
            )
            """,
            (terminal_job_id, run_id, now, now + timedelta(minutes=1)),
        )
        connection.execute(
            """
            INSERT INTO job_work_items (job_id, discovery_run_id, keyword, state, created_at)
            VALUES (%s, %s, 'python', 'pending', %s)
            """,
            (pending_job_id, run_id, now),
        )

        snapshot = load_operations_snapshot(connection, recent_run_limit=1, failure_limit=1)

    assert snapshot.health is OperationsHealth.ACTION_REQUIRED
    assert snapshot.queues.pending == 1
    assert snapshot.queues.terminal_error == 1
    assert snapshot.spend.known_usd == Decimal("1.25000000")
    assert snapshot.spend.unknown_attempts == 1
    assert len(snapshot.recent_runs) == 1
    assert snapshot.recent_runs[0].id == run_id
    assert len(snapshot.failures) == 1
    assert snapshot.failures[0].source == "job"
    assert snapshot.failures[0].summary == "invalid_job: Job is invalid"


@contextmanager
def _connection(
    schema_name: str,
) -> Generator[psycopg.Connection[tuple[object, ...]], None, None]:
    settings = PostgresContractSettings.from_environment()
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema_name)))
        yield connection
