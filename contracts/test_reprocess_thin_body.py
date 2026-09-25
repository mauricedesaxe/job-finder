from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql

from job_finder.pipeline.reprocess import (
    reset_thin_body_jobs,
    select_thin_body_jobs,
)
from job_finder.pipeline.work_items import claim_next_job, find_terminal_decision_id
from job_finder.config import PostgresContractSettings
from job_finder.database import apply_migrations


@pytest.fixture
def authority_schema() -> Generator[str]:
    settings = PostgresContractSettings.from_environment()
    schema_name = f"job_finder_reprocess_thin_contract_{uuid4().hex}"
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
        try:
            yield schema_name
        finally:
            connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema_name))
            )


@contextmanager
def _connection(schema_name: str) -> Generator[psycopg.Connection[tuple[object, ...]]]:
    settings = PostgresContractSettings.from_environment()
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema_name)))
        yield connection


_NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


def _seed_release_and_run(connection: psycopg.Connection[tuple[object, ...]]) -> tuple[str, UUID]:
    release_id = f"{uuid4().int:064x}"
    version_id = f"{uuid4().int:064x}"
    with connection.transaction():
        connection.execute(
            """
            INSERT INTO prompt_versions (
              id, prompt_name, criterion, phase, content_digest, messages,
              input_schema, output_schema, model, parameters, created_at
            ) VALUES (%s, 'contract-prompt', 'contract', 'enrichment', %s, '[]'::jsonb,
              '{}'::jsonb, '{}'::jsonb, 'test/model', '{}'::jsonb, %s)
            """,
            (version_id, f"{uuid4().int:064x}", _NOW),
        )
        connection.execute(
            """
            INSERT INTO prompt_releases (
              id, name, content_digest, expected_member_count, created_at, created_by
            ) VALUES (%s, %s, %s, 1, %s, 'contract')
            """,
            (release_id, f"release-{uuid4().hex[:8]}", f"{uuid4().int:064x}", _NOW),
        )
        connection.execute(
            """
            INSERT INTO prompt_release_members (
              release_id, prompt_name, prompt_version_id, position
            ) VALUES (%s, 'contract-prompt', %s, 0)
            """,
            (release_id, version_id),
        )
    run_id = uuid4()
    connection.execute(
        """
        INSERT INTO pipeline_runs (
          id, idempotency_key, kind, implementation_ref, prompt_release_id, parameters,
          status, started_at, completed_at
        ) VALUES (%s, %s, 'processing', 'contract-ref', %s, '{}'::jsonb, 'completed', %s, %s)
        """,
        (run_id, f"reprocess-thin:{run_id}", release_id, _NOW, _NOW),
    )
    return release_id, run_id


def _seed_thin_body_job(
    connection: psycopg.Connection[tuple[object, ...]],
    run_id: UUID,
    value: int,
    description: str = "## Overview\nAcme is hiring.",
) -> UUID:
    job_id = UUID(int=value)
    snapshot_id = f"{value + 100:064x}"
    evaluation_id = f"{value + 1000:064x}"
    raw_url = f"https://jobs.ashbyhq.com/acme/{value}"
    connection.execute(
        """
        INSERT INTO jobs (id, raw_url, first_discovered_at, last_discovered_at)
        VALUES (%s, %s, %s, %s)
        """,
        (job_id, raw_url, _NOW, _NOW),
    )
    connection.execute(
        """
        INSERT INTO job_snapshots (
          id, job_id, content_digest, title, company, normalized_company,
          normalized_title, source, raw_url, description, location, keywords,
          date_posted, observed_at
        ) VALUES (%s, %s, %s, %s, 'Acme', 'acme', %s, 'ashbyhq', %s,
          %s, 'Remote', '["python"]'::jsonb, %s, %s)
        """,
        (
            snapshot_id,
            job_id,
            f"{value + 200:064x}",
            f"About Acme {value}",
            f"about acme {value}",
            raw_url,
            description,
            _NOW.date(),
            _NOW,
        ),
    )
    connection.execute(
        """
        INSERT INTO evaluation_decisions (
          id, snapshot_id, pipeline_run_id, prompt_release_id, policy_version,
          outcome, matched_profile, reason, created_at
        ) VALUES (%s, %s, %s, (SELECT prompt_release_id FROM pipeline_runs WHERE id = %s),
          'policy-1', 'rejected', NULL, %s, %s)
        """,
        (
            evaluation_id,
            snapshot_id,
            run_id,
            run_id,
            f"The description is empty, so the title alone suggests a fit. {value}",
            _NOW,
        ),
    )
    connection.execute(
        """
        INSERT INTO job_work_items (
          job_id, discovery_run_id, keyword, state, attempt_count,
          terminal_decision_id, created_at, completed_at
        ) VALUES (%s, %s, 'ai', 'completed', 1, %s, %s, %s)
        """,
        (job_id, run_id, evaluation_id, _NOW, _NOW),
    )
    return job_id


def _seed_correction(connection: psycopg.Connection[tuple[object, ...]], value: int) -> None:
    connection.execute(
        """
        INSERT INTO snapshot_corrections (
          snapshot_id, description, reason, created_at
        ) VALUES (%s, %s, 'ats backfill', %s)
        """,
        (f"{value + 100:064x}", "## Your mission\n" + "Own features end to end. " * 40, _NOW),
    )


def test_selects_only_corrected_thin_bodies_and_reset_requeues_them(
    authority_schema: str,
) -> None:
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        _release_id, run_id = _seed_release_and_run(connection)
        thin = _seed_thin_body_job(connection, run_id, 1)
        fat = _seed_thin_body_job(connection, run_id, 2, description="A full posting. " + "x" * 600)
        _seed_correction(connection, 1)
        _seed_correction(connection, 2)

        selected = select_thin_body_jobs(connection)
        assert selected == (thin,)

        reset = reset_thin_body_jobs(connection, selected)
        assert reset == 1

        row = connection.execute(
            "SELECT state, terminal_decision_id, attempt_count FROM job_work_items WHERE job_id = %s",
            (thin,),
        ).fetchone()
        assert row == ("pending", None, 0)
        assert find_terminal_decision_id(connection, thin) is None

        survivor = connection.execute(
            """
            SELECT count(*) FROM evaluation_decisions d
            JOIN job_snapshots s ON s.id = d.snapshot_id
            WHERE s.job_id = %s
            """,
            (fat,),
        ).fetchone()
        assert survivor == (1,)


def test_ignores_thin_bodies_without_a_correction(authority_schema: str) -> None:
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        _release_id, run_id = _seed_release_and_run(connection)
        _seed_thin_body_job(connection, run_id, 3)

        assert select_thin_body_jobs(connection) == ()


def test_keeps_decisions_quoted_by_review_items(authority_schema: str) -> None:
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        _release_id, run_id = _seed_release_and_run(connection)
        reviewed = _seed_thin_body_job(connection, run_id, 4)
        _seed_correction(connection, 4)
        decision_row = connection.execute(
            "SELECT terminal_decision_id FROM job_work_items WHERE job_id = %s",
            (reviewed,),
        ).fetchone()
        assert decision_row is not None
        decision_id = decision_row[0]
        connection.execute(
            """
            INSERT INTO review_items (
              id, evaluation_id, review_day, lane, position, created_at
            ) VALUES (%s, %s, %s, 'rejected_audit', 0, %s)
            """,
            (uuid4(), decision_id, _NOW.date(), _NOW),
        )

        assert select_thin_body_jobs(connection) == ()
        assert reset_thin_body_jobs(connection, (reviewed,)) == 0

        row = connection.execute(
            "SELECT state, terminal_decision_id FROM job_work_items WHERE job_id = %s",
            (reviewed,),
        ).fetchone()
        assert row == ("completed", decision_id)


def test_a_reset_job_flows_through_claim_without_short_circuit(authority_schema: str) -> None:
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        _release_id, run_id = _seed_release_and_run(connection)
        job_id = _seed_thin_body_job(connection, run_id, 5)
        _seed_correction(connection, 5)

        reset = reset_thin_body_jobs(connection, (job_id,))
        assert reset == 1

        claim = claim_next_job(
            connection,
            owner_token=uuid4(),
            claimed_at=_NOW,
            lease_for=timedelta(minutes=30),
        )
        assert claim is not None
        assert claim.job_id == job_id
        assert find_terminal_decision_id(connection, job_id) is None


def test_reset_restores_the_immutability_trigger(authority_schema: str) -> None:
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        _release_id, run_id = _seed_release_and_run(connection)
        job_id = _seed_thin_body_job(connection, run_id, 6)
        _seed_correction(connection, 6)

        _ = reset_thin_body_jobs(connection, (job_id,))

        _seed_thin_body_job(connection, run_id, 7)
        _seed_correction(connection, 7)

        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute("DELETE FROM evaluation_decisions WHERE outcome = 'rejected'")
