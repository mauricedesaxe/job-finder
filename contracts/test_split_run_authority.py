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
from job_finder.acquisition_policy import (
    AcquisitionPolicyRevisionId,
    acquisition_policy_revision_id,
)
from job_finder.acquisition_policy_service import load_acquisition_policy_revision
from job_finder.ats.models import AtsNotApplicable
from job_finder.config import PostgresContractSettings
from job_finder.database import apply_migrations
from job_finder.discovery.jina import JinaUnavailable, SearchSucceeded
from job_finder.evaluation.implementation_artifacts import write_implementation_artifact
from job_finder.evaluation.qualification_components import (
    QualificationTargetId,
    qualification_target_id,
)
from job_finder.evaluation.qualification_prompt_compilations import (
    bind_qualification_prompt_release,
)
from job_finder.pipeline.runs import SplitOrchestrationRun, load_run_by_id
from job_finder.pipeline.orchestration import PipelineBoundaries, discover_jobs


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


def _store_alternate_acquisition_policy(
    connection: psycopg.Connection[tuple[object, ...]], now: datetime
) -> AcquisitionPolicyRevisionId:
    active = connection.execute(
        "SELECT revision_id FROM active_acquisition_policy WHERE singleton_id = 1"
    ).fetchone()
    assert active is not None
    policy = load_acquisition_policy_revision(
        connection, AcquisitionPolicyRevisionId(str(active[0]))
    ).policy
    different = policy.model_copy(
        update={
            "search_keywords": ("split policy",),
            "enabled_sources": (policy.enabled_sources[0],),
        }
    )
    revision_id = acquisition_policy_revision_id(different)
    _ = connection.execute(
        """
        INSERT INTO acquisition_policy_revisions (id, content, created_at, created_by)
        VALUES (%s, %s, %s, 'owner')
        """,
        (revision_id, Jsonb(different.model_dump(mode="json")), now),
    )
    return revision_id


def _assert_split_discovery(
    connection: psycopg.Connection[tuple[object, ...]], run: SplitOrchestrationRun, now: datetime
) -> None:
    searches: list[tuple[str, str]] = []

    def search(keyword: str, domain: str) -> SearchSucceeded:
        searches.append((keyword, domain))
        return SearchSucceeded(urls=())

    discovery = discover_jobs(
        connection,
        run,
        PipelineBoundaries(
            search=search,
            scrape=lambda _url: JinaUnavailable(
                operation="scrape", error_code="unused", reason="unused"
            ),
            fetch_ats=lambda _url, _title: AtsNotApplicable(),
        ),
        discovered_at=now,
        max_workers=1,
    )
    assert discovery.query_count == 1
    assert len(searches) == 1 and searches[0][0] == "split policy"


def test_split_run_keeps_independent_authority_and_legacy_columns_separate() -> None:
    now = datetime(2026, 9, 25, tzinfo=UTC)
    with _schema() as connection:
        _ = apply_migrations(connection)
        _, components, target = _store_default_qualification_target(connection, now)
        qualification_id = qualification_target_id(target)
        prompt_id = _compiled_release(connection, qualification_id, now)
        relevance_id = components[1].relevance_release_id
        acquisition_id = _store_alternate_acquisition_policy(connection, now)
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
                    acquisition_id,
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
        assert row == ("split", None, prompt_id, relevance_id, acquisition_id, qualification_id)
        loaded = load_run_by_id(connection, run_id)
        assert isinstance(loaded, SplitOrchestrationRun)
        assert loaded.acquisition_policy_revision_id == acquisition_id
        assert loaded.qualification_target_id == qualification_id
        assert loaded.target.prompt_release_id == prompt_id
        _assert_split_discovery(connection, loaded, now)
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
