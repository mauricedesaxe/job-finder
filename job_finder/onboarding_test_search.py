from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import ClassVar, Literal, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field

from job_finder.configuration_service import (
    Actor,
    IdempotencyKey,
    load_published_active_search_configuration,
)
from job_finder.evaluation.models import PromptReleaseId, RelevanceReleaseId
from job_finder.evaluation.release_targets import get_active_release_target
from job_finder.execution_budget import (
    ExecutionAdmitted,
    ExecutionBudgetPolicy,
    admit_onboarding_test_execution,
)
from job_finder.pipeline.state import JOB_WORK_ATTEMPT_LIMIT
from job_finder.review.owner_access import OnboardingStage
from job_finder.review.postgres import Connection, ConnectionFactory
from job_finder.search_configuration import SearchConfigurationRevisionId


class OnboardingTestSearchError(RuntimeError):
    pass


class OnboardingTestSearchModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class OnboardingTestSearchLimits(OnboardingTestSearchModel):
    max_queries: int = Field(gt=0)
    max_urls: int = Field(gt=0)
    max_jobs: int = Field(gt=0)
    max_work_attempts: int = Field(gt=0)
    max_provider_attempts: int = Field(gt=0)
    run_allowance_usd: Decimal = Field(gt=0, max_digits=18, decimal_places=8)

    @classmethod
    def from_policy(cls, policy: ExecutionBudgetPolicy) -> OnboardingTestSearchLimits:
        return cls(
            max_queries=policy.max_search_queries_per_run,
            max_urls=policy.max_jobs_per_run * 4,
            max_jobs=policy.max_jobs_per_run,
            max_work_attempts=JOB_WORK_ATTEMPT_LIMIT,
            max_provider_attempts=policy.max_provider_attempts_per_run,
            run_allowance_usd=policy.run_allowance_usd,
        )


class CreateOnboardingTestSearch(OnboardingTestSearchModel):
    idempotency_key: IdempotencyKey
    actor: Actor
    requested_at: datetime


class OnboardingTestSearchRequest(OnboardingTestSearchModel):
    id: UUID
    idempotency_key: str
    actor: str
    status: Literal["pending", "leased", "completed", "failed"]
    configuration_revision_id: SearchConfigurationRevisionId
    prompt_release_id: PromptReleaseId
    relevance_release_id: RelevanceReleaseId
    release_generation: int = Field(ge=0)
    budget_policy_version: int = Field(ge=1)
    budget_reservation_key: str
    limits: OnboardingTestSearchLimits
    worker_attempt_limit: int = Field(gt=0)
    attempt_count: int = Field(ge=0)
    owner_token: UUID | None
    lease_expires_at: datetime | None
    pipeline_run_id: UUID | None
    requested_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    error_code: str | None
    reason: str | None


class OnboardingTestSearchRequested(OnboardingTestSearchModel):
    request: OnboardingTestSearchRequest
    replayed: bool


@dataclass(frozen=True)
class OnboardingTestSearchService:
    inspect: Callable[[], OnboardingTestSearchRequest | None]
    request: Callable[[CreateOnboardingTestSearch], OnboardingTestSearchRequested]


def postgres_onboarding_test_search_service(
    connect: ConnectionFactory,
) -> OnboardingTestSearchService:
    def inspect() -> OnboardingTestSearchRequest | None:
        with connect() as connection:
            return load_latest_onboarding_test_search(connection)

    def request(command: CreateOnboardingTestSearch) -> OnboardingTestSearchRequested:
        with connect() as connection:
            return create_onboarding_test_search(connection, command)

    return OnboardingTestSearchService(inspect=inspect, request=request)


def create_onboarding_test_search(
    connection: Connection,
    command: CreateOnboardingTestSearch,
) -> OnboardingTestSearchRequested:
    reservation_key = f"onboarding-test-search:{command.idempotency_key}"
    request_id = uuid5(NAMESPACE_URL, reservation_key)
    with connection.transaction():
        _ = connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (command.idempotency_key,),
        ).fetchone()
        existing = load_onboarding_test_search_by_key(connection, command.idempotency_key)
        if existing is not None:
            if existing.actor != command.actor:
                raise OnboardingTestSearchError("Idempotency key belongs to another request")
            return OnboardingTestSearchRequested(request=existing, replayed=True)
        owner_row = connection.execute(
            "SELECT stage FROM owner_onboarding WHERE singleton_id = 1 FOR UPDATE"
        ).fetchone()
        if owner_row is None or OnboardingStage(str(owner_row[0])) is not OnboardingStage.TEST_SEARCH:
            raise OnboardingTestSearchError("Owner onboarding is not ready for a test search")
        _ = connection.execute(
            "SELECT 1 FROM active_search_configuration WHERE singleton_id = 1 FOR SHARE"
        ).fetchone()
        _ = connection.execute(
            "SELECT 1 FROM active_release_target WHERE singleton_id = 1 FOR SHARE"
        ).fetchone()
        admission = admit_onboarding_test_execution(
            connection,
            idempotency_key=reservation_key,
            requested_at=command.requested_at,
        )
        if not isinstance(admission, ExecutionAdmitted):
            raise OnboardingTestSearchError(f"Test search was not admitted: {admission.reason}")
        policy = _load_policy(connection)
        limits = OnboardingTestSearchLimits.from_policy(policy)
        active_configuration = load_published_active_search_configuration(connection)
        active_target = get_active_release_target(connection)
        _ = connection.execute(
            """
            INSERT INTO onboarding_test_search_requests (
              id, idempotency_key, actor, status,
              configuration_revision_id, prompt_release_id, relevance_release_id,
              release_generation, budget_policy_version, budget_reservation_key,
              max_queries, max_urls, max_jobs, max_work_attempts,
              max_provider_attempts, run_allowance_usd, requested_at
            ) VALUES (
              %s, %s, %s, 'pending', %s, %s, %s, %s, %s, %s,
              %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                request_id,
                command.idempotency_key,
                command.actor,
                active_configuration.active.revision.id,
                active_target.target.prompt_release_id,
                active_target.target.relevance_release_id,
                active_target.generation,
                policy.version,
                reservation_key,
                limits.max_queries,
                limits.max_urls,
                limits.max_jobs,
                limits.max_work_attempts,
                limits.max_provider_attempts,
                limits.run_allowance_usd,
                command.requested_at,
            ),
        )
        created = load_onboarding_test_search_by_key(connection, command.idempotency_key)
        if created is None:
            raise RuntimeError("Onboarding test search was not stored")
        return OnboardingTestSearchRequested(request=created, replayed=False)


def load_latest_onboarding_test_search(
    connection: Connection,
) -> OnboardingTestSearchRequest | None:
    row = connection.execute(
        """
        SELECT id FROM onboarding_test_search_requests
        ORDER BY requested_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    return None if row is None else _load_request(connection, UUID(str(row[0])))


def load_onboarding_test_search_by_key(
    connection: Connection,
    idempotency_key: str,
) -> OnboardingTestSearchRequest | None:
    row = connection.execute(
        "SELECT id FROM onboarding_test_search_requests WHERE idempotency_key = %s",
        (idempotency_key,),
    ).fetchone()
    return None if row is None else _load_request(connection, UUID(str(row[0])))


def claim_next_onboarding_test_search(
    connection: Connection,
    *,
    owner_token: UUID,
    claimed_at: datetime,
    lease_for: timedelta,
) -> OnboardingTestSearchRequest | None:
    if lease_for <= timedelta(0):
        raise ValueError("Onboarding test search lease must be positive")
    with connection.transaction():
        _ = connection.execute(
            """
            UPDATE onboarding_test_search_requests
            SET status = 'failed', owner_token = NULL, lease_expires_at = NULL,
                completed_at = %s, error_code = 'worker_attempts_exhausted',
                reason = 'The bounded test search could not recover after repeated worker stops.'
            WHERE status = 'leased' AND lease_expires_at <= %s
              AND attempt_count >= worker_attempt_limit
            """,
            (claimed_at, claimed_at),
        )
        row = connection.execute(
            """
            WITH candidate AS (
              SELECT id
              FROM onboarding_test_search_requests
              WHERE (status = 'pending' OR (status = 'leased' AND lease_expires_at <= %s))
                AND attempt_count < worker_attempt_limit
              ORDER BY requested_at, id
              FOR UPDATE SKIP LOCKED
              LIMIT 1
            )
            UPDATE onboarding_test_search_requests AS request
            SET status = 'leased', owner_token = %s, lease_expires_at = %s,
                attempt_count = request.attempt_count + 1,
                started_at = COALESCE(request.started_at, %s)
            FROM candidate
            WHERE request.id = candidate.id
            RETURNING request.id
            """,
            (claimed_at, owner_token, claimed_at + lease_for, claimed_at),
        ).fetchone()
        return None if row is None else _load_request(connection, UUID(str(row[0])))


def _load_policy(connection: Connection) -> ExecutionBudgetPolicy:
    row = connection.execute(
        """
        SELECT version, monthly_limit_usd, run_allowance_usd,
               max_jobs_per_run, max_search_queries_per_run,
               max_provider_attempts_per_run
        FROM execution_budget_policy
        WHERE singleton_id = 1
        """
    ).fetchone()
    if row is None:
        raise OnboardingTestSearchError("Execution budget is not configured")
    return ExecutionBudgetPolicy(
        version=int(str(row[0])),
        monthly_limit_usd=Decimal(str(row[1])),
        run_allowance_usd=Decimal(str(row[2])),
        max_jobs_per_run=int(str(row[3])),
        max_search_queries_per_run=int(str(row[4])),
        max_provider_attempts_per_run=int(str(row[5])),
    )


def _load_request(connection: Connection, request_id: UUID) -> OnboardingTestSearchRequest:
    row = connection.execute(
        """
        SELECT id, idempotency_key, actor, status,
               configuration_revision_id, prompt_release_id, relevance_release_id,
               release_generation, budget_policy_version, budget_reservation_key,
               max_queries, max_urls, max_jobs, max_work_attempts,
               max_provider_attempts, run_allowance_usd,
               worker_attempt_limit, attempt_count, owner_token, lease_expires_at,
               pipeline_run_id, requested_at, started_at, completed_at,
               error_code, reason
        FROM onboarding_test_search_requests
        WHERE id = %s
        """,
        (request_id,),
    ).fetchone()
    if row is None:
        raise OnboardingTestSearchError("Onboarding test search does not exist")
    return OnboardingTestSearchRequest(
        id=UUID(str(row[0])),
        idempotency_key=str(row[1]),
        actor=str(row[2]),
        status=cast(
            Literal["pending", "leased", "completed", "failed"],
            str(row[3]),
        ),
        configuration_revision_id=SearchConfigurationRevisionId(str(row[4])),
        prompt_release_id=PromptReleaseId(str(row[5])),
        relevance_release_id=RelevanceReleaseId(str(row[6])),
        release_generation=int(str(row[7])),
        budget_policy_version=int(str(row[8])),
        budget_reservation_key=str(row[9]),
        limits=OnboardingTestSearchLimits(
            max_queries=int(str(row[10])),
            max_urls=int(str(row[11])),
            max_jobs=int(str(row[12])),
            max_work_attempts=int(str(row[13])),
            max_provider_attempts=int(str(row[14])),
            run_allowance_usd=Decimal(str(row[15])),
        ),
        worker_attempt_limit=int(str(row[16])),
        attempt_count=int(str(row[17])),
        owner_token=None if row[18] is None else UUID(str(row[18])),
        lease_expires_at=None if row[19] is None else datetime.fromisoformat(str(row[19])),
        pipeline_run_id=None if row[20] is None else UUID(str(row[20])),
        requested_at=datetime.fromisoformat(str(row[21])),
        started_at=None if row[22] is None else datetime.fromisoformat(str(row[22])),
        completed_at=None if row[23] is None else datetime.fromisoformat(str(row[23])),
        error_code=None if row[24] is None else str(row[24]),
        reason=None if row[25] is None else str(row[25]),
    )
