from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TypedDict, cast
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
from job_finder.acquisition_policy_activation import (
    ActivateAcquisitionPolicyCommand,
    activate_acquisition_policy,
)
from job_finder.ats.models import AtsNotApplicable
from job_finder.config import PostgresContractSettings
from job_finder.database import apply_migrations
from job_finder.discovery.exchange_rates import ExchangeRateSnapshot
from job_finder.discovery.jina import JinaUnavailable, SearchSucceeded
from job_finder.execution_budget import (
    ExecutionBlocked,
    SplitExecutionAdmitted,
    admit_scheduled_execution,
)
from job_finder.evaluation.implementation_artifacts import write_implementation_artifact
from job_finder.evaluation.qualification_components import (
    QualificationTargetId,
    qualification_target_id,
)
from job_finder.evaluation.qualification_prompt_compilations import (
    bind_qualification_prompt_release,
)
from job_finder.evaluation.release_targets import get_active_release_target
from job_finder.search_configuration import load_active_search_configuration
from job_finder.pipeline.orchestration import PipelineBoundaries, discover_jobs
from job_finder.onboarding_test_search import (
    CreateOnboardingTestSearch,
    OnboardingTestSearchAccepted,
    SplitOnboardingTestSearchRequest,
    create_onboarding_test_search,
    load_onboarding_test_search,
)
from job_finder.onboarding_test_search_worker import execute_next_onboarding_test_search
from job_finder.pipeline.runs import (
    SplitOrchestrationRun,
    fail_orchestration_run,
    load_run_by_id,
    prepare_orchestration_run,
    prepare_split_onboarding_run,
    prepare_split_orchestration_run,
)


class _SplitRunArgs(TypedDict):
    idempotency_key: str
    implementation_ref: str
    acquisition_policy_revision_id: AcquisitionPolicyRevisionId
    qualification_target_id: QualificationTargetId
    artifact_path: Path
    started_at: datetime


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


def _seed_test_search_owner(
    connection: psycopg.Connection[tuple[object, ...]], now: datetime
) -> None:
    _ = connection.execute("TRUNCATE owner_onboarding")
    _ = connection.execute(
        """
        INSERT INTO owner_onboarding (singleton_id, stage, password_hash)
        VALUES (1, 'test_search', 'scrypt$v=1$fixture')
        """
    )
    _ = connection.execute(
        """
        INSERT INTO execution_budget_policy (
          singleton_id, version, monthly_limit_usd, run_allowance_usd,
          max_jobs_per_run, max_search_queries_per_run,
          max_provider_attempts_per_run, updated_at, updated_by
        ) VALUES (1, 1, 500, 50, 5, 10000, 1000000, %s, 'owner')
        """,
        (now,),
    )


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


def test_scheduled_split_admission_blocks_until_target_is_active() -> None:
    now = datetime(2026, 9, 25, tzinfo=UTC)
    with _schema() as connection:
        _ = apply_migrations(connection)
        _ = connection.execute("TRUNCATE owner_onboarding")
        _ = connection.execute(
            "INSERT INTO owner_onboarding (singleton_id, stage) VALUES (1, 'legacy_owner_import')"
        )
        _ = connection.execute(
            """
            INSERT INTO execution_budget_policy (
                singleton_id, version, monthly_limit_usd, run_allowance_usd,
                max_jobs_per_run, max_search_queries_per_run,
                max_provider_attempts_per_run, updated_at, updated_by
                ) VALUES (1, 1, 500, 50, 5, 10000, 1000000, %s, 'owner')
            ON CONFLICT (singleton_id) DO UPDATE SET
                max_jobs_per_run = 5, max_search_queries_per_run = 10000,
                max_provider_attempts_per_run = 1000000
            """,
            (now,),
        )
        root = Path(__file__).resolve().parents[1]
        with NamedTemporaryFile(dir=root, suffix=".json") as temporary:
            artifact_path = Path(temporary.name)
            _ = write_implementation_artifact(root, artifact_path)
            blocked = admit_scheduled_execution(
                connection,
                idempotency_key="scheduled-split",
                requested_at=now,
                artifact_path=artifact_path,
            )
            assert blocked == ExecutionBlocked(reason="qualification_target_not_active")
            _, _, target = _store_default_qualification_target(connection, now)
            target_id = qualification_target_id(target)
            _ = bind_qualification_prompt_release(
                connection, target_id, artifact_path, created_at=now, created_by="owner"
            )
            _ = connection.execute(
                """
                UPDATE active_qualification_target
                SET target_id = %s, generation = 1, activated_at = %s, activated_by = 'owner'
                WHERE singleton_id = 1
                """,
                (target_id, now),
            )
            admitted = admit_scheduled_execution(
                connection,
                idempotency_key="scheduled-split",
                requested_at=now,
                artifact_path=artifact_path,
            )
            assert isinstance(admitted, SplitExecutionAdmitted)
            assert admitted.qualification_target_id == target_id
            replayed = admit_scheduled_execution(
                connection, idempotency_key="scheduled-split", requested_at=now
            )
            assert replayed == admitted
            row = connection.execute(
                """
                SELECT authority_kind, configuration_revision_id, qualification_target_id,
                       qualification_generation
                FROM execution_budget_reservations WHERE idempotency_key = 'scheduled-split'
                """
            ).fetchone()
            assert row == ("split", None, target_id, 1)


def test_split_onboarding_request_matches_immutable_reservation() -> None:
    now = datetime(2026, 9, 25, tzinfo=UTC)
    with _schema() as connection:
        _ = apply_migrations(connection)
        _, _, target = _store_default_qualification_target(connection, now)
        target_id = qualification_target_id(target)
        acquisition = connection.execute(
            "SELECT revision_id, generation FROM active_acquisition_policy WHERE singleton_id = 1"
        ).fetchone()
        assert acquisition is not None
        _ = connection.execute(
            """
            INSERT INTO execution_budget_reservations (
                idempotency_key, policy_version, period_start, reserved_usd,
                status, max_jobs, authority_kind, acquisition_policy_revision_id,
                qualification_target_id, acquisition_generation, qualification_generation,
                search_queries, logical_model_calls_per_job,
                maximum_provider_attempts, created_at
            ) VALUES ('split-onboarding', 1, %s, 1, 'reserved', 1, 'split',
                      %s, %s, %s, 1, 1, 1, 1, %s)
            """,
            (now.date(), acquisition[0], target_id, acquisition[1], now),
        )
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            _ = connection.execute(
                """
                INSERT INTO onboarding_test_search_requests (
                    idempotency_key, run_id, state, execution_authority_kind,
                    acquisition_policy_revision_id, qualification_target_id,
                    acquisition_generation, qualification_generation,
                    budget_policy_version, budget_reservation_key, max_queries, max_urls,
                    max_jobs, max_work_attempts, max_provider_attempts, run_allowance_usd,
                    created_at, updated_at
                ) VALUES ('wrong-onboarding', %s, 'pending', 'split', %s, %s, %s, 2,
                          1, 'split-onboarding', 1, 1, 1, 1, 1, 1, %s, %s)
                """,
                (uuid4(), acquisition[0], target_id, acquisition[1], now, now),
            )
        request_id = uuid4()
        _ = connection.execute(
            """
            INSERT INTO onboarding_test_search_requests (
                idempotency_key, run_id, state, execution_authority_kind,
                acquisition_policy_revision_id, qualification_target_id,
                acquisition_generation, qualification_generation,
                budget_policy_version, budget_reservation_key, max_queries, max_urls,
                max_jobs, max_work_attempts, max_provider_attempts, run_allowance_usd,
                created_at, updated_at
            ) VALUES ('split-onboarding', %s, 'pending', 'split', %s, %s, %s, 1,
                      1, 'split-onboarding', 1, 1, 1, 1, 1, 1, %s, %s)
            """,
            (request_id, acquisition[0], target_id, acquisition[1], now, now),
        )
        stored = connection.execute(
            """
            SELECT execution_authority_kind, configuration_revision_id,
                   acquisition_policy_revision_id, qualification_target_id
            FROM onboarding_test_search_requests WHERE idempotency_key = 'split-onboarding'
            """
        ).fetchone()
        assert stored == ("split", None, acquisition[0], target_id)
        with pytest.raises(psycopg.errors.CheckViolation):
            _ = connection.execute(
                """
                UPDATE onboarding_test_search_requests SET qualification_generation = 2
                WHERE idempotency_key = 'split-onboarding'
                """
            )


def test_split_onboarding_run_retries_with_pinned_authority_and_rates() -> None:
    now = datetime(2026, 9, 25, tzinfo=UTC)
    rates = ExchangeRateSnapshot(rates={"EUR": Decimal("1.12")}, source="fallback", observed_at=now)
    with _schema() as connection:
        _ = apply_migrations(connection)
        _, _, target = _store_default_qualification_target(connection, now)
        target_id = qualification_target_id(target)
        acquisition_id = _store_alternate_acquisition_policy(connection, now)
        root = Path(__file__).resolve().parents[1]
        with NamedTemporaryFile(dir=root, suffix=".json") as temporary:
            artifact_path = Path(temporary.name)
            _ = write_implementation_artifact(root, artifact_path)
            _ = bind_qualification_prompt_release(
                connection, target_id, artifact_path, created_at=now, created_by="owner"
            )
            run_id = uuid4()
            first = prepare_split_onboarding_run(
                connection,
                run_id=run_id,
                request_key="split-onboarding-run",
                implementation_ref="build",
                acquisition_policy_revision_id=acquisition_id,
                qualification_target_id=target_id,
                artifact_path=artifact_path,
                started_at=now,
                fetch_rates=lambda: rates,
            )
            assert first.id == run_id
            assert first.acquisition_policy_revision_id == acquisition_id
            assert first.qualification_target_id == target_id
            fail_orchestration_run(
                connection,
                run_id,
                completed_at=now,
                error_code="interrupted",
                reason="retry",
            )
            resumed = prepare_split_onboarding_run(
                connection,
                run_id=run_id,
                request_key="split-onboarding-run",
                implementation_ref="build",
                acquisition_policy_revision_id=acquisition_id,
                qualification_target_id=target_id,
                artifact_path=artifact_path,
                started_at=now,
                fetch_rates=lambda: pytest.fail("rates were refetched"),
            )
            assert resumed.id == first.id
            assert resumed.status == "running"
            assert resumed.exchange_rates == rates
            with pytest.raises(ValueError, match="provenance"):
                _ = prepare_split_onboarding_run(
                    connection,
                    run_id=run_id,
                    request_key="split-onboarding-run",
                    implementation_ref="build",
                    acquisition_policy_revision_id=AcquisitionPolicyRevisionId("a" * 64),
                    qualification_target_id=target_id,
                    artifact_path=artifact_path,
                    started_at=now,
                    fetch_rates=lambda: pytest.fail("rates were refetched"),
                )


def _assert_candidate_key_conflict(
    connection: psycopg.Connection[tuple[object, ...]], artifact_path: Path, now: datetime
) -> None:
    with pytest.raises(ValueError, match="another candidate"):
        _ = create_onboarding_test_search(
            connection,
            CreateOnboardingTestSearch(
                idempotency_key="split-worker",
                actor="owner",
                timestamp=now,
                candidate_target_id=QualificationTargetId("a" * 64),
            ),
            artifact_path=artifact_path,
        )


def _assert_missing_candidate_blocks(
    connection: psycopg.Connection[tuple[object, ...]], artifact_path: Path, now: datetime
) -> None:
    missing = create_onboarding_test_search(
        connection,
        CreateOnboardingTestSearch(idempotency_key="split-worker", actor="owner", timestamp=now),
        artifact_path=artifact_path,
    )
    assert missing == ExecutionBlocked(reason="qualification_candidate_required")


def test_split_onboarding_worker_uses_reserved_policy_after_active_changes() -> None:
    now = datetime(2026, 9, 25, tzinfo=UTC)
    searched: list[str] = []
    with _schema() as connection:
        _ = apply_migrations(connection)
        _seed_test_search_owner(connection, now)
        _, _, target = _store_default_qualification_target(connection, now)
        target_id = qualification_target_id(target)
        alternate_id = _store_alternate_acquisition_policy(connection, now)
        original = connection.execute(
            "SELECT revision_id FROM active_acquisition_policy WHERE singleton_id = 1"
        ).fetchone()
        assert original is not None
        _ = connection.execute(
            """
            INSERT INTO acquisition_policy_publications (revision_id, published_at, published_by)
            VALUES (%s, %s, 'owner')
            """,
            (alternate_id, now),
        )
        _ = activate_acquisition_policy(
            connection,
            ActivateAcquisitionPolicyCommand(
                idempotency_key="activate-split-worker",
                candidate_revision_id=alternate_id,
                expected_revision_id=AcquisitionPolicyRevisionId(cast(str, original[0])),
                expected_generation=0,
                actor="owner",
                timestamp=now,
            ),
        )
        root = Path(__file__).resolve().parents[1]
        with NamedTemporaryFile(dir=root, suffix=".json") as temporary:
            artifact_path = Path(temporary.name)
            _ = write_implementation_artifact(root, artifact_path)
            _ = bind_qualification_prompt_release(
                connection, target_id, artifact_path, created_at=now, created_by="owner"
            )
            _assert_missing_candidate_blocks(connection, artifact_path, now)
            created = create_onboarding_test_search(
                connection,
                CreateOnboardingTestSearch(
                    idempotency_key="split-worker",
                    actor="owner",
                    timestamp=now,
                    candidate_target_id=target_id,
                ),
                artifact_path=artifact_path,
            )
            assert isinstance(created, OnboardingTestSearchAccepted)
            assert isinstance(created.request, SplitOnboardingTestSearchRequest)
            assert created.request.acquisition_policy_revision_id == alternate_id
            assert created.request.qualification_generation == 0
            _assert_candidate_key_conflict(connection, artifact_path, now)
            _ = activate_acquisition_policy(
                connection,
                ActivateAcquisitionPolicyCommand(
                    idempotency_key="restore-split-worker",
                    candidate_revision_id=AcquisitionPolicyRevisionId(cast(str, original[0])),
                    expected_revision_id=alternate_id,
                    expected_generation=1,
                    actor="owner",
                    timestamp=now,
                ),
            )
            result = execute_next_onboarding_test_search(
                connection,
                PipelineBoundaries(
                    search=lambda keyword, _domain: (
                        searched.append(keyword) or SearchSucceeded(urls=())
                    ),
                    scrape=lambda _url: JinaUnavailable(
                        operation="scrape", error_code="unused", reason="unused"
                    ),
                    fetch_ats=lambda _url, _title: AtsNotApplicable(),
                ),
                implementation_ref="build",
                openrouter_api_key="unused",
                typesafe_api_key=None,
                owner_token=uuid4(),
                lease_for=timedelta(minutes=5),
                retry_after=timedelta(minutes=1),
                enable_ats_enrichment=False,
                fetch_rates=lambda: ExchangeRateSnapshot(
                    rates={"EUR": Decimal("1.1")}, source="fallback", observed_at=now
                ),
                artifact_path=artifact_path,
                now=lambda: now,
            )
            stored = load_onboarding_test_search(connection, "split-worker")
            assert result.state == "completed"
            assert searched == ["split policy"]
            assert isinstance(stored, SplitOnboardingTestSearchRequest)
            assert stored.acquisition_policy_revision_id == alternate_id


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


def test_split_run_creation_and_retry_keep_reserved_authority() -> None:
    now = datetime(2026, 9, 25, tzinfo=UTC)
    with _schema() as connection:
        _ = apply_migrations(connection)
        _, _, target = _store_default_qualification_target(connection, now)
        qualification_id = qualification_target_id(target)
        _ = _compiled_release(connection, qualification_id, now)
        active = connection.execute(
            "SELECT revision_id, generation FROM active_acquisition_policy WHERE singleton_id = 1"
        ).fetchone()
        assert active is not None
        acquisition_id = AcquisitionPolicyRevisionId(cast(str, active[0]))
        key = f"split-run-{uuid4()}"
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
            (key, now.date(), acquisition_id, qualification_id, active[1], now),
        )
        root = Path(__file__).resolve().parents[1]
        with NamedTemporaryFile(
            dir=root, prefix=".qualification-artifact-", suffix=".json"
        ) as temporary:
            artifact_path = Path(temporary.name)
            _ = write_implementation_artifact(root, artifact_path)
            arguments: _SplitRunArgs = {
                "idempotency_key": key,
                "implementation_ref": "build",
                "acquisition_policy_revision_id": acquisition_id,
                "qualification_target_id": qualification_id,
                "artifact_path": artifact_path,
                "started_at": now,
            }
            first = prepare_split_orchestration_run(
                connection,
                **arguments,
                fetch_rates=lambda: ExchangeRateSnapshot(
                    rates={"USD": Decimal("1")}, source="fallback", observed_at=now
                ),
            )
            assert first.acquisition_policy_revision_id == acquisition_id
            assert first.qualification_target_id == qualification_id
            with pytest.raises(ValueError, match="another execution authority"):
                _ = prepare_split_orchestration_run(
                    connection,
                    idempotency_key=key,
                    implementation_ref="build",
                    acquisition_policy_revision_id=AcquisitionPolicyRevisionId("a" * 64),
                    qualification_target_id=qualification_id,
                    artifact_path=artifact_path,
                    started_at=now,
                    fetch_rates=lambda: pytest.fail("Rates should remain pinned"),
                )
            fail_orchestration_run(
                connection, first.id, completed_at=now, error_code="retry", reason="retry"
            )
            resumed = prepare_split_orchestration_run(
                connection,
                **arguments,
                fetch_rates=lambda: pytest.fail("Rates should remain pinned"),
            )
            assert resumed.id == first.id and resumed.status == "running"
            assert resumed.acquisition_policy_revision_id == acquisition_id
            assert resumed.qualification_target_id == qualification_id


def test_run_keys_never_cross_execution_authorities() -> None:
    now = datetime(2026, 9, 25, tzinfo=UTC)
    with _schema() as connection:
        _ = apply_migrations(connection)
        _, _, target = _store_default_qualification_target(connection, now)
        qualification_id = qualification_target_id(target)
        _ = _compiled_release(connection, qualification_id, now)
        active = connection.execute(
            "SELECT revision_id, generation FROM active_acquisition_policy WHERE singleton_id = 1"
        ).fetchone()
        assert active is not None
        acquisition_id = AcquisitionPolicyRevisionId(cast(str, active[0]))
        configuration = load_active_search_configuration(connection)
        legacy_target = get_active_release_target(connection).target

        def rates() -> ExchangeRateSnapshot:
            return ExchangeRateSnapshot(
                rates={"USD": Decimal("1")}, source="fallback", observed_at=now
            )

        root = Path(__file__).resolve().parents[1]
        with NamedTemporaryFile(
            dir=root, prefix=".qualification-artifact-", suffix=".json"
        ) as temporary:
            artifact_path = Path(temporary.name)
            _ = write_implementation_artifact(root, artifact_path)
            _ = connection.execute(
                """
                INSERT INTO execution_budget_reservations (
                    idempotency_key, policy_version, period_start, reserved_usd,
                    status, max_jobs, authority_kind, acquisition_policy_revision_id,
                    qualification_target_id, acquisition_generation, qualification_generation,
                    search_queries, logical_model_calls_per_job,
                    maximum_provider_attempts, created_at
                ) VALUES ('split-key', 1, %s, 1, 'reserved', 1, 'split', %s, %s, %s, 1,
                          1, 1, 1, %s)
                """,
                (now.date(), acquisition_id, qualification_id, active[1], now),
            )
            legacy = prepare_orchestration_run(
                connection,
                idempotency_key="shared-key",
                implementation_ref="build",
                configuration_revision_id=configuration.revision.id,
                target=legacy_target,
                started_at=now,
                fetch_rates=rates,
            )
            assert legacy.authority_kind == "legacy"
            with pytest.raises(ValueError, match="another execution authority"):
                _ = prepare_split_orchestration_run(
                    connection,
                    idempotency_key="shared-key",
                    implementation_ref="build",
                    acquisition_policy_revision_id=acquisition_id,
                    qualification_target_id=qualification_id,
                    artifact_path=artifact_path,
                    started_at=now,
                    fetch_rates=lambda: pytest.fail("Rates must not be re-fetched"),
                )

            _ = prepare_split_orchestration_run(
                connection,
                idempotency_key="split-key",
                implementation_ref="build",
                acquisition_policy_revision_id=acquisition_id,
                qualification_target_id=qualification_id,
                artifact_path=artifact_path,
                started_at=now,
                fetch_rates=rates,
            )
            with pytest.raises(ValueError, match="another execution authority"):
                _ = prepare_orchestration_run(
                    connection,
                    idempotency_key="split-key",
                    implementation_ref="build",
                    configuration_revision_id=configuration.revision.id,
                    target=legacy_target,
                    started_at=now,
                    fetch_rates=lambda: pytest.fail("Rates must not be re-fetched"),
                )
