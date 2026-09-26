from __future__ import annotations

from job_finder.execution_budget import (
    estimate_execution,
    owner_may_run_onboarding_test_search,
    owner_may_run_scheduled_execution,
)
from job_finder.evaluation.jev import JevRetryPolicy
from job_finder.evaluation.openrouter import RetryPolicy as OpenRouterRetryPolicy
from job_finder.evaluation.prompt_releases import build_prompt_release
from job_finder.evaluation.relevance_releases import (
    build_gemini_policy,
    build_jev_atomic_policy,
)
from job_finder.review.owner_access import OnboardingStage
from job_finder.search_configuration import DEFAULT_SEARCH_CONFIGURATION


def test_execution_estimate_bounds_queries_and_provider_retries() -> None:
    configuration = DEFAULT_SEARCH_CONFIGURATION
    prompt_release = build_prompt_release(configuration)

    jev_estimate = estimate_execution(
        configuration,
        prompt_release,
        build_jev_atomic_policy(),
        max_jobs=25,
    )
    gemini_estimate = estimate_execution(
        configuration,
        prompt_release,
        build_gemini_policy(prompt_release),
        max_jobs=25,
    )

    assert jev_estimate.search_queries == (
        len(configuration.search_keywords) * len(configuration.enabled_sources)
    )
    assert jev_estimate.logical_model_calls_per_job == len(prompt_release.versions)
    relevance_calls = sum(
        version.definition.phase in ("filter", "profile") for version in prompt_release.versions
    )
    openrouter_calls = len(prompt_release.versions) - relevance_calls
    per_job_attempts = relevance_calls * JevRetryPolicy().max_attempts + openrouter_calls * (
        OpenRouterRetryPolicy().max_attempts * 2
    )
    assert jev_estimate.maximum_provider_attempts == per_job_attempts * 25
    assert gemini_estimate.maximum_provider_attempts == (
        (relevance_calls + openrouter_calls) * OpenRouterRetryPolicy().max_attempts * 2 * 25
    )


def test_execution_estimate_uses_prompt_release_instead_of_configuration_prompts() -> None:
    prompt_release = build_prompt_release(DEFAULT_SEARCH_CONFIGURATION)
    acquisition_only_change = DEFAULT_SEARCH_CONFIGURATION.model_copy(
        update={
            "personal_criteria": DEFAULT_SEARCH_CONFIGURATION.personal_criteria[:1],
            "target_profiles": DEFAULT_SEARCH_CONFIGURATION.target_profiles[:1],
        }
    )

    estimate = estimate_execution(
        acquisition_only_change,
        prompt_release,
        build_jev_atomic_policy(),
        max_jobs=1,
    )

    assert estimate.logical_model_calls_per_job == len(prompt_release.versions)
    assert estimate.maximum_provider_attempts == 40


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
