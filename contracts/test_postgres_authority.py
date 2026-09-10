from __future__ import annotations

from collections.abc import Generator, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql

from job_finder.config import PostgresContractSettings
from job_finder.database import apply_migrations


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

        assert first == ("0001_authoritative_job_state.sql",)
        assert second == first
        assert connection.execute(
            "SELECT count(*) FROM job_finder_schema_migrations"
        ).fetchone() == (1,)


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
