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
from job_finder.evaluation import bootstrap_prompt_release
from job_finder.review.models import ReviewSaved, ReviewSubmission
from job_finder.review.postgres import (
    load_daily_review,
    prepare_daily_review,
    record_review,
    thaw_review_day,
)


@pytest.fixture
def authority_schema() -> Iterator[str]:
    settings = PostgresContractSettings.from_environment()
    schema_name = f"job_finder_thaw_contract_{uuid4().hex}"
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
        try:
            yield schema_name
        finally:
            connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema_name))
            )


def test_thaws_a_frozen_day_without_submitted_reviews_and_refreezes_the_same_decisions(
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
        _ = tuple(
            _insert_review_decision(connection, run_id, release.id, now, value, "rejected")
            for value in range(10, 18)
        )

        prepare_daily_review(connection, review_day, created_at=now)
        frozen = load_daily_review(connection, review_day)
        deleted_items, deleted_days = thaw_review_day(connection, review_day)

        assert (deleted_items, deleted_days) == (7, 1)
        assert connection.execute(
            "SELECT count(*) FROM review_items WHERE review_day = %s", (review_day,)
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM review_days WHERE review_day = %s", (review_day,)
        ).fetchone() == (0,)

        prepare_daily_review(connection, review_day, created_at=now)
        refrozen = load_daily_review(connection, review_day)

        assert {item.evaluation_id for item in refrozen.qualified.pending} == set(qualified_ids)
        assert refrozen.rejected_audit.total == frozen.rejected_audit.total == 3
        assert {item.evaluation_id for item in refrozen.rejected_audit.pending} == {
            item.evaluation_id for item in frozen.rejected_audit.pending
        }


def test_refuses_to_thaw_a_day_with_submitted_reviews(authority_schema: str) -> None:
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
        saved = record_review(
            connection,
            ReviewSubmission(
                review_item_id=item.id,
                evaluation_id=item.evaluation_id,
                snapshot_id=item.snapshot_id,
                decision="pursue",
                note="Strong fit.",
                actor="owner",
                created_at=now,
            ),
        )
        assert isinstance(saved, ReviewSaved)

        with pytest.raises(ValueError, match="2026-09-10 has submitted reviews"):
            thaw_review_day(connection, now.date())

        assert connection.execute(
            "SELECT count(*) FROM review_items WHERE review_day = %s", (now.date(),)
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM review_days WHERE review_day = %s", (now.date(),)
        ).fetchone() == (1,)


def test_a_thaw_restores_the_immutability_triggers(authority_schema: str) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    review_day = now.date()
    run_id = uuid4()
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        release = bootstrap_prompt_release(connection)
        _insert_prompt_run(connection, run_id, release.id, now)
        _insert_review_decision(connection, run_id, release.id, now, 1, "qualified")
        prepare_daily_review(connection, review_day, created_at=now)

        assert thaw_review_day(connection, review_day) == (1, 1)

        prepare_daily_review(connection, review_day, created_at=now)
        item = load_daily_review(connection, review_day).current
        assert item is not None
        with pytest.raises(psycopg.errors.CheckViolation, match="immutable"):
            connection.execute("DELETE FROM review_items WHERE id = %s", (item.id,))
        with pytest.raises(psycopg.errors.CheckViolation, match="immutable"):
            connection.execute("DELETE FROM review_days WHERE review_day = %s", (review_day,))


@contextmanager
def _connection(
    schema_name: str,
) -> Generator[psycopg.Connection[tuple[object, ...]], None, None]:
    settings = PostgresContractSettings.from_environment()
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema_name)))
        yield connection


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
