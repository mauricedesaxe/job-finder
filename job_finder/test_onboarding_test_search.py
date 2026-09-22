from decimal import Decimal

from job_finder.execution_budget import ExecutionBudgetPolicy
from job_finder.onboarding_test_search import OnboardingTestSearchLimits


def test_onboarding_test_search_limits_are_derived_from_the_budget_policy() -> None:
    policy = ExecutionBudgetPolicy(
        version=3,
        monthly_limit_usd=Decimal("20"),
        run_allowance_usd=Decimal("2"),
        max_jobs_per_run=25,
        max_search_queries_per_run=12,
        max_provider_attempts_per_run=1600,
    )

    limits = OnboardingTestSearchLimits.from_policy(policy)

    assert limits.max_queries == 12
    assert limits.max_urls == 100
    assert limits.max_jobs == 25
    assert limits.max_work_attempts == 3
    assert limits.max_provider_attempts == 1600
    assert limits.run_allowance_usd == Decimal("2")
