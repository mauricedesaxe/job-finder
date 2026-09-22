from __future__ import annotations

from job_finder.execution_budget import estimate_execution
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
