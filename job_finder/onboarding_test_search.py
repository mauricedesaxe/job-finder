from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, ClassVar, Literal, TypeAlias, cast
from uuid import NAMESPACE_URL, UUID, uuid5

import psycopg
from pydantic import BaseModel, ConfigDict, Field

from job_finder.evaluation.models import PromptReleaseId, RelevanceReleaseId
from job_finder.execution_budget import (
    ExecutionAdmitted,
    ExecutionBlocked,
    ExecutionEstimate,
    admit_onboarding_test_execution,
    settle_execution_budget,
)
from job_finder.pipeline.work_items import JOB_WORK_ATTEMPT_LIMIT
from job_finder.search_configuration import (
    SearchConfigurationRevisionId,
    SearchQuery,
)
from job_finder.pipeline.discoveries import DiscoveryRegistration, register_discoveries

Connection = psycopg.Connection[tuple[object, ...]]
RESERVATION_KEY_PREFIX = "onboarding-test-search:"
URLS_PER_JOB = 4
ONBOARDING_TEST_SEARCH_NAMESPACE = uuid5(NAMESPACE_URL, "job-finder:onboarding-test-search")
_DEFAULT_PROVIDER_RETRYABLE_STATUSES = frozenset({429})


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
    def from_estimate(
        cls, estimate: ExecutionEstimate, run_allowance_usd: Decimal
    ) -> OnboardingTestSearchLimits:
        return cls(
            max_queries=estimate.search_queries,
            max_urls=estimate.jobs_per_run * URLS_PER_JOB,
            max_jobs=estimate.jobs_per_run,
            max_work_attempts=JOB_WORK_ATTEMPT_LIMIT,
            max_provider_attempts=estimate.maximum_provider_attempts,
            run_allowance_usd=run_allowance_usd,
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
    provider_attempt_count: int = Field(ge=0)
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


class OnboardingProviderAttemptLimit(RuntimeError):
    pass


class OnboardingProviderOutcomeUnknown(RuntimeError):
    pass


class OnboardingProviderDispatch(OnboardingTestSearchModel):
    attempt_number: int = Field(ge=1)
    cached_status_code: int | None = None
    cached_body: str | None = None
    cached_provider_response_id: str | None = None
    cached_retry_after_seconds: float | None = None


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
        reservation_key = onboarding_test_search_reservation_key(command.idempotency_key)
        admission = admit_onboarding_test_execution(
            connection, idempotency_key=reservation_key, requested_at=command.timestamp
        )
        if not isinstance(admission, ExecutionAdmitted):
            return admission
        limits = OnboardingTestSearchLimits.from_estimate(
            admission.estimate, admission.run_allowance_usd
        )
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
                admission.configuration_revision_id,
                admission.target.prompt_release_id,
                admission.target.relevance_release_id,
                admission.release_generation,
                admission.budget_policy_version,
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
        exhausted = connection.execute(
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
            RETURNING idempotency_key, run_id, budget_reservation_key,
                      provider_attempt_count
            """,
            (claimed_at, claimed_at, claimed_at),
        ).fetchall()
        for exhausted_row in exhausted:
            queried = connection.execute(
                "SELECT 1 FROM onboarding_search_queries WHERE request_key = %s LIMIT 1",
                (exhausted_row[0],),
            ).fetchone()
            run_row = connection.execute(
                "SELECT id FROM pipeline_runs WHERE id = %s", (exhausted_row[1],)
            ).fetchone()
            settle_execution_budget(
                connection,
                idempotency_key=str(exhausted_row[2]),
                pipeline_run_id=None if run_row is None else cast(UUID, run_row[0]),
                settled_at=claimed_at,
                consume_allowance=queried is not None or cast(int, exhausted_row[3]) > 0,
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


def renew_onboarding_test_search_lease(
    connection: Connection,
    *,
    request_key: str,
    owner_token: UUID,
    renewed_at: datetime,
    lease_for: timedelta,
) -> bool:
    _require_autocommit(connection)
    if lease_for <= timedelta(0):
        raise ValueError("Onboarding test search lease must be positive")
    with connection.transaction():
        changed = connection.execute(
            """
            UPDATE onboarding_test_search_requests
            SET lease_expires_at = %s, updated_at = %s
            WHERE idempotency_key = %s AND state = 'leased' AND owner_token = %s
              AND lease_expires_at > %s
            """,
            (renewed_at + lease_for, renewed_at, request_key, owner_token, renewed_at),
        ).rowcount
    return changed == 1


def reserve_onboarding_search_query(
    connection: Connection,
    *,
    request: OnboardingTestSearchRequest,
    owner_token: UUID,
    ordinal: int,
    query: SearchQuery,
    reserved_at: datetime,
) -> bool:
    _require_autocommit(connection)
    with connection.transaction():
        _require_live_lease(connection, request.idempotency_key, owner_token, reserved_at)
        existing = connection.execute(
            """
            SELECT keyword, domain, state FROM onboarding_search_queries
            WHERE request_key = %s AND ordinal = %s
            """,
            (request.idempotency_key, ordinal),
        ).fetchone()
        if existing is not None:
            if str(existing[0]) != query.keyword or str(existing[1]) != query.domain:
                raise ValueError("Pinned onboarding query differs from its receipt")
            if str(existing[2]) == "reserved":
                _ = connection.execute(
                    """
                    UPDATE onboarding_search_queries SET state = 'unavailable'
                    WHERE request_key = %s AND ordinal = %s AND state = 'reserved'
                    """,
                    (request.idempotency_key, ordinal),
                )
            return False
        count = connection.execute(
            "SELECT count(*) FROM onboarding_search_queries WHERE request_key = %s",
            (request.idempotency_key,),
        ).fetchone()
        if count is None or cast(int, count[0]) >= request.limits.max_queries:
            raise RuntimeError("Onboarding search query limit reached")
        _ = connection.execute(
            """
            INSERT INTO onboarding_search_queries (
              request_key, ordinal, keyword, domain, state
            ) VALUES (%s, %s, %s, %s, 'reserved')
            """,
            (request.idempotency_key, ordinal, query.keyword, query.domain),
        )
    return True


def finish_onboarding_search_query(
    connection: Connection,
    *,
    request: OnboardingTestSearchRequest,
    owner_token: UUID,
    ordinal: int,
    query: SearchQuery,
    raw_urls: tuple[str, ...] | None,
    finished_at: datetime,
) -> DiscoveryRegistration:
    _require_autocommit(connection)
    with connection.transaction():
        _require_live_lease(connection, request.idempotency_key, owner_token, finished_at)
        receipt = connection.execute(
            """
            SELECT state FROM onboarding_search_queries
            WHERE request_key = %s AND ordinal = %s AND keyword = %s AND domain = %s
            FOR UPDATE
            """,
            (request.idempotency_key, ordinal, query.keyword, query.domain),
        ).fetchone()
        if receipt is None or str(receipt[0]) != "reserved":
            raise RuntimeError("Onboarding query reservation is no longer active")
        if raw_urls is None:
            _ = connection.execute(
                """
                UPDATE onboarding_search_queries SET state = 'unavailable'
                WHERE request_key = %s AND ordinal = %s
                """,
                (request.idempotency_key, ordinal),
            )
            return DiscoveryRegistration(discovered_count=0, new_work_count=0)
        totals = connection.execute(
            """
            SELECT COALESCE(sum(url_count), 0), COALESCE(sum(new_work_count), 0)
            FROM onboarding_search_queries WHERE request_key = %s
            """,
            (request.idempotency_key,),
        ).fetchone()
        if totals is None:
            raise RuntimeError("Onboarding search counters are missing")
        remaining_urls = request.limits.max_urls - cast(int, totals[0])
        remaining_jobs = request.limits.max_jobs - cast(int, totals[1])
        accepted_urls = tuple(dict.fromkeys(raw_urls))[: max(0, remaining_urls)]
        registration = register_discoveries(
            connection,
            run_id=request.run_id,
            keyword=query.keyword,
            domain=query.domain,
            raw_urls=accepted_urls,
            discovered_at=finished_at,
            onboarding_request_key=request.idempotency_key,
            max_new_work=max(0, remaining_jobs),
        )
        _ = connection.execute(
            """
            UPDATE onboarding_search_queries
            SET state = 'completed', url_count = %s,
                discovered_count = %s, new_work_count = %s
            WHERE request_key = %s AND ordinal = %s
            """,
            (
                registration.processed_url_count,
                registration.discovered_count,
                registration.new_work_count,
                request.idempotency_key,
                ordinal,
            ),
        )
    return registration


def prepare_onboarding_provider_dispatch(
    connection: Connection,
    *,
    request_key: str,
    owner_token: UUID,
    job_id: UUID,
    operation_key: str,
    provider: Literal["openrouter", "typesafe"],
    body_digest: str,
    attempted_at: datetime,
    retryable_statuses: frozenset[int] = _DEFAULT_PROVIDER_RETRYABLE_STATUSES,
    retry_reserved: bool = False,
) -> OnboardingProviderDispatch:
    _require_autocommit(connection)
    with connection.transaction():
        _require_live_lease(connection, request_key, owner_token, attempted_at)
        prior = connection.execute(
            """
            SELECT attempt_number, state, status_code, response_body,
                   provider_response_id, retry_after_seconds
            FROM onboarding_provider_dispatches
            WHERE request_key = %s AND job_id = %s AND operation_key = %s
              AND provider = %s AND body_digest = %s
            ORDER BY attempt_number DESC LIMIT 1
            """,
            (request_key, job_id, operation_key, provider, body_digest),
        ).fetchone()
        if prior is not None:
            if str(prior[1]) == "reserved" and not retry_reserved:
                raise OnboardingProviderOutcomeUnknown(
                    "A previous provider request has no recorded response"
                )
            if str(prior[1]) == "responded" and cast(int, prior[2]) not in retryable_statuses:
                return OnboardingProviderDispatch(
                    attempt_number=cast(int, prior[0]),
                    cached_status_code=cast(int, prior[2]),
                    cached_body=str(prior[3]),
                    cached_provider_response_id=(None if prior[4] is None else str(prior[4])),
                    cached_retry_after_seconds=cast(float | None, prior[5]),
                )
        attempt_number = 1 if prior is None else cast(int, prior[0]) + 1
        changed = connection.execute(
            """
            UPDATE onboarding_test_search_requests
            SET provider_attempt_count = provider_attempt_count + 1, updated_at = %s
            WHERE idempotency_key = %s AND state = 'leased' AND owner_token = %s
              AND lease_expires_at > %s
              AND provider_attempt_count < max_provider_attempts
            """,
            (attempted_at, request_key, owner_token, attempted_at),
        ).rowcount
        if changed != 1:
            raise OnboardingProviderAttemptLimit("Onboarding provider attempt limit reached")
        _ = connection.execute(
            """
            INSERT INTO onboarding_provider_dispatches (
              request_key, job_id, operation_key, provider, body_digest,
              attempt_number, state
            ) VALUES (%s, %s, %s, %s, %s, %s, 'reserved')
            """,
            (request_key, job_id, operation_key, provider, body_digest, attempt_number),
        )
    return OnboardingProviderDispatch(attempt_number=attempt_number)


def finish_onboarding_provider_dispatch(
    connection: Connection,
    *,
    request_key: str,
    job_id: UUID,
    operation_key: str,
    provider: Literal["openrouter", "typesafe"],
    body_digest: str,
    attempt_number: int,
    status_code: int,
    response_body: str,
    provider_response_id: str | None = None,
    retry_after_seconds: float | None = None,
) -> None:
    _require_autocommit(connection)
    with connection.transaction():
        changed = connection.execute(
            """
            UPDATE onboarding_provider_dispatches
            SET state = 'responded', status_code = %s, response_body = %s,
                provider_response_id = %s, retry_after_seconds = %s
            WHERE request_key = %s AND job_id = %s AND operation_key = %s AND provider = %s
              AND body_digest = %s AND attempt_number = %s AND state = 'reserved'
            """,
            (
                status_code,
                response_body,
                provider_response_id,
                retry_after_seconds,
                request_key,
                job_id,
                operation_key,
                provider,
                body_digest,
                attempt_number,
            ),
        ).rowcount
        if changed != 1:
            raise RuntimeError("Provider dispatch reservation was lost")


def _require_live_lease(
    connection: Connection, request_key: str, owner_token: UUID, now: datetime
) -> None:
    row = connection.execute(
        """
        SELECT 1 FROM onboarding_test_search_requests
        WHERE idempotency_key = %s AND state = 'leased' AND owner_token = %s
          AND lease_expires_at > %s
        FOR UPDATE
        """,
        (request_key, owner_token, now),
    ).fetchone()
    if row is None:
        raise RuntimeError("Onboarding test search lease was lost")


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
               created_at, updated_at, completed_at, provider_attempt_count
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
        provider_attempt_count=cast(int, row[23]),
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
