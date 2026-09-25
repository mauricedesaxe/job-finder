from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from job_finder.configuration_service import (
    ActivateConfigurationCommand,
    ActivateConfigurationResult,
    ConfigurationActivated,
    activate_search_configuration,
)
from job_finder.database import ConnectionFactory
from job_finder.execution_budget import ExecutionBlocked
from job_finder.onboarding_test_search import (
    CreateOnboardingTestSearch,
    CreateOnboardingTestSearchResult,
    OnboardingTestSearchAccepted,
    OnboardingTestSearchRequest,
    create_onboarding_test_search,
    load_onboarding_test_search,
)
from job_finder.review.owner_access import OnboardingStage


@dataclass(frozen=True)
class OnboardingProgressService:
    activate_preferences: Callable[[ActivateConfigurationCommand], ActivateConfigurationResult]


@dataclass(frozen=True)
class OnboardingSearchJob:
    title: str
    company: str
    url: str


@dataclass(frozen=True)
class OnboardingSearchProgress:
    request: OnboardingTestSearchRequest | None
    queries_completed: int = 0
    urls_checked: int = 0
    jobs_found: int = 0
    jobs: tuple[OnboardingSearchJob, ...] = ()


@dataclass(frozen=True)
class OnboardingSearchService:
    inspect: Callable[[], OnboardingSearchProgress]
    launch: Callable[[str, datetime], CreateOnboardingTestSearchResult]


def postgres_test_search_service(connect: ConnectionFactory) -> OnboardingSearchService:
    def inspect() -> OnboardingSearchProgress:
        with connect() as connection:
            row = connection.execute(
                """
                SELECT idempotency_key FROM onboarding_test_search_requests
                ORDER BY created_at DESC, idempotency_key DESC LIMIT 1
                """
            ).fetchone()
            if row is None:
                return OnboardingSearchProgress(request=None)
            request = load_onboarding_test_search(connection, str(row[0]))
            if request is None:
                raise RuntimeError("Onboarding test search request is missing")
            counts = connection.execute(
                """
                SELECT count(*) FILTER (WHERE state <> 'reserved'),
                       COALESCE(sum(url_count), 0), COALESCE(sum(new_work_count), 0)
                FROM onboarding_search_queries WHERE request_key = %s
                """,
                (request.idempotency_key,),
            ).fetchone()
            if counts is None:
                raise RuntimeError("Onboarding test search progress is missing")
            rows = connection.execute(
                """
                SELECT COALESCE(snapshot.title, jobs.raw_url),
                       COALESCE(snapshot.company, ''), jobs.raw_url
                FROM job_discoveries discovery
                JOIN jobs ON jobs.id = discovery.job_id
                LEFT JOIN LATERAL (
                  SELECT title, company FROM job_snapshots
                  WHERE job_id = jobs.id ORDER BY observed_at DESC, id DESC LIMIT 1
                ) snapshot ON TRUE
                WHERE discovery.pipeline_run_id = %s
                ORDER BY discovery.discovered_at DESC, jobs.id
                LIMIT %s
                """,
                (request.run_id, request.limits.max_jobs),
            ).fetchall()
            return OnboardingSearchProgress(
                request=request,
                queries_completed=int(str(counts[0])),
                urls_checked=int(str(counts[1])),
                jobs_found=int(str(counts[2])),
                jobs=tuple(
                    OnboardingSearchJob(str(row[0]), str(row[1]), str(row[2])) for row in rows
                ),
            )

    def launch(actor: str, timestamp: datetime) -> CreateOnboardingTestSearchResult:
        with connect() as connection, connection.transaction():
            owner = connection.execute(
                "SELECT stage FROM owner_onboarding WHERE singleton_id = 1 FOR UPDATE"
            ).fetchone()
            if owner is None:
                raise RuntimeError("Owner onboarding state is missing")
            stage = OnboardingStage(str(owner[0]))
            row = connection.execute(
                """
                SELECT idempotency_key FROM onboarding_test_search_requests
                ORDER BY created_at DESC, idempotency_key DESC LIMIT 1
                """
            ).fetchone()
            if row is not None:
                existing = load_onboarding_test_search(connection, str(row[0]))
                if existing is None:
                    raise RuntimeError("Onboarding test search request is missing")
                if existing.state != "failed":
                    return OnboardingTestSearchAccepted(replayed=True, request=existing)
            if stage is not OnboardingStage.TEST_SEARCH:
                return ExecutionBlocked(reason="onboarding_incomplete")
            return create_onboarding_test_search(
                connection,
                CreateOnboardingTestSearch(
                    idempotency_key=f"owner-setup:{uuid4().hex}",
                    actor=actor,
                    timestamp=timestamp,
                ),
            )

    return OnboardingSearchService(inspect=inspect, launch=launch)


def postgres_onboarding_progress_service(
    connect: ConnectionFactory,
) -> OnboardingProgressService:
    def activate_preferences(
        command: ActivateConfigurationCommand,
    ) -> ActivateConfigurationResult:
        with connect() as connection, connection.transaction():
            row = connection.execute(
                """
                SELECT stage
                FROM owner_onboarding
                WHERE singleton_id = 1
                FOR UPDATE
                """
            ).fetchone()
            if row is None:
                raise RuntimeError("Owner onboarding state is missing")
            stage = OnboardingStage(str(row[0]))
            result = activate_search_configuration(connection, command)
            if not isinstance(result, ConfigurationActivated):
                return result
            if stage is OnboardingStage.PREFERENCES:
                _ = connection.execute(
                    """
                    UPDATE owner_onboarding
                    SET stage = 'budget', updated_at = CURRENT_TIMESTAMP
                    WHERE singleton_id = 1 AND stage = 'preferences'
                    """
                )
        return result

    return OnboardingProgressService(activate_preferences=activate_preferences)
