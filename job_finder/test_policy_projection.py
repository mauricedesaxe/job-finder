from __future__ import annotations

from job_finder.acquisition_policy import (
    acquisition_policy_revision_id,
    build_search_queries as build_acquisition_queries,
)
from job_finder.evaluation.prompt_releases import build_prompt_release
from job_finder.policy_projection import project_legacy_search_configuration
from job_finder.qualification_definition import qualification_definition_revision_id
from job_finder.search_configuration import (
    DEFAULT_SEARCH_CONFIGURATION,
    build_search_queries as build_legacy_queries,
)


def test_legacy_projection_preserves_executable_searches_and_prompts() -> None:
    configuration = DEFAULT_SEARCH_CONFIGURATION
    projected = project_legacy_search_configuration(configuration)

    assert projected.acquisition.search_keywords == configuration.search_keywords
    assert projected.acquisition.enabled_sources == configuration.enabled_sources
    assert projected.qualification.personal_criteria == configuration.personal_criteria
    assert projected.qualification.target_profiles == configuration.target_profiles
    assert build_acquisition_queries(projected.acquisition) == build_legacy_queries(configuration)
    assert build_prompt_release(projected.qualification) == build_prompt_release(configuration)


def test_policy_identities_change_only_with_their_own_authored_content() -> None:
    configuration = DEFAULT_SEARCH_CONFIGURATION
    original = project_legacy_search_configuration(configuration)
    different_search = project_legacy_search_configuration(
        configuration.model_copy(
            update={"search_keywords": (*configuration.search_keywords, "new role")}
        )
    )
    first_criterion = configuration.personal_criteria[0]
    renamed_criterion = first_criterion.model_copy(update={"name": "Renamed criterion"})
    renamed = project_legacy_search_configuration(
        configuration.model_copy(
            update={
                "personal_criteria": (
                    renamed_criterion,
                    *configuration.personal_criteria[1:],
                )
            }
        )
    )

    assert acquisition_policy_revision_id(different_search.acquisition) != (
        acquisition_policy_revision_id(original.acquisition)
    )
    assert qualification_definition_revision_id(different_search.qualification) == (
        qualification_definition_revision_id(original.qualification)
    )
    assert acquisition_policy_revision_id(renamed.acquisition) == (
        acquisition_policy_revision_id(original.acquisition)
    )
    assert qualification_definition_revision_id(renamed.qualification) != (
        qualification_definition_revision_id(original.qualification)
    )
    assert (
        build_prompt_release(renamed.qualification).id
        == build_prompt_release(original.qualification).id
    )
