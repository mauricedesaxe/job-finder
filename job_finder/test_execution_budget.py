from __future__ import annotations

from job_finder.execution_budget import (
    estimate_execution,
    owner_may_run_onboarding_test_search,
    owner_may_run_scheduled_execution,
)
from job_finder.review.owner_access import OnboardingStage
from job_finder.search_configuration import DEFAULT_SEARCH_CONFIGURATION


def test_execution_estimate_bounds_queries_and_provider_retries() -> None:
    configuration = DEFAULT_SEARCH_CONFIGURATION

    estimate = estimate_execution(configuration, max_jobs=25)

    assert estimate.search_queries == (
        len(configuration.search_keywords) * len(configuration.enabled_sources)
    )
    assert estimate.logical_model_calls_per_job == (
        len(configuration.personal_criteria) + len(configuration.target_profiles) + 2
    )
    assert estimate.maximum_provider_attempts == (estimate.logical_model_calls_per_job * 25 * 8)


def test_existing_installs_can_run_scheduled_work_before_budget_setup() -> None:
    assert owner_may_run_scheduled_execution(OnboardingStage.COMPLETE)
    assert owner_may_run_scheduled_execution(OnboardingStage.LEGACY_OWNER_IMPORT)
    assert not owner_may_run_scheduled_execution(OnboardingStage.OWNER_ACCOUNT)
    assert not owner_may_run_scheduled_execution(OnboardingStage.BUDGET)
    assert not owner_may_run_scheduled_execution(OnboardingStage.TEST_SEARCH)
    assert not owner_may_run_scheduled_execution(None)
    assert owner_may_run_onboarding_test_search(OnboardingStage.TEST_SEARCH)
    assert not owner_may_run_onboarding_test_search(OnboardingStage.COMPLETE)
    assert not owner_may_run_onboarding_test_search(None)
