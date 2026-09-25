from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Callable, Literal
from uuid import UUID

import requests

from job_finder.discovery.exchange_rates import ExchangeRateSnapshot
from job_finder.discovery.jina import JinaUnavailable
from job_finder.evaluation.jev import JevHttpResponse, send_system_one
from job_finder.evaluation.models import ReleaseTarget
from job_finder.evaluation.openrouter import HttpResponse, send_chat_completion
from job_finder.execution_budget import settle_execution_budget
from job_finder.onboarding_test_search import (
    OnboardingTestSearchRequest,
    OnboardingProviderAttemptLimit,
    OnboardingProviderOutcomeUnknown,
    claim_next_onboarding_test_search,
    complete_onboarding_test_search,
    fail_onboarding_test_search,
    finish_onboarding_search_query,
    renew_onboarding_test_search_lease,
    finish_onboarding_provider_dispatch,
    prepare_onboarding_provider_dispatch,
    reserve_onboarding_search_query,
)
from job_finder.pipeline.orchestration import PipelineBoundaries, process_claimed_jobs
from job_finder.pipeline.connection import Connection
from job_finder.pipeline.work_items import JobWorkClaim
from job_finder.pipeline.runs import (
    complete_orchestration_run,
    fail_orchestration_run,
    prepare_onboarding_run,
)
from job_finder.search_configuration import (
    build_search_queries,
    load_search_configuration_revision,
)

Now = Callable[[], datetime]
RateSnapshotFactory = Callable[[], ExchangeRateSnapshot]


class OnboardingSearchLimitReached(RuntimeError):
    pass


@dataclass(frozen=True)
class OnboardingSearchWorkerResult:
    request_key: str | None
    state: str
    queries: int = 0
    urls: int = 0
    jobs: int = 0
    provider_attempts: int = 0


def execute_next_onboarding_test_search(
    connection: Connection,
    boundaries: PipelineBoundaries,
    *,
    implementation_ref: str,
    openrouter_api_key: str,
    typesafe_api_key: str | None,
    owner_token: UUID,
    lease_for: timedelta,
    retry_after: timedelta,
    enable_ats_enrichment: bool,
    fetch_rates: RateSnapshotFactory,
    now: Now = lambda: datetime.now(UTC),
) -> OnboardingSearchWorkerResult:
    request = claim_next_onboarding_test_search(
        connection, owner_token=owner_token, claimed_at=now(), lease_for=lease_for
    )
    if request is None:
        return OnboardingSearchWorkerResult(request_key=None, state="idle")
    run = prepare_onboarding_run(
        connection,
        run_id=request.run_id,
        request_key=request.idempotency_key,
        implementation_ref=implementation_ref,
        configuration_revision_id=request.configuration_revision_id,
        target=ReleaseTarget(
            prompt_release_id=request.prompt_release_id,
            relevance_release_id=request.relevance_release_id,
        ),
        started_at=now(),
        fetch_rates=fetch_rates,
    )
    if run.status == "completed":
        _finish_request(connection, request, owner_token, now())
        return _result(connection, request, "completed")
    try:
        configuration = load_search_configuration_revision(
            connection, request.configuration_revision_id
        ).configuration
        queries = build_search_queries(configuration)
        if len(queries) > request.limits.max_queries:
            raise OnboardingSearchLimitReached("Pinned search exceeds its query limit")
        for ordinal, query in enumerate(queries):
            _renew(connection, request, owner_token, now(), lease_for)
            if not reserve_onboarding_search_query(
                connection,
                request=request,
                owner_token=owner_token,
                ordinal=ordinal,
                query=query,
                reserved_at=now(),
            ):
                continue
            search = boundaries.search(query.keyword, query.domain)
            _ = finish_onboarding_search_query(
                connection,
                request=request,
                owner_token=owner_token,
                ordinal=ordinal,
                query=query,
                raw_urls=None if isinstance(search, JinaUnavailable) else search.urls,
                finished_at=now(),
            )
        unavailable = connection.execute(
            """
            SELECT count(*) FROM onboarding_search_queries
            WHERE request_key = %s AND state = 'unavailable'
            """,
            (request.idempotency_key,),
        ).fetchone()
        if unavailable is None:
            raise RuntimeError("Onboarding search results are missing")
        if int(str(unavailable[0])) == len(queries):
            raise OnboardingSearchLimitReached("Every onboarding search query was unavailable")
        guarded, select_claim = _guarded_boundaries(
            connection, boundaries, request, owner_token, now
        )
        for _ in range(request.limits.max_jobs * request.limits.max_work_attempts):
            _renew(connection, request, owner_token, now(), lease_for)
            processing = process_claimed_jobs(
                connection,
                run,
                guarded,
                openrouter_api_key=openrouter_api_key,
                typesafe_api_key=typesafe_api_key,
                owner_token=owner_token,
                observed_at=now(),
                max_items=1,
                lease_for=lease_for,
                retry_after=retry_after,
                enable_ats_enrichment=enable_ats_enrichment,
                onboarding_request_key=request.idempotency_key,
                attempt_limit=request.limits.max_work_attempts,
                on_claim=select_claim,
                now=now,
            )
            if processing.claimed_count == 0:
                break
        pending = connection.execute(
            """
            SELECT count(*) FROM job_work_items
            WHERE onboarding_request_key = %s AND state IN ('pending', 'failed', 'leased')
            """,
            (request.idempotency_key,),
        ).fetchone()
        if pending is None:
            raise RuntimeError("Onboarding work state is missing")
        if int(str(pending[0])) > 0:
            _renew(
                connection,
                request,
                owner_token,
                now(),
                max(retry_after, timedelta(seconds=1)),
            )
            return _result(connection, request, "waiting_for_retry")
        complete_orchestration_run(connection, run.id, completed_at=now())
        _finish_request(connection, request, owner_token, now())
        return _result(connection, request, "completed")
    except (
        OnboardingSearchLimitReached,
        OnboardingProviderAttemptLimit,
        OnboardingProviderOutcomeUnknown,
    ) as error:
        fail_orchestration_run(
            connection,
            run.id,
            completed_at=now(),
            error_code=type(error).__name__,
            reason=str(error),
        )
        with connection.transaction():
            finished = fail_onboarding_test_search(
                connection,
                run_id=run.id,
                owner_token=owner_token,
                completed_at=now(),
                error_code=type(error).__name__,
                error_reason=str(error),
            )
            if finished is None:
                raise RuntimeError("Onboarding test search lease was lost") from error
            settle_execution_budget(
                connection,
                idempotency_key=request.budget_reservation_key,
                pipeline_run_id=run.id,
                settled_at=now(),
                consume_allowance=True,
            )
        return _result(connection, request, "failed")
    except Exception as error:
        fail_orchestration_run(
            connection,
            run.id,
            completed_at=now(),
            error_code=type(error).__name__,
            reason=str(error),
        )
        raise


def _finish_request(
    connection: Connection,
    request: OnboardingTestSearchRequest,
    owner_token: UUID,
    finished_at: datetime,
) -> None:
    with connection.transaction():
        finished = complete_onboarding_test_search(
            connection,
            run_id=request.run_id,
            owner_token=owner_token,
            completed_at=finished_at,
        )
        if finished is None:
            raise RuntimeError("Onboarding test search lease was lost")
        settle_execution_budget(
            connection,
            idempotency_key=request.budget_reservation_key,
            pipeline_run_id=request.run_id,
            settled_at=finished_at,
            consume_allowance=True,
        )


def _renew(
    connection: Connection,
    request: OnboardingTestSearchRequest,
    owner_token: UUID,
    renewed_at: datetime,
    lease_for: timedelta,
) -> None:
    if not renew_onboarding_test_search_lease(
        connection,
        request_key=request.idempotency_key,
        owner_token=owner_token,
        renewed_at=renewed_at,
        lease_for=lease_for,
    ):
        raise RuntimeError("Onboarding test search lease was lost")


def _guarded_boundaries(
    connection: Connection,
    boundaries: PipelineBoundaries,
    request: OnboardingTestSearchRequest,
    owner_token: UUID,
    now: Now,
) -> tuple[PipelineBoundaries, Callable[[JobWorkClaim], None]]:
    openrouter_sender = boundaries.model_sender or send_chat_completion
    jev_sender = boundaries.jev_sender or send_system_one
    current_job_id: UUID | None = None
    current_operation_key: str | None = None

    def select_claim(claim: JobWorkClaim) -> None:
        nonlocal current_job_id, current_operation_key
        current_job_id = claim.job_id
        current_operation_key = None

    def select_operation(operation_key: str) -> None:
        nonlocal current_operation_key
        current_operation_key = operation_key

    def dispatch(
        body: dict[str, object], provider: Literal["openrouter", "typesafe"]
    ) -> tuple[UUID, str, str, int, int | None, str | None, str | None, float | None]:
        if current_job_id is None or current_operation_key is None:
            raise RuntimeError("Provider request has no claimed onboarding operation")
        digest = hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        reservation = prepare_onboarding_provider_dispatch(
            connection,
            request_key=request.idempotency_key,
            owner_token=owner_token,
            job_id=current_job_id,
            operation_key=current_operation_key,
            provider=provider,
            body_digest=digest,
            attempted_at=now(),
        )
        return (
            current_job_id,
            current_operation_key,
            digest,
            reservation.attempt_number,
            reservation.cached_status_code,
            reservation.cached_body,
            reservation.cached_provider_response_id,
            reservation.cached_retry_after_seconds,
        )

    def send_openrouter(
        url: str, headers: Mapping[str, str], body: dict[str, object], timeout: float
    ) -> HttpResponse:
        job_id, operation_key, digest, attempt, cached_status, cached_body, _, _ = dispatch(
            body, "openrouter"
        )
        if cached_status is not None and cached_body is not None:
            return HttpResponse(status_code=cached_status, body=cached_body)
        try:
            response = openrouter_sender(url, headers, body, timeout)
        except requests.RequestException as error:
            raise OnboardingProviderOutcomeUnknown(
                "OpenRouter request outcome is unknown; the call will not be repeated"
            ) from error
        finish_onboarding_provider_dispatch(
            connection,
            request_key=request.idempotency_key,
            job_id=job_id,
            operation_key=operation_key,
            provider="openrouter",
            body_digest=digest,
            attempt_number=attempt,
            status_code=response.status_code,
            response_body=response.body,
        )
        return response

    def send_jev(
        url: str, headers: Mapping[str, str], body: dict[str, object], timeout: float
    ) -> JevHttpResponse:
        (
            job_id,
            operation_key,
            digest,
            attempt,
            cached_status,
            cached_body,
            cached_id,
            cached_retry,
        ) = dispatch(body, "typesafe")
        if cached_status is not None and cached_body is not None:
            return JevHttpResponse(
                status_code=cached_status,
                body=cached_body,
                provider_request_id=cached_id,
                retry_after_seconds=cached_retry,
            )
        try:
            response = jev_sender(url, headers, body, timeout)
        except requests.RequestException as error:
            raise OnboardingProviderOutcomeUnknown(
                "TypeSafe request outcome is unknown; the call will not be repeated"
            ) from error
        finish_onboarding_provider_dispatch(
            connection,
            request_key=request.idempotency_key,
            job_id=job_id,
            operation_key=operation_key,
            provider="typesafe",
            body_digest=digest,
            attempt_number=attempt,
            status_code=response.status_code,
            response_body=response.body,
            provider_response_id=response.provider_request_id,
            retry_after_seconds=response.retry_after_seconds,
        )
        return response

    return (
        replace(
            boundaries,
            model_sender=send_openrouter,
            jev_sender=send_jev,
            model_call_started=select_operation,
        ),
        select_claim,
    )


def _result(
    connection: Connection, request: OnboardingTestSearchRequest, state: str
) -> OnboardingSearchWorkerResult:
    row = connection.execute(
        """
        SELECT count(*), COALESCE(sum(url_count), 0), COALESCE(sum(new_work_count), 0)
        FROM onboarding_search_queries WHERE request_key = %s
        """,
        (request.idempotency_key,),
    ).fetchone()
    attempts = connection.execute(
        """
        SELECT provider_attempt_count FROM onboarding_test_search_requests
        WHERE idempotency_key = %s
        """,
        (request.idempotency_key,),
    ).fetchone()
    if row is None or attempts is None:
        raise RuntimeError("Onboarding test search progress is missing")
    return OnboardingSearchWorkerResult(
        request_key=request.idempotency_key,
        state=state,
        queries=int(str(row[0])),
        urls=int(str(row[1])),
        jobs=int(str(row[2])),
        provider_attempts=int(str(attempts[0])),
    )
