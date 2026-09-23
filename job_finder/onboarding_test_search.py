from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, ClassVar, Literal, TypeAlias, cast
from uuid import NAMESPACE_URL, UUID, uuid5

import psycopg
from pydantic import BaseModel, ConfigDict, Field

from job_finder.configuration_service import load_published_active_search_configuration
from job_finder.evaluation.models import PromptReleaseId, RelevanceReleaseId
from job_finder.execution_budget import (
    ExecutionAdmitted,
    ExecutionBlocked,
    ExecutionBudgetPolicy,
    admit_onboarding_test_execution,
    estimate_execution,
)
from job_finder.pipeline.state import JOB_WORK_ATTEMPT_LIMIT
from job_finder.review.owner_access import OnboardingStage
from job_finder.search_configuration import SearchConfiguration, SearchConfigurationRevisionId

Connection = psycopg.Connection[tuple[object, ...]]
RESERVATION_KEY_PREFIX = "onboarding-test-search:"
URLS_PER_JOB = 4
ONBOARDING_TEST_SEARCH_NAMESPACE = uuid5(NAMESPACE_URL, "job-finder:onboarding-test-search")


class OnboardingTestSearchModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class OnboardingTestSearchLimits(OnboardingTestSearchModel):
    max_queries: int = Field(ge=1)
    max_urls: int = Field(ge=1)
    max_jobs: int = Field(ge=1)
    max_work_attempts: int = Field(ge=1)
    max_provider_attempts: int = Field(ge=1)
    run_allowance_usd: Decimal = Field(gt=0, max_digits=18, decimal_places=8)

    @classmethod
    def from_policy(
        cls, policy: ExecutionBudgetPolicy, configuration: SearchConfiguration
    ) -> OnboardingTestSearchLimits:
        estimate = estimate_execution(configuration, policy.max_jobs_per_run)
        return cls(
            max_queries=estimate.search_queries,
            max_urls=policy.max_jobs_per_run * URLS_PER_JOB,
            max_jobs=policy.max_jobs_per_run,
            max_work_attempts=JOB_WORK_ATTEMPT_LIMIT,
            max_provider_attempts=estimate.maximum_provider_attempts,
            run_allowance_usd=policy.run_allowance_usd,
        )


class CreateOnboardingTestSearch(OnboardingTestSearchModel):
    idempotency_key: str = Field(min_length=1, max_length=200)
    actor: str = Field(min_length=1, max_length=200)
    timestamp: datetime


class OnboardingTestSearchRequest(OnboardingTestSearchModel):
    idempotency_key: str = Field(min_length=1, max_length=200)
    run_id: UUID
    state: Literal["pending", "leased", "completed", "failed"]
    configuration_revision_id: Annotated[
        SearchConfigurationRevisionId, Field(pattern=r"^[0-9a-f]{64}$")
    ]
    prompt_release_id: Annotated[PromptReleaseId, Field(pattern=r"^[0-9a-f]{64}$")]
    relevance_release_id: Annotated[RelevanceReleaseId, Field(pattern=r"^[0-9a-f]{64}$")]
    release_generation: int = Field(ge=0)
    budget_policy_version: int = Field(ge=1)
    budget_reservation_key: str = Field(min_length=1)
    limits: OnboardingTestSearchLimits
    attempt_count: int = Field(ge=0)
    owner_token: UUID | None
    lease_expires_at: datetime | None
    error_code: str | None
    error_reason: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class OnboardingTestSearchAccepted(OnboardingTestSearchModel):
    kind: Literal["accepted"] = "accepted"
    replayed: bool
    request: OnboardingTestSearchRequest


CreateOnboardingTestSearchResult: TypeAlias = OnboardingTestSearchAccepted | ExecutionBlocked


def onboarding_test_search_run_id(idempotency_key: str) -> UUID:
    return uuid5(ONBOARDING_TEST_SEARCH_NAMESPACE, idempotency_key)


def onboarding_test_search_reservation_key(idempotency_key: str) -> str:
    return f"{RESERVATION_KEY_PREFIX}{idempotency_key}"


def create_onboarding_test_search(
    connection: Connection, command: CreateOnboardingTestSearch
) -> CreateOnboardingTestSearchResult:
    _require_autocommit(connection)
    with connection.transaction():
        _ = connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
            ("onboarding_test_search", command.idempotency_key),
        )
        existing = _load_request(connection, command.idempotency_key)
        if existing is not None:
            return OnboardingTestSearchAccepted(replayed=True, request=existing)
        owner_row = connection.execute(
            "SELECT stage FROM owner_onboarding WHERE singleton_id = 1 FOR UPDATE"
        ).fetchone()
        if (
            owner_row is None
            or OnboardingStage(str(owner_row[0])) is not OnboardingStage.TEST_SEARCH
        ):
            return ExecutionBlocked(reason="onboarding_incomplete")
        configuration_row = connection.execute(
            """
            SELECT revision_id
            FROM active_search_configuration
            WHERE singleton_id = 1
            FOR SHARE
            """
        ).fetchone()
        release_row = connection.execute(
            """
            SELECT prompt_release_id, relevance_release_id, generation
            FROM active_release_target
            WHERE singleton_id = 1
            FOR SHARE
            """
        ).fetchone()
        if configuration_row is None or release_row is None:
            raise RuntimeError("Pinned onboarding search provenance is missing")
        published = load_published_active_search_configuration(connection)
        policy_row = connection.execute(
            """
            SELECT version, monthly_limit_usd, run_allowance_usd,
                   max_jobs_per_run, max_search_queries_per_run,
                   max_provider_attempts_per_run
            FROM execution_budget_policy
            WHERE singleton_id = 1
            """
        ).fetchone()
        if policy_row is None:
            return ExecutionBlocked(reason="budget_not_configured")
        policy = ExecutionBudgetPolicy(
            version=cast(int, policy_row[0]),
            monthly_limit_usd=Decimal(str(policy_row[1])),
            run_allowance_usd=Decimal(str(policy_row[2])),
            max_jobs_per_run=cast(int, policy_row[3]),
            max_search_queries_per_run=cast(int, policy_row[4]),
            max_provider_attempts_per_run=cast(int, policy_row[5]),
        )
        limits = OnboardingTestSearchLimits.from_policy(
            policy, published.active.revision.configuration
        )
        reservation_key = onboarding_test_search_reservation_key(command.idempotency_key)
        admission = admit_onboarding_test_execution(
            connection, idempotency_key=reservation_key, requested_at=command.timestamp
        )
        if not isinstance(admission, ExecutionAdmitted):
            return admission
        run_id = onboarding_test_search_run_id(command.idempotency_key)
        _ = connection.execute(
            """
            INSERT INTO onboarding_test_search_requests (
              idempotency_key, run_id, state, configuration_revision_id,
              prompt_release_id, relevance_release_id, release_generation,
              budget_policy_version, budget_reservation_key, max_queries, max_urls,
              max_jobs, max_work_attempts, max_provider_attempts, run_allowance_usd,
              attempt_count, created_at, updated_at
            ) VALUES (
              %s, %s, 'pending', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0, %s, %s
            )
            """,
            (
                command.idempotency_key,
                run_id,
                published.active.revision.id,
                str(release_row[0]),
                str(release_row[1]),
                release_row[2],
                policy.version,
                reservation_key,
                limits.max_queries,
                limits.max_urls,
                admission.max_jobs,
                limits.max_work_attempts,
                limits.max_provider_attempts,
                limits.run_allowance_usd,
                command.timestamp,
                command.timestamp,
            ),
        )
        stored = _load_request(connection, command.idempotency_key)
        if stored is None:
            raise RuntimeError("Created onboarding test search request is missing")
        return OnboardingTestSearchAccepted(replayed=False, request=stored)


def claim_next_onboarding_test_search(
    connection: Connection,
    *,
    owner_token: UUID,
    claimed_at: datetime,
    lease_for: timedelta,
) -> OnboardingTestSearchRequest | None:
    _require_autocommit(connection)
    if lease_for <= timedelta(0):
        raise ValueError("Onboarding test search lease must be positive")
    with connection.transaction():
        _ = connection.execute(
            """
            UPDATE onboarding_test_search_requests
            SET state = 'failed',
                owner_token = NULL,
                lease_expires_at = NULL,
                error_code = 'worker_attempts_exhausted',
                error_reason = 'Onboarding test search exhausted worker attempts',
                updated_at = %s,
                completed_at = %s
            WHERE attempt_count >= max_work_attempts
              AND (
                state = 'pending'
                OR (state = 'leased' AND lease_expires_at <= %s)
              )
            """,
            (claimed_at, claimed_at, claimed_at),
        )
        row = connection.execute(
            """
            WITH candidate AS (
              SELECT idempotency_key
              FROM onboarding_test_search_requests
              WHERE attempt_count < max_work_attempts
                AND (
                  state = 'pending'
                  OR (state = 'leased' AND lease_expires_at <= %s)
                )
              ORDER BY created_at, idempotency_key
              FOR UPDATE SKIP LOCKED
              LIMIT 1
            )
            UPDATE onboarding_test_search_requests request
            SET state = 'leased',
                owner_token = %s,
                lease_expires_at = %s,
                attempt_count = request.attempt_count + 1,
                updated_at = %s
            FROM candidate
            WHERE request.idempotency_key = candidate.idempotency_key
            RETURNING request.idempotency_key
            """,
            (claimed_at, owner_token, claimed_at + lease_for, claimed_at),
        ).fetchone()
    if row is None:
        return None
    stored = _load_request(connection, str(row[0]))
    if stored is None:
        raise RuntimeError("Claimed onboarding test search request is missing")
    return stored


def complete_onboarding_test_search(
    connection: Connection,
    *,
    run_id: UUID,
    owner_token: UUID,
    completed_at: datetime,
) -> OnboardingTestSearchRequest | None:
    return _finish_onboarding_test_search(
        connection,
        run_id=run_id,
        owner_token=owner_token,
        completed_at=completed_at,
        state="completed",
        error_code=None,
        error_reason=None,
    )


def fail_onboarding_test_search(
    connection: Connection,
    *,
    run_id: UUID,
    owner_token: UUID,
    completed_at: datetime,
    error_code: str,
    error_reason: str,
) -> OnboardingTestSearchRequest | None:
    return _finish_onboarding_test_search(
        connection,
        run_id=run_id,
        owner_token=owner_token,
        completed_at=completed_at,
        state="failed",
        error_code=error_code,
        error_reason=error_reason,
    )


def load_onboarding_test_search(
    connection: Connection, idempotency_key: str
) -> OnboardingTestSearchRequest | None:
    return _load_request(connection, idempotency_key)


def _finish_onboarding_test_search(
    connection: Connection,
    *,
    run_id: UUID,
    owner_token: UUID,
    completed_at: datetime,
    state: Literal["completed", "failed"],
    error_code: str | None,
    error_reason: str | None,
) -> OnboardingTestSearchRequest | None:
    _require_autocommit(connection)
    with connection.transaction():
        row = connection.execute(
            """
            UPDATE onboarding_test_search_requests
            SET state = %s,
                error_code = %s,
                error_reason = %s,
                updated_at = %s,
                completed_at = %s
            WHERE run_id = %s
              AND owner_token = %s
              AND state = 'leased'
              AND lease_expires_at > %s
            RETURNING idempotency_key
            """,
            (
                state,
                error_code,
                error_reason,
                completed_at,
                completed_at,
                run_id,
                owner_token,
                completed_at,
            ),
        ).fetchone()
    if row is None:
        return None
    stored = _load_request(connection, str(row[0]))
    if stored is None:
        raise RuntimeError("Finished onboarding test search request is missing")
    return stored


def _load_request(
    connection: Connection, idempotency_key: str
) -> OnboardingTestSearchRequest | None:
    row = connection.execute(
        """
        SELECT idempotency_key, run_id, state, configuration_revision_id,
               prompt_release_id, relevance_release_id, release_generation,
               budget_policy_version, budget_reservation_key, max_queries, max_urls,
               max_jobs, max_work_attempts, max_provider_attempts, run_allowance_usd,
               attempt_count, owner_token, lease_expires_at, error_code, error_reason,
               created_at, updated_at, completed_at
        FROM onboarding_test_search_requests
        WHERE idempotency_key = %s
        """,
        (idempotency_key,),
    ).fetchone()
    if row is None:
        return None
    return OnboardingTestSearchRequest(
        idempotency_key=str(row[0]),
        run_id=cast(UUID, row[1]),
        state=cast(Literal["pending", "leased", "completed", "failed"], str(row[2])),
        configuration_revision_id=SearchConfigurationRevisionId(str(row[3])),
        prompt_release_id=PromptReleaseId(str(row[4])),
        relevance_release_id=RelevanceReleaseId(str(row[5])),
        release_generation=cast(int, row[6]),
        budget_policy_version=cast(int, row[7]),
        budget_reservation_key=str(row[8]),
        limits=OnboardingTestSearchLimits(
            max_queries=cast(int, row[9]),
            max_urls=cast(int, row[10]),
            max_jobs=cast(int, row[11]),
            max_work_attempts=cast(int, row[12]),
            max_provider_attempts=cast(int, row[13]),
            run_allowance_usd=Decimal(str(row[14])),
        ),
        attempt_count=cast(int, row[15]),
        owner_token=cast(UUID | None, row[16]),
        lease_expires_at=cast(datetime | None, row[17]),
        error_code=None if row[18] is None else str(row[18]),
        error_reason=None if row[19] is None else str(row[19]),
        created_at=cast(datetime, row[20]),
        updated_at=cast(datetime, row[21]),
        completed_at=cast(datetime | None, row[22]),
    )


def _require_autocommit(connection: Connection) -> None:
    if not connection.autocommit:
        raise ValueError("Onboarding test search operations require an autocommit connection")
