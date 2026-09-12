from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql

from job_finder.pipeline import (
    claim_next_job,
    find_terminal_decision_id,
    reset_mis_titled_jobs,
    select_mis_titled_jobs,
)
from job_finder.config import PostgresContractSettings
from job_finder.database import apply_migrations


@pytest.fixture
def authority_schema() -> Generator[str]:
    settings = PostgresContractSettings.from_environment()
    schema_name = f"job_finder_reprocess_contract_{uuid4().hex}"
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
        (run_id, f"reprocess:{run_id}", release_id, _NOW, _NOW),
    )
    return release_id, run_id


def _seed_receipt(
    connection: psycopg.Connection[tuple[object, ...]],
    run_id: UUID,
    job_id: UUID,
) -> None:
    connection.execute(
        """
        INSERT INTO pipeline_receipts (
          id, idempotency_key, pipeline_run_id, job_id, operation_key,
          input_digest, output_digest, output, implementation_ref,
          prompt_release_id, completed_at
        ) VALUES (
          %s, %s, %s, %s, 'process_qualified_job', %s, %s, '{}'::jsonb,
          'contract-ref', NULL, %s
        )
        """,
        (
            f"{uuid4().int:064x}",
            f"rejected-decision:{uuid4().hex}",
            run_id,
            job_id,
            f"{uuid4().int:064x}",
            f"{uuid4().int:064x}",
            _NOW,
        ),
    )


def _seed_refused_job(
    connection: psycopg.Connection[tuple[object, ...]],
    run_id: UUID,
    value: int,
    reason: str,
) -> UUID:
    job_id = UUID(int=value)
    snapshot_id = f"{value + 100:064x}"
    evaluation_id = f"{value + 1000:064x}"
    raw_url = f"https://jobs.lever.co/acme/{value}"
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
        ) VALUES (%s, %s, %s, %s, 'Acme', 'acme', %s, 'lever', %s,
          'Build useful tools.', 'Remote', '["python"]'::jsonb, %s, %s)
        """,
        (
            snapshot_id,
            job_id,
            f"{value + 200:064x}",
            f"About Acme {value}",
            f"about acme {value}",
            raw_url,
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
        (evaluation_id, snapshot_id, run_id, run_id, reason, _NOW),
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


def test_selects_only_mis_titled_refusals_and_reset_requeues_them(
    authority_schema: str,
) -> None:
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        _release_id, run_id = _seed_release_and_run(connection)
        not_a_role = _seed_refused_job(
            connection, run_id, 1, "Title does not identify a role (About Acme 1)"
        )
        generic = _seed_refused_job(
            connection, run_id, 2, "Generic / talent-pool title (Join our team)"
        )
        _seed_refused_job(
            connection,
            run_id,
            3,
            'The job description explicitly states "Remote (India)", which excludes Europe.',
        )
        _seed_receipt(connection, run_id, not_a_role)

        selected = select_mis_titled_jobs(connection)
        assert set(selected) == {not_a_role, generic}

        reset = reset_mis_titled_jobs(connection, selected)
        assert reset == 2

        for job_id in (not_a_role, generic):
            row = connection.execute(
                "SELECT state, terminal_decision_id FROM job_work_items WHERE job_id = %s",
                (job_id,),
            ).fetchone()
            assert row == ("pending", None)
            assert find_terminal_decision_id(connection, job_id) is None

        receipts = connection.execute(
            "SELECT count(*) FROM pipeline_receipts WHERE job_id = %s",
            (not_a_role,),
        ).fetchone()
        assert receipts == (0,)

        survivor = connection.execute(
            """
            SELECT count(*) FROM evaluation_decisions d
            JOIN job_snapshots s ON s.id = d.snapshot_id
            WHERE s.job_id = %s
            """,
            (UUID(int=3),),
        ).fetchone()
        assert survivor == (1,)


def test_ignores_jobs_with_more_than_one_decision(authority_schema: str) -> None:
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        _release_id, run_id = _seed_release_and_run(connection)
        multi = _seed_refused_job(
            connection, run_id, 8, "Title does not identify a role (About Acme 8)"
        )
        second_snapshot = f"{8 + 300:064x}"
        connection.execute(
            """
            INSERT INTO job_snapshots (
              id, job_id, content_digest, title, company, normalized_company,
              normalized_title, source, raw_url, description, location, keywords,
              date_posted, observed_at
            ) VALUES (%s, %s, %s, 'Sr Engineer - Acme', 'Acme', 'acme',
              'sr engineer - acme', 'lever', 'https://jobs.lever.co/acme/8',
              'Build useful tools.', 'Remote', '["python"]'::jsonb, %s, %s)
            """,
            (second_snapshot, multi, f"{8 + 400:064x}", _NOW.date(), _NOW),
        )
        connection.execute(
            """
            INSERT INTO evaluation_decisions (
              id, snapshot_id, pipeline_run_id, prompt_release_id, policy_version,
              outcome, matched_profile, reason, created_at
            ) VALUES (%s, %s, %s, (SELECT prompt_release_id FROM pipeline_runs WHERE id = %s),
              'policy-1', 'rejected', NULL, 'The job is explicitly hybrid in Mexico City.', %s)
            """,
            (f"{8 + 1100:064x}", second_snapshot, run_id, run_id, _NOW),
        )

        assert select_mis_titled_jobs(connection) == ()


def test_keeps_decisions_quoted_by_review_items(authority_schema: str) -> None:
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        _release_id, run_id = _seed_release_and_run(connection)
        reviewed = _seed_refused_job(
            connection, run_id, 4, "Title does not identify a role (About Acme 4)"
        )
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

        assert select_mis_titled_jobs(connection) == ()
        assert reset_mis_titled_jobs(connection, (reviewed,)) == 0

        row = connection.execute(
            "SELECT state, terminal_decision_id FROM job_work_items WHERE job_id = %s",
            (reviewed,),
        ).fetchone()
        assert row == ("completed", decision_id)


def test_a_reset_job_flows_through_claim_without_short_circuit(authority_schema: str) -> None:
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        _release_id, run_id = _seed_release_and_run(connection)
        job_id = _seed_refused_job(
            connection, run_id, 5, "Title does not identify a role (About Acme 5)"
        )

        reset = reset_mis_titled_jobs(connection, (job_id,))
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
        job_id = _seed_refused_job(
            connection, run_id, 6, "Title does not identify a role (About Acme 6)"
        )

        _ = reset_mis_titled_jobs(connection, (job_id,))

        _seed_refused_job(connection, run_id, 7, "Title does not identify a role (About Acme 7)")

        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute("DELETE FROM evaluation_decisions WHERE outcome = 'rejected'")
