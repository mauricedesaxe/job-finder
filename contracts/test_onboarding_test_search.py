from __future__ import annotations

from collections.abc import Generator, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from job_finder.config import PostgresContractSettings
from job_finder.database import apply_migrations
from job_finder.execution_budget import (
    BudgetSaved,
    ExecutionBlocked,
    postgres_budget_setup_service,
)
from job_finder.onboarding_test_search import (
    CreateOnboardingTestSearch,
    OnboardingTestSearchAccepted,
    claim_next_onboarding_test_search,
    complete_onboarding_test_search,
    create_onboarding_test_search,
    load_onboarding_test_search,
)
from job_finder.review.owner_access import OwnerBootstrapped, postgres_owner_access_service
from job_finder.search_configuration import load_active_search_configuration


@pytest.fixture
def authority_schema() -> Iterator[str]:
    settings = PostgresContractSettings.from_environment()
    schema_name = f"job_finder_onboarding_search_{uuid4().hex}"
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
        try:
            yield schema_name
        finally:
            connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema_name))
            )


def test_concurrent_same_key_submissions_produce_one_request(authority_schema: str) -> None:
    now = datetime(2026, 9, 23, 12, tzinfo=UTC)
    _prepare_test_search_owner(authority_schema, now)
    barrier = Barrier(2)

    def submit(_index: int) -> object:
        _ = barrier.wait()
        with _connection(authority_schema) as connection:
            return create_onboarding_test_search(
                connection,
                CreateOnboardingTestSearch(
                    idempotency_key="owner-setup",
                    actor="owner",
                    timestamp=now,
                ),
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(submit, range(2)))

    accepted = [result for result in results if isinstance(result, OnboardingTestSearchAccepted)]
    assert len(accepted) == 2
    assert sum(result.replayed for result in accepted) == 1
    assert accepted[0].request.run_id == accepted[1].request.run_id
    with _connection(authority_schema) as connection:
        count = connection.execute(
            "SELECT count(*) FROM onboarding_test_search_requests"
        ).fetchone()
        assert count == (1,)
        reservations = connection.execute(
            "SELECT count(*) FROM execution_budget_reservations"
        ).fetchone()
        assert reservations == (1,)


def test_replay_keeps_pinned_provenance_after_active_config_changes(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 23, 12, tzinfo=UTC)
    _prepare_test_search_owner(authority_schema, now)
    command = CreateOnboardingTestSearch(
        idempotency_key="owner-setup",
        actor="owner",
        timestamp=now,
    )
    with _connection(authority_schema) as connection:
        created = create_onboarding_test_search(connection, command)
        assert isinstance(created, OnboardingTestSearchAccepted)
        assert created.replayed is False
        original = created.request
        _ = connection.execute(
            """
            UPDATE active_search_configuration
            SET generation = generation + 1, activated_at = %s, activated_by = 'later'
            WHERE singleton_id = 1
            """,
            (now + timedelta(hours=1),),
        )
        _ = connection.execute(
            """
            UPDATE execution_budget_policy
            SET version = version + 1, monthly_limit_usd = 900, updated_at = %s,
                updated_by = 'later'
            WHERE singleton_id = 1
            """,
            (now + timedelta(hours=1),),
        )
        replayed = create_onboarding_test_search(connection, command)

    assert isinstance(replayed, OnboardingTestSearchAccepted)
    assert replayed.replayed is True
    assert replayed.request.run_id == original.run_id
    assert replayed.request.configuration_revision_id == original.configuration_revision_id
    assert replayed.request.prompt_release_id == original.prompt_release_id
    assert replayed.request.relevance_release_id == original.relevance_release_id
    assert replayed.request.release_generation == original.release_generation
    assert replayed.request.budget_policy_version == original.budget_policy_version
    assert replayed.request.limits == original.limits


def test_leases_are_exclusive_reclaimable_and_reject_stale_owners(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 23, 12, tzinfo=UTC)
    _prepare_test_search_owner(authority_schema, now)
    first_owner = uuid4()
    second_owner = uuid4()
    with _connection(authority_schema) as connection:
        created = create_onboarding_test_search(
            connection,
            CreateOnboardingTestSearch(
                idempotency_key="owner-setup",
                actor="owner",
                timestamp=now,
            ),
        )
        assert isinstance(created, OnboardingTestSearchAccepted)
        claimed = claim_next_onboarding_test_search(
            connection,
            owner_token=first_owner,
            claimed_at=now,
            lease_for=timedelta(minutes=5),
        )
        assert claimed is not None
        assert claimed.state == "leased"
        assert claimed.owner_token == first_owner
        assert claimed.attempt_count == 1
        concurrent = claim_next_onboarding_test_search(
            connection,
            owner_token=second_owner,
            claimed_at=now,
            lease_for=timedelta(minutes=5),
        )
        assert concurrent is None
        stale = complete_onboarding_test_search(
            connection,
            run_id=created.request.run_id,
            owner_token=second_owner,
            completed_at=now + timedelta(seconds=1),
        )
        assert stale is None
        reclaimed = claim_next_onboarding_test_search(
            connection,
            owner_token=second_owner,
            claimed_at=now + timedelta(minutes=6),
            lease_for=timedelta(minutes=5),
        )
        assert reclaimed is not None
        assert reclaimed.owner_token == second_owner
        assert reclaimed.attempt_count == 2
        lost = complete_onboarding_test_search(
            connection,
            run_id=created.request.run_id,
            owner_token=first_owner,
            completed_at=now + timedelta(minutes=6),
        )
        assert lost is None
        finished = complete_onboarding_test_search(
            connection,
            run_id=created.request.run_id,
            owner_token=second_owner,
            completed_at=now + timedelta(minutes=6),
        )
        assert finished is not None
        assert finished.state == "completed"
        with pytest.raises(psycopg.errors.CheckViolation, match="terminal"):
            connection.execute(
                """
                UPDATE onboarding_test_search_requests
                SET updated_at = %s
                WHERE idempotency_key = 'owner-setup'
                """,
                (now + timedelta(minutes=7),),
            )
        with pytest.raises(psycopg.errors.CheckViolation, match="cannot be deleted"):
            connection.execute(
                "DELETE FROM onboarding_test_search_requests WHERE idempotency_key = 'owner-setup'"
            )


def test_attempt_exhaustion_fails_the_request(authority_schema: str) -> None:
    now = datetime(2026, 9, 23, 12, tzinfo=UTC)
    _prepare_test_search_owner(authority_schema, now)
    with _connection(authority_schema) as connection:
        created = create_onboarding_test_search(
            connection,
            CreateOnboardingTestSearch(
                idempotency_key="owner-setup",
                actor="owner",
                timestamp=now,
            ),
        )
        assert isinstance(created, OnboardingTestSearchAccepted)
        claimed_at = now
        for _ in range(created.request.limits.max_work_attempts):
            owner = uuid4()
            claimed = claim_next_onboarding_test_search(
                connection,
                owner_token=owner,
                claimed_at=claimed_at,
                lease_for=timedelta(minutes=1),
            )
            assert claimed is not None
            claimed_at = claimed_at + timedelta(minutes=2)
        exhausted = claim_next_onboarding_test_search(
            connection,
            owner_token=uuid4(),
            claimed_at=claimed_at,
            lease_for=timedelta(minutes=1),
        )
        assert exhausted is None
        stored = load_onboarding_test_search(connection, "owner-setup")
        assert stored is not None
        assert stored.state == "failed"
        assert stored.error_code == "worker_attempts_exhausted"


def test_create_is_blocked_before_test_search_stage(authority_schema: str) -> None:
    now = datetime(2026, 9, 23, 12, tzinfo=UTC)
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        blocked = create_onboarding_test_search(
            connection,
            CreateOnboardingTestSearch(
                idempotency_key="owner-setup",
                actor="owner",
                timestamp=now,
            ),
        )
    assert blocked == ExecutionBlocked(reason="onboarding_incomplete")


def _prepare_test_search_owner(schema_name: str, timestamp: datetime) -> None:
    with _connection(schema_name) as connection:
        apply_migrations(connection)
        _ = load_active_search_configuration(connection)
    owner = postgres_owner_access_service(lambda: _connection(schema_name))
    bootstrapped = owner.bootstrap("first secure owner password")
    assert isinstance(bootstrapped, OwnerBootstrapped)
    with _connection(schema_name) as connection:
        _ = connection.execute(
            """
            UPDATE owner_onboarding
            SET stage = 'preferences', updated_at = %s
            WHERE singleton_id = 1
            """,
            (timestamp,),
        )
        _ = connection.execute(
            """
            UPDATE owner_onboarding
            SET stage = 'budget', updated_at = %s
            WHERE singleton_id = 1
            """,
            (timestamp,),
        )
    saved = postgres_budget_setup_service(lambda: _connection(schema_name)).save(
        0,
        Decimal("500"),
        Decimal("50"),
        10,
        "owner",
        timestamp,
    )
    assert isinstance(saved, BudgetSaved)


@contextmanager
def _connection(
    schema_name: str,
) -> Generator[psycopg.Connection[tuple[object, ...]], None, None]:
    settings = PostgresContractSettings.from_environment()
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema_name)))
        yield connection
