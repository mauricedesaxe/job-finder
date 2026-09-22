from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import ClassVar, Literal, TypeAlias, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from job_finder.configuration_service import load_published_active_search_configuration
from job_finder.evaluation.jev import JevRetryPolicy
from job_finder.evaluation.openrouter import RetryPolicy as OpenRouterRetryPolicy
from job_finder.review.owner_access import OnboardingStage, OwnerAccessState
from job_finder.review.postgres import Connection, ConnectionFactory
from job_finder.search_configuration import SearchConfiguration


class ExecutionBudgetModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class ExecutionEstimate(ExecutionBudgetModel):
    search_queries: int = Field(gt=0)
    jobs_per_run: int = Field(gt=0)
    logical_model_calls_per_job: int = Field(gt=0)
    maximum_provider_attempts: int = Field(gt=0)


class ExecutionBudgetPolicy(ExecutionBudgetModel):
    version: int = Field(ge=0)
    monthly_limit_usd: Decimal = Field(gt=0, max_digits=18, decimal_places=8)
    run_allowance_usd: Decimal = Field(gt=0, max_digits=18, decimal_places=8)
    max_jobs_per_run: int = Field(gt=0, le=1000)
    max_search_queries_per_run: int = Field(gt=0)
    max_provider_attempts_per_run: int = Field(gt=0)


class BudgetSetupState(ExecutionBudgetModel):
    policy: ExecutionBudgetPolicy | None
    estimate: ExecutionEstimate


class BudgetSaved(ExecutionBudgetModel):
    kind: Literal["saved"] = "saved"
    policy: ExecutionBudgetPolicy
    owner_state: OwnerAccessState


class BudgetChanged(ExecutionBudgetModel):
    kind: Literal["changed"] = "changed"
    policy: ExecutionBudgetPolicy | None


BudgetSaveResult: TypeAlias = BudgetSaved | BudgetChanged


class ExecutionAdmitted(ExecutionBudgetModel):
    kind: Literal["admitted"] = "admitted"
    max_jobs: int


class ExecutionBlocked(ExecutionBudgetModel):
    kind: Literal["blocked"] = "blocked"
    reason: Literal[
        "onboarding_incomplete",
        "budget_not_configured",
        "configuration_exceeds_policy",
        "monthly_budget_exhausted",
        "already_consumed",
    ]


ExecutionAdmission: TypeAlias = ExecutionAdmitted | ExecutionBlocked


@dataclass(frozen=True)
class BudgetSetupService:
    inspect: Callable[[int], BudgetSetupState]
    save: Callable[[int, Decimal, Decimal, int, str, datetime], BudgetSaveResult]


def estimate_execution(configuration: SearchConfiguration, max_jobs: int) -> ExecutionEstimate:
    search_queries = len(configuration.search_keywords) * len(configuration.enabled_sources)
    logical_calls_per_job = (
        len(configuration.personal_criteria) + len(configuration.target_profiles) + 2
    )
    return ExecutionEstimate(
        search_queries=search_queries,
        jobs_per_run=max_jobs,
        logical_model_calls_per_job=logical_calls_per_job,
        maximum_provider_attempts=(
            logical_calls_per_job
            * max_jobs
            * max(
                OpenRouterRetryPolicy().max_attempts * 2,
                JevRetryPolicy().max_attempts,
            )
        ),
    )


def postgres_budget_setup_service(connect: ConnectionFactory) -> BudgetSetupService:
    def inspect(max_jobs: int) -> BudgetSetupState:
        with connect() as connection:
            configuration = load_published_active_search_configuration(
                connection
            ).active.revision.configuration
            row = connection.execute(
                """
                SELECT version, monthly_limit_usd, run_allowance_usd,
                       max_jobs_per_run, max_search_queries_per_run,
                       max_provider_attempts_per_run
                FROM execution_budget_policy
                WHERE singleton_id = 1
                """
            ).fetchone()
        policy = None if row is None else _policy_from_row(row)
        return BudgetSetupState(
            policy=policy,
            estimate=estimate_execution(
                configuration,
                max_jobs if policy is None else policy.max_jobs_per_run,
            ),
        )

    def save(
        expected_version: int,
        monthly_limit_usd: Decimal,
        run_allowance_usd: Decimal,
        max_jobs: int,
        actor: str,
        timestamp: datetime,
    ) -> BudgetSaveResult:
        with connect() as connection, connection.transaction():
            owner_row = connection.execute(
                """
                SELECT stage, password_hash IS NOT NULL
                FROM owner_onboarding
                WHERE singleton_id = 1
                FOR UPDATE
                """
            ).fetchone()
            if owner_row is None:
                raise RuntimeError("Owner onboarding state is missing")
            owner_state = OwnerAccessState(
                stage=OnboardingStage(str(owner_row[0])), has_password=bool(owner_row[1])
            )
            current_row = connection.execute(
                """
                SELECT version, monthly_limit_usd, run_allowance_usd,
                       max_jobs_per_run, max_search_queries_per_run,
                       max_provider_attempts_per_run
                FROM execution_budget_policy
                WHERE singleton_id = 1
                FOR UPDATE
                """
            ).fetchone()
            current_version = 0 if current_row is None else cast(int, current_row[0])
            if current_version != expected_version:
                return BudgetChanged(
                    policy=None if current_row is None else _policy_from_row(current_row)
                )
            if owner_state.stage not in (OnboardingStage.BUDGET, OnboardingStage.COMPLETE):
                return BudgetChanged(
                    policy=None if current_row is None else _policy_from_row(current_row)
                )
            configuration = load_published_active_search_configuration(
                connection
            ).active.revision.configuration
            estimate = estimate_execution(configuration, max_jobs)
            policy = ExecutionBudgetPolicy(
                version=expected_version + 1,
                monthly_limit_usd=monthly_limit_usd,
                run_allowance_usd=run_allowance_usd,
                max_jobs_per_run=max_jobs,
                max_search_queries_per_run=estimate.search_queries,
                max_provider_attempts_per_run=estimate.maximum_provider_attempts,
            )
            if policy.run_allowance_usd > policy.monthly_limit_usd:
                raise ValueError("Per-run allowance cannot exceed the monthly limit")
            if current_row is None:
                _ = connection.execute(
                    """
                    INSERT INTO execution_budget_policy (
                      singleton_id, version, monthly_limit_usd, run_allowance_usd,
                      max_jobs_per_run, max_search_queries_per_run,
                      max_provider_attempts_per_run, updated_at, updated_by
                    ) VALUES (1, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        policy.version,
                        policy.monthly_limit_usd,
                        policy.run_allowance_usd,
                        policy.max_jobs_per_run,
                        policy.max_search_queries_per_run,
                        policy.max_provider_attempts_per_run,
                        timestamp,
                        actor,
                    ),
                )
            else:
                _ = connection.execute(
                    """
                    UPDATE execution_budget_policy
                    SET version = %s, monthly_limit_usd = %s, run_allowance_usd = %s,
                        max_jobs_per_run = %s, max_search_queries_per_run = %s,
                        max_provider_attempts_per_run = %s, updated_at = %s, updated_by = %s
                    WHERE singleton_id = 1 AND version = %s
                    """,
                    (
                        policy.version,
                        policy.monthly_limit_usd,
                        policy.run_allowance_usd,
                        policy.max_jobs_per_run,
                        policy.max_search_queries_per_run,
                        policy.max_provider_attempts_per_run,
                        timestamp,
                        actor,
                        expected_version,
                    ),
                )
            if owner_state.stage is OnboardingStage.BUDGET:
                _ = connection.execute(
                    """
                    UPDATE owner_onboarding
                    SET stage = 'test_search', updated_at = CURRENT_TIMESTAMP
                    WHERE singleton_id = 1 AND stage = 'budget'
                    """
                )
                resulting_stage = OnboardingStage.TEST_SEARCH
            else:
                resulting_stage = OnboardingStage.COMPLETE
        return BudgetSaved(
            policy=policy,
            owner_state=OwnerAccessState(stage=resulting_stage, has_password=True),
        )

    return BudgetSetupService(inspect=inspect, save=save)


def admit_scheduled_execution(
    connection: Connection,
    *,
    idempotency_key: str,
    requested_at: datetime,
) -> ExecutionAdmission:
    period_start = date(requested_at.year, requested_at.month, 1)
    with connection.transaction():
        owner_row = connection.execute(
            "SELECT stage FROM owner_onboarding WHERE singleton_id = 1 FOR UPDATE"
        ).fetchone()
        if owner_row is None or OnboardingStage(str(owner_row[0])) is not OnboardingStage.COMPLETE:
            return ExecutionBlocked(reason="onboarding_incomplete")
        existing = connection.execute(
            """
            SELECT max_jobs, status
            FROM execution_budget_reservations
            WHERE idempotency_key = %s
            """,
            (idempotency_key,),
        ).fetchone()
        if existing is not None:
            if str(existing[1]) == "settled":
                return ExecutionBlocked(reason="already_consumed")
            return ExecutionAdmitted(max_jobs=cast(int, existing[0]))
        policy_row = connection.execute(
            """
            SELECT version, monthly_limit_usd, run_allowance_usd, max_jobs_per_run
            FROM execution_budget_policy
            WHERE singleton_id = 1
            FOR UPDATE
            """
        ).fetchone()
        if policy_row is None:
            return ExecutionBlocked(reason="budget_not_configured")
        configuration = load_published_active_search_configuration(
            connection
        ).active.revision.configuration
        estimate = estimate_execution(configuration, cast(int, policy_row[3]))
        limits_row = connection.execute(
            """
            SELECT max_search_queries_per_run, max_provider_attempts_per_run
            FROM execution_budget_policy
            WHERE singleton_id = 1
            """
        ).fetchone()
        if limits_row is None:
            raise RuntimeError("Execution budget limits could not be loaded")
        if estimate.search_queries > cast(
            int, limits_row[0]
        ) or estimate.maximum_provider_attempts > cast(int, limits_row[1]):
            return ExecutionBlocked(reason="configuration_exceeds_policy")
        consumed_row = connection.execute(
            """
            SELECT COALESCE(sum(CASE WHEN status = 'settled' THEN consumed_usd ELSE reserved_usd END), 0)
            FROM execution_budget_reservations
            WHERE period_start = %s
            """,
            (period_start,),
        ).fetchone()
        if consumed_row is None:
            raise RuntimeError("Execution budget usage could not be loaded")
        consumed = Decimal(str(consumed_row[0]))
        monthly_limit = Decimal(str(policy_row[1]))
        reservation = Decimal(str(policy_row[2]))
        if consumed + reservation > monthly_limit:
            return ExecutionBlocked(reason="monthly_budget_exhausted")
        _ = connection.execute(
            """
            INSERT INTO execution_budget_reservations (
              idempotency_key, policy_version, period_start, reserved_usd,
              status, max_jobs, created_at
            ) VALUES (%s, %s, %s, %s, 'reserved', %s, %s)
            """,
            (
                idempotency_key,
                policy_row[0],
                period_start,
                reservation,
                policy_row[3],
                requested_at,
            ),
        )
        return ExecutionAdmitted(max_jobs=cast(int, policy_row[3]))


def settle_execution_budget(
    connection: Connection,
    *,
    idempotency_key: str,
    pipeline_run_id: UUID | None,
    settled_at: datetime,
    consume_allowance: bool,
) -> None:
    _ = connection.execute(
        """
        UPDATE execution_budget_reservations
        SET status = 'settled',
            consumed_usd = CASE WHEN %s THEN reserved_usd ELSE 0 END,
            pipeline_run_id = %s, settled_at = %s
        WHERE idempotency_key = %s AND status = 'reserved'
        """,
        (consume_allowance, pipeline_run_id, settled_at, idempotency_key),
    )


def reserve_discovery(connection: Connection, idempotency_key: str) -> bool:
    row = connection.execute(
        """
        UPDATE execution_budget_reservations
        SET discovery_reserved = TRUE
        WHERE idempotency_key = %s AND status = 'reserved' AND discovery_reserved = FALSE
        RETURNING 1
        """,
        (idempotency_key,),
    ).fetchone()
    return row is not None


def reserve_job_capacity(connection: Connection, idempotency_key: str) -> int:
    with connection.transaction():
        row = connection.execute(
            """
            SELECT max_jobs, jobs_reserved, status
            FROM execution_budget_reservations
            WHERE idempotency_key = %s
            FOR UPDATE
            """,
            (idempotency_key,),
        ).fetchone()
        if row is None:
            raise RuntimeError("Execution budget reservation is missing")
        if str(row[2]) != "reserved":
            return 0
        remaining = cast(int, row[0]) - cast(int, row[1])
        if remaining:
            _ = connection.execute(
                """
                UPDATE execution_budget_reservations
                SET jobs_reserved = max_jobs
                WHERE idempotency_key = %s
                """,
                (idempotency_key,),
            )
        return remaining


def _policy_from_row(row: tuple[object, ...]) -> ExecutionBudgetPolicy:
    return ExecutionBudgetPolicy(
        version=cast(int, row[0]),
        monthly_limit_usd=Decimal(str(row[1])),
        run_allowance_usd=Decimal(str(row[2])),
        max_jobs_per_run=cast(int, row[3]),
        max_search_queries_per_run=cast(int, row[4]),
        max_provider_attempts_per_run=cast(int, row[5]),
    )
