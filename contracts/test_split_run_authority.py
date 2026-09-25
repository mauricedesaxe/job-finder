from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from contracts.test_postgres_authority import _store_default_qualification_target  # pyright: ignore[reportPrivateUsage]
from job_finder.config import PostgresContractSettings
from job_finder.database import apply_migrations
from job_finder.evaluation.implementation_artifacts import write_implementation_artifact
from job_finder.evaluation.qualification_components import (
    QualificationTargetId,
    qualification_target_id,
)
from job_finder.evaluation.qualification_prompt_compilations import (
    bind_qualification_prompt_release,
)


@contextmanager
def _schema():
    settings = PostgresContractSettings.from_environment()
    name = f"job_finder_split_run_{uuid4().hex}"
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as connection:
        _ = connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
        try:
            _ = connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(name)))
            yield connection
        finally:
            _ = connection.execute("SET search_path TO public")
            _ = connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


def _compiled_release(
    connection: psycopg.Connection[tuple[object, ...]],
    target_id: QualificationTargetId,
    now: datetime,
) -> str:
    root = Path(__file__).resolve().parents[1]
    with NamedTemporaryFile(
        dir=root, prefix=".qualification-artifact-", suffix=".json"
    ) as temporary:
        artifact_path = Path(temporary.name)
        _ = write_implementation_artifact(root, artifact_path)
        return str(
            bind_qualification_prompt_release(
                connection, target_id, artifact_path, created_at=now, created_by="owner"
            )
        )


def test_split_run_keeps_independent_authority_and_legacy_columns_separate() -> None:
    now = datetime(2026, 9, 25, tzinfo=UTC)
    with _schema() as connection:
        _ = apply_migrations(connection)
        _, components, target = _store_default_qualification_target(connection, now)
        qualification_id = qualification_target_id(target)
        prompt_id = _compiled_release(connection, qualification_id, now)
        relevance_id = components[1].relevance_release_id
        acquisition = connection.execute(
            "SELECT revision_id FROM active_acquisition_policy WHERE singleton_id = 1"
        ).fetchone()
        assert acquisition is not None
        run_id = uuid4()
        with connection.transaction():
            _ = connection.execute(
                """
                INSERT INTO pipeline_runs (
                    id, idempotency_key, kind, implementation_ref, parameters,
                    status, started_at, execution_authority_kind,
                    acquisition_policy_revision_id, qualification_target_id,
                    prompt_release_id, relevance_release_id
                ) VALUES (%s, %s, 'onboarding', 'build', '{}'::jsonb, 'running', %s,
                          'split', %s, %s, %s, %s)
                """,
                (
                    run_id,
                    str(run_id),
                    now,
                    acquisition[0],
                    qualification_id,
                    prompt_id,
                    relevance_id,
                ),
            )
            _ = connection.execute(
                """
                INSERT INTO run_exchange_rate_snapshots (
                    pipeline_run_id, content_digest, rates, source, observed_at
                ) VALUES (%s, %s, %s, 'fallback', %s)
                """,
                (run_id, "a" * 64, Jsonb({"USD": "1"}), now),
            )
        row = connection.execute(
            """
            SELECT execution_authority_kind, configuration_revision_id,
                   prompt_release_id, relevance_release_id,
                   acquisition_policy_revision_id, qualification_target_id
            FROM pipeline_runs WHERE id = %s
            """,
            (run_id,),
        ).fetchone()
        assert row == ("split", None, prompt_id, relevance_id, acquisition[0], qualification_id)
        with pytest.raises(psycopg.errors.CheckViolation):
            _ = connection.execute(
                """
                UPDATE pipeline_runs SET configuration_revision_id = (
                    SELECT revision_id FROM active_search_configuration WHERE singleton_id = 1
                ) WHERE id = %s
                """,
                (run_id,),
            )


def test_split_reservation_pins_matching_orchestration_run() -> None:
    now = datetime(2026, 9, 25, tzinfo=UTC)
    with _schema() as connection:
        _ = apply_migrations(connection)
        _, components, target = _store_default_qualification_target(connection, now)
        acquisition = connection.execute(
            "SELECT revision_id, generation FROM active_acquisition_policy WHERE singleton_id = 1"
        ).fetchone()
        assert acquisition is not None
        qualification_id = qualification_target_id(target)
        prompt_id = _compiled_release(connection, qualification_id, now)
        relevance_id = components[1].relevance_release_id
        run_id = uuid4()
        key = str(run_id)
        _ = connection.execute(
            """
            INSERT INTO execution_budget_reservations (
                idempotency_key, policy_version, period_start, reserved_usd,
                status, max_jobs, authority_kind, acquisition_policy_revision_id,
                qualification_target_id, acquisition_generation, qualification_generation,
                search_queries, logical_model_calls_per_job,
                maximum_provider_attempts, created_at
            ) VALUES (%s, 1, %s, 1, 'reserved', 1, 'split', %s, %s, %s, 1,
                      1, 1, 1, %s)
            """,
            (key, now.date(), acquisition[0], qualification_id, acquisition[1], now),
        )
        with connection.transaction():
            _ = connection.execute(
                """
                INSERT INTO pipeline_runs (
                    id, idempotency_key, kind, implementation_ref, parameters,
                    status, started_at, execution_authority_kind,
                    acquisition_policy_revision_id, qualification_target_id,
                    prompt_release_id, relevance_release_id
                ) VALUES (%s, %s, 'orchestration', 'build', '{}'::jsonb, 'running',
                          %s, 'split', %s, %s, %s, %s)
                """,
                (run_id, key, now, acquisition[0], qualification_id, prompt_id, relevance_id),
            )
            _ = connection.execute(
                """
                INSERT INTO run_exchange_rate_snapshots (
                    pipeline_run_id, content_digest, rates, source, observed_at
                ) VALUES (%s, %s, %s, 'fallback', %s)
                """,
                (run_id, "a" * 64, Jsonb({"USD": "1"}), now),
            )
            _ = connection.execute(
                """
                UPDATE execution_budget_reservations
                SET pipeline_run_id = %s WHERE idempotency_key = %s
                """,
                (run_id, key),
            )
        assert connection.execute(
            "SELECT pipeline_run_id FROM execution_budget_reservations WHERE idempotency_key = %s",
            (key,),
        ).fetchone() == (run_id,)
        with pytest.raises(psycopg.errors.CheckViolation):
            _ = connection.execute(
                """
                UPDATE execution_budget_reservations
                SET qualification_target_id = NULL WHERE idempotency_key = %s
                """,
                (key,),
            )
