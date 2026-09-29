from __future__ import annotations

from job_finder.execution_budget import (
    estimate_execution,
    owner_may_run_onboarding_test_search,
    owner_may_run_scheduled_execution,
)
from job_finder.evaluation.prompt_releases import build_prompt_release
from job_finder.evaluation.relevance_releases import (
    build_gemini_policy,
    build_jev_atomic_policy,
    build_jev_faithful_policy,
)
from job_finder.review.owner_access import OnboardingStage
from job_finder.search_configuration import DEFAULT_SEARCH_CONFIGURATION

_TWO_CRITERIA_CONFIGURATION = DEFAULT_SEARCH_CONFIGURATION.model_copy(
    update={
        "personal_criteria": DEFAULT_SEARCH_CONFIGURATION.personal_criteria[:1],
        "target_profiles": DEFAULT_SEARCH_CONFIGURATION.target_profiles[:1],
    }
)


def test_execution_estimate_bounds_queries_and_provider_retries() -> None:
    configuration = _TWO_CRITERIA_CONFIGURATION
    prompt_release = build_prompt_release(configuration)

    assert [version.definition.phase for version in prompt_release.versions] == [
        "filter",
        "profile",
        "enrichment",
        "deduplication",
    ]

    jev_estimate = estimate_execution(
        configuration,
        prompt_release,
        build_jev_atomic_policy(),
        max_jobs=2,
    )
    faithful_estimate = estimate_execution(
        configuration,
        prompt_release,
        build_jev_faithful_policy(prompt_release),
        max_jobs=2,
    )
    gemini_estimate = estimate_execution(
        configuration,
        prompt_release,
        build_gemini_policy(prompt_release),
        max_jobs=2,
    )

    assert jev_estimate.search_queries == (
        len(configuration.search_keywords) * len(configuration.enabled_sources)
    )
    assert jev_estimate.logical_model_calls_per_job == 4
    # 2 relevance criteria retry 4 times via Jev; 2 remaining prompts retry
    # 4 times via OpenRouter with 2 requests per attempt; all doubled by max_jobs.
    assert jev_estimate.maximum_provider_attempts == 48
    assert faithful_estimate.maximum_provider_attempts == 48
    assert gemini_estimate.maximum_provider_attempts == 64


def test_execution_estimate_uses_prompt_release_instead_of_configuration_prompts() -> None:
    prompt_release = build_prompt_release(DEFAULT_SEARCH_CONFIGURATION)

    baseline = estimate_execution(
        DEFAULT_SEARCH_CONFIGURATION,
        prompt_release,
        build_jev_atomic_policy(),
        max_jobs=1,
    )
    changed = estimate_execution(
        _TWO_CRITERIA_CONFIGURATION,
        prompt_release,
        build_jev_atomic_policy(),
        max_jobs=1,
    )

    assert changed.logical_model_calls_per_job == len(prompt_release.versions)
    assert changed.logical_model_calls_per_job == baseline.logical_model_calls_per_job
    assert changed.maximum_provider_attempts == baseline.maximum_provider_attempts


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
