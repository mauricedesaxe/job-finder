from __future__ import annotations

from decimal import Decimal

from job_finder.execution_budget import ExecutionBudgetPolicy, estimate_execution
from job_finder.onboarding_test_search import (
    URLS_PER_JOB,
    OnboardingTestSearchLimits,
    onboarding_test_search_reservation_key,
    onboarding_test_search_run_id,
)
from job_finder.pipeline.work_items import JOB_WORK_ATTEMPT_LIMIT
from job_finder.search_configuration import DEFAULT_SEARCH_CONFIGURATION


def test_limits_derive_urls_from_job_cap_and_reuse_work_attempt_limit() -> None:
    policy = ExecutionBudgetPolicy(
        version=1,
        monthly_limit_usd=Decimal("500"),
        run_allowance_usd=Decimal("50"),
        max_jobs_per_run=10,
        max_search_queries_per_run=10000,
        max_provider_attempts_per_run=1000000,
    )
    configuration = DEFAULT_SEARCH_CONFIGURATION
    estimate = estimate_execution(configuration, policy.max_jobs_per_run)

    limits = OnboardingTestSearchLimits.from_policy(policy, configuration)

    assert limits.max_queries == estimate.search_queries
    assert limits.max_urls == policy.max_jobs_per_run * URLS_PER_JOB
    assert limits.max_jobs == policy.max_jobs_per_run
    assert limits.max_work_attempts == JOB_WORK_ATTEMPT_LIMIT
    assert limits.max_provider_attempts == estimate.maximum_provider_attempts
    assert limits.run_allowance_usd == policy.run_allowance_usd


def test_run_identity_is_deterministic_for_the_idempotency_key() -> None:
    assert onboarding_test_search_run_id("owner-setup") == onboarding_test_search_run_id(
        "owner-setup"
    )
    assert onboarding_test_search_run_id("owner-setup") != onboarding_test_search_run_id(
        "other-setup"
    )
    assert onboarding_test_search_reservation_key("owner-setup") == (
        "onboarding-test-search:owner-setup"
    )
