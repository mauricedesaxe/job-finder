from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, ClassVar, Literal, TypeAlias, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from job_finder.database import Connection, ConnectionFactory
from job_finder.evaluation.models import PromptReleaseId, ReleaseTarget, RelevanceReleaseId
from job_finder.evaluation.prompt_releases import PromptRelease
from job_finder.evaluation.release_targets import load_release_target
from job_finder.evaluation.relevance_releases import (
    GeminiExecutionPolicy,
    JevAtomicExecutionPolicy,
    JevFaithfulExecutionPolicy,
    RelevanceExecutionPolicy,
    RelevanceRelease,
)
from job_finder.evaluation.jev import JevRetryPolicy
from job_finder.evaluation.openrouter import RetryPolicy as OpenRouterRetryPolicy
from job_finder.review.owner_access import OnboardingStage, OwnerAccessState
from job_finder.search_configuration import (
    SearchConfiguration,
    SearchConfigurationRevisionId,
    build_search_queries,
    load_search_configuration_revision,
)


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
    configuration_revision_id: Annotated[
        SearchConfigurationRevisionId, Field(pattern=r"^[0-9a-f]{64}$")
    ]
    target: ReleaseTarget
    release_generation: int | None = Field(default=None, ge=0)
    budget_policy_version: int = Field(ge=1)
    run_allowance_usd: Decimal = Field(gt=0, max_digits=18, decimal_places=8)
    estimate: ExecutionEstimate


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

_SCHEDULABLE_ONBOARDING_STAGES = frozenset(
    {OnboardingStage.COMPLETE, OnboardingStage.LEGACY_OWNER_IMPORT}
)
_ONBOARDING_TEST_SEARCH_STAGES = frozenset({OnboardingStage.TEST_SEARCH})


def owner_may_run_scheduled_execution(stage: OnboardingStage | None) -> bool:
    return stage in _SCHEDULABLE_ONBOARDING_STAGES


def owner_may_run_onboarding_test_search(stage: OnboardingStage | None) -> bool:
    return stage in _ONBOARDING_TEST_SEARCH_STAGES


@dataclass(frozen=True)
class BudgetSetupService:
    inspect: Callable[[int], BudgetSetupState]
    save: Callable[[int, Decimal, Decimal, int, str, datetime], BudgetSaveResult]


def estimate_execution(
    configuration: SearchConfiguration,
    prompt_release: PromptRelease,
    relevance_policy: RelevanceExecutionPolicy,
    *,
    max_jobs: int,
) -> ExecutionEstimate:
    relevance_calls = sum(
        version.definition.phase in ("filter", "profile") for version in prompt_release.versions
    )
    openrouter_calls = len(prompt_release.versions) - relevance_calls
    match relevance_policy:
        case GeminiExecutionPolicy():
            relevance_attempts = relevance_calls * OpenRouterRetryPolicy().max_attempts * 2
        case JevAtomicExecutionPolicy() | JevFaithfulExecutionPolicy():
            relevance_attempts = relevance_calls * JevRetryPolicy().max_attempts
    openrouter_attempts = openrouter_calls * OpenRouterRetryPolicy().max_attempts * 2
    return ExecutionEstimate(
        search_queries=len(build_search_queries(configuration)),
        jobs_per_run=max_jobs,
        logical_model_calls_per_job=len(prompt_release.versions),
        maximum_provider_attempts=(relevance_attempts + openrouter_attempts) * max_jobs,
    )


def postgres_budget_setup_service(connect: ConnectionFactory) -> BudgetSetupService:
    def inspect(max_jobs: int) -> BudgetSetupState:
        with connect() as connection:
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
            estimate = _estimate_active_execution(
                connection, max_jobs if policy is None else policy.max_jobs_per_run
            )
        return BudgetSetupState(policy=policy, estimate=estimate)

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
            estimate = _estimate_active_execution(connection, max_jobs)
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
    with connection.transaction():
        return _admit_execution(
            connection,
            idempotency_key=idempotency_key,
            requested_at=requested_at,
            allowed_stages=_SCHEDULABLE_ONBOARDING_STAGES,
        )


def admit_onboarding_test_execution(
    connection: Connection,
    *,
    idempotency_key: str,
    requested_at: datetime,
) -> ExecutionAdmission:
    return _admit_execution(
        connection,
        idempotency_key=idempotency_key,
        requested_at=requested_at,
        allowed_stages=_ONBOARDING_TEST_SEARCH_STAGES,
    )


def _admit_execution(
    connection: Connection,
    *,
    idempotency_key: str,
    requested_at: datetime,
    allowed_stages: frozenset[OnboardingStage],
) -> ExecutionAdmission:
    period_start = date(requested_at.year, requested_at.month, 1)
    owner_row = connection.execute(
        "SELECT stage FROM owner_onboarding WHERE singleton_id = 1 FOR UPDATE"
    ).fetchone()
    if owner_row is None or OnboardingStage(str(owner_row[0])) not in allowed_stages:
        return ExecutionBlocked(reason="onboarding_incomplete")
    existing = connection.execute(
        """
        SELECT max_jobs, status, authority_kind, policy_version, reserved_usd,
               configuration_revision_id, prompt_release_id, relevance_release_id,
               release_generation, search_queries, logical_model_calls_per_job,
               maximum_provider_attempts
        FROM execution_budget_reservations
        WHERE idempotency_key = %s
        """,
        (idempotency_key,),
    ).fetchone()
    if existing is not None:
        if str(existing[1]) == "settled":
            return ExecutionBlocked(reason="already_consumed")
        return _admitted_from_row(connection, idempotency_key, existing)
    policy_row = connection.execute(
        """
        SELECT version, monthly_limit_usd, run_allowance_usd, max_jobs_per_run,
               max_search_queries_per_run, max_provider_attempts_per_run
        FROM execution_budget_policy
        WHERE singleton_id = 1
        FOR UPDATE
        """
    ).fetchone()
    if policy_row is None:
        return ExecutionBlocked(reason="budget_not_configured")
    (
        configuration_revision_id,
        configuration,
        target,
        release_generation,
        prompt_release,
        relevance_release,
    ) = _load_active_execution(connection)
    estimate = estimate_execution(
        configuration,
        prompt_release,
        relevance_release.policy,
        max_jobs=cast(int, policy_row[3]),
    )
    if estimate.search_queries > cast(
        int, policy_row[4]
    ) or estimate.maximum_provider_attempts > cast(int, policy_row[5]):
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
          status, max_jobs, authority_kind, configuration_revision_id, prompt_release_id,
          relevance_release_id, release_generation, search_queries,
          logical_model_calls_per_job, maximum_provider_attempts, created_at
        ) VALUES (
          %s, %s, %s, %s, 'reserved', %s, 'pinned', %s, %s, %s, %s, %s, %s, %s, %s
        )
        """,
        (
            idempotency_key,
            policy_row[0],
            period_start,
            reservation,
            policy_row[3],
            configuration_revision_id,
            target.prompt_release_id,
            target.relevance_release_id,
            release_generation,
            estimate.search_queries,
            estimate.logical_model_calls_per_job,
            estimate.maximum_provider_attempts,
            requested_at,
        ),
    )
    return ExecutionAdmitted(
        max_jobs=cast(int, policy_row[3]),
        configuration_revision_id=configuration_revision_id,
        target=target,
        release_generation=release_generation,
        budget_policy_version=cast(int, policy_row[0]),
        run_allowance_usd=reservation,
        estimate=estimate,
    )


def _estimate_active_execution(connection: Connection, max_jobs: int) -> ExecutionEstimate:
    _, configuration, _, _, prompt_release, relevance_release = _load_active_execution(connection)
    return estimate_execution(
        configuration,
        prompt_release,
        relevance_release.policy,
        max_jobs=max_jobs,
    )


def _load_active_execution(
    connection: Connection,
) -> tuple[
    SearchConfigurationRevisionId,
    SearchConfiguration,
    ReleaseTarget,
    int,
    PromptRelease,
    RelevanceRelease,
]:
    row = connection.execute(
        """
        SELECT configuration.revision_id,
               target.prompt_release_id, target.relevance_release_id, target.generation
        FROM active_search_configuration configuration
        CROSS JOIN active_release_target target
        WHERE configuration.singleton_id = 1 AND target.singleton_id = 1
        FOR SHARE OF configuration, target
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("Active execution authority is missing")
    configuration_revision_id = SearchConfigurationRevisionId(str(row[0]))
    target = ReleaseTarget(
        prompt_release_id=PromptReleaseId(str(row[1])),
        relevance_release_id=RelevanceReleaseId(str(row[2])),
    )
    configuration = load_search_configuration_revision(
        connection, configuration_revision_id
    ).configuration
    prompt_release, relevance_release = load_release_target(connection, target)
    return (
        configuration_revision_id,
        configuration,
        target,
        cast(int, row[3]),
        prompt_release,
        relevance_release,
    )


def _admitted_from_row(
    connection: Connection,
    idempotency_key: str,
    row: tuple[object, ...],
) -> ExecutionAdmitted:
    max_jobs = cast(int, row[0])
    if str(row[2]) == "legacy":
        return _legacy_admission(
            connection,
            idempotency_key=idempotency_key,
            max_jobs=max_jobs,
            budget_policy_version=cast(int, row[3]),
            run_allowance_usd=Decimal(str(row[4])),
        )
    if str(row[2]) not in {"adopted", "pinned"}:
        raise RuntimeError("Execution reservation has an unknown authority kind")
    required_indexes = (5, 6, 7, 9, 10, 11)
    if any(row[index] is None for index in required_indexes) or (
        str(row[2]) == "pinned" and row[8] is None
    ):
        raise RuntimeError("Execution reservation has incomplete authority")
    target = ReleaseTarget(
        prompt_release_id=PromptReleaseId(str(row[6])),
        relevance_release_id=RelevanceReleaseId(str(row[7])),
    )
    return ExecutionAdmitted(
        max_jobs=max_jobs,
        configuration_revision_id=SearchConfigurationRevisionId(str(row[5])),
        target=target,
        release_generation=None if row[8] is None else cast(int, row[8]),
        budget_policy_version=cast(int, row[3]),
        run_allowance_usd=Decimal(str(row[4])),
        estimate=ExecutionEstimate(
            search_queries=cast(int, row[9]),
            jobs_per_run=max_jobs,
            logical_model_calls_per_job=cast(int, row[10]),
            maximum_provider_attempts=cast(int, row[11]),
        ),
    )


def _legacy_admission(
    connection: Connection,
    *,
    idempotency_key: str,
    max_jobs: int,
    budget_policy_version: int,
    run_allowance_usd: Decimal,
) -> ExecutionAdmitted:
    run = connection.execute(
        """
        SELECT configuration_revision_id, prompt_release_id, relevance_release_id
        FROM pipeline_runs
        WHERE idempotency_key = %s AND kind = 'orchestration'
        """,
        (idempotency_key,),
    ).fetchone()
    if run is None:
        configuration_revision_id, configuration, target, generation, prompt, relevance = (
            _load_active_execution(connection)
        )
        authority_kind = "pinned"
    else:
        configuration_revision_id = SearchConfigurationRevisionId(str(run[0]))
        configuration = load_search_configuration_revision(
            connection, configuration_revision_id
        ).configuration
        target = ReleaseTarget(
            prompt_release_id=PromptReleaseId(str(run[1])),
            relevance_release_id=RelevanceReleaseId(str(run[2])),
        )
        prompt, relevance = load_release_target(connection, target)
        generation = None
        authority_kind = "adopted"
    estimate = estimate_execution(
        configuration,
        prompt,
        relevance.policy,
        max_jobs=max_jobs,
    )
    changed = connection.execute(
        """
        UPDATE execution_budget_reservations
        SET authority_kind = %s, configuration_revision_id = %s,
            prompt_release_id = %s, relevance_release_id = %s,
            release_generation = %s, search_queries = %s,
            logical_model_calls_per_job = %s, maximum_provider_attempts = %s
        WHERE idempotency_key = %s AND authority_kind = 'legacy'
        """,
        (
            authority_kind,
            configuration_revision_id,
            target.prompt_release_id,
            target.relevance_release_id,
            generation,
            estimate.search_queries,
            estimate.logical_model_calls_per_job,
            estimate.maximum_provider_attempts,
            idempotency_key,
        ),
    ).rowcount
    if changed != 1:
        raise RuntimeError("Legacy execution authority could not be adopted")
    return ExecutionAdmitted(
        max_jobs=max_jobs,
        configuration_revision_id=configuration_revision_id,
        target=target,
        release_generation=generation,
        budget_policy_version=budget_policy_version,
        run_allowance_usd=run_allowance_usd,
        estimate=estimate,
    )


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
