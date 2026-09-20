from __future__ import annotations

import subprocess
import sys

from job_finder.evaluation.prompt_releases import (
    DEDUPLICATION_OUTPUT_SCHEMA,
    ENRICHMENT_OUTPUT_SCHEMA,
    EVALUATION_OUTPUT_SCHEMA,
    MODEL,
    build_prompt_release,
    build_prompt_version,
)
from job_finder.evaluation.prompts import PROMPTS, PromptDefinition
from job_finder.search_configuration import (
    DEFAULT_SEARCH_CONFIGURATION,
    PersonalCriterion,
    SupportedSearchSource,
    TargetProfile,
)


def test_search_configuration_imports_in_a_fresh_interpreter() -> None:
    completed = subprocess.run(
        [sys.executable, "-c", "import job_finder.search_configuration"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_builds_the_complete_prompt_release() -> None:
    release = build_prompt_release()

    assert release.name == "release-2026-09-14-1"
    assert len(release.versions) == 8
    assert [version.definition.phase for version in release.versions] == [
        "filter",
        "filter",
        "filter",
        "filter",
        "profile",
        "profile",
        "enrichment",
        "deduplication",
    ]
    assert len({version.id for version in release.versions}) == 8
    assert release.id == release.content_digest
    assert release.id == "4a97113e9adf11dcf56b8aecdb0414cf7ce484ef30935546f2d42b6dadaf1dc0"


def test_preserves_each_prompt_execution_contract() -> None:
    release = build_prompt_release()

    assert MODEL == "google/gemini-2.5-flash"
    assert EVALUATION_OUTPUT_SCHEMA["required"] == ["pass", "reason"]
    assert ENRICHMENT_OUTPUT_SCHEMA["required"] == [
        "title",
        "company",
        "description",
        "location",
    ]
    assert DEDUPLICATION_OUTPUT_SCHEMA["required"] == ["isDuplicate"]
    assert [version.definition.inputs for version in release.versions] == [
        ("job",),
        ("job", "rates"),
        ("job",),
        ("job",),
        ("job",),
        ("job",),
        ("job",),
        ("newTitle", "existingTitles"),
    ]
    assert [version.tool_name for version in release.versions[-2:]] == [
        "enrich_job",
        "check_duplicate",
    ]


def test_derives_stable_content_identities() -> None:
    first = build_prompt_release()
    second = build_prompt_release()

    assert first == second
    assert all(len(version.id) == 64 for version in first.versions)


def test_default_configuration_exactly_preserves_the_legacy_release() -> None:
    legacy = build_prompt_release()
    configured = build_prompt_release(DEFAULT_SEARCH_CONFIGURATION)

    assert configured == legacy
    assert configured.name == "release-2026-09-14-1"
    assert configured.id == "4a97113e9adf11dcf56b8aecdb0414cf7ce484ef30935546f2d42b6dadaf1dc0"
    assert [version.id for version in configured.versions] == [
        build_prompt_version(prompt).id for prompt in PROMPTS
    ]


def test_compiles_custom_configuration_deterministically_in_configuration_order() -> None:
    configuration = DEFAULT_SEARCH_CONFIGURATION.model_copy(
        update={
            "personal_criteria": (
                PersonalCriterion(key="custom-first", name="First", instructions="First filter."),
                DEFAULT_SEARCH_CONFIGURATION.personal_criteria[1],
            ),
            "target_profiles": (
                TargetProfile(key="custom-profile", name="Profile", instructions="Match profile."),
            ),
        }
    )

    first = build_prompt_release(configuration)
    second = build_prompt_release(configuration)

    assert first == second
    assert first.name == f"release-{first.content_digest}"
    assert [version.definition.criterion for version in first.versions] == [
        "custom-first",
        "compensation-minimum",
        "custom-profile",
        "enrichment",
        "title-deduplication",
    ]
    assert [version.definition.phase for version in first.versions[-2:]] == [
        "enrichment",
        "deduplication",
    ]
    assert first.versions[0].definition.name == "job-finder-configured-filter-custom-first"
    assert first.versions[2].definition.name == "job-finder-configured-profile-custom-profile"
    assert first.versions[1].definition.inputs == ("job", "rates")


def test_meaningful_prompt_configuration_changes_release_identity() -> None:
    base = DEFAULT_SEARCH_CONFIGURATION
    changed_instructions = base.model_copy(
        update={
            "personal_criteria": (
                base.personal_criteria[0].model_copy(update={"instructions": "Changed."}),
                *base.personal_criteria[1:],
            )
        }
    )
    reordered = base.model_copy(
        update={"personal_criteria": tuple(reversed(base.personal_criteria))}
    )
    changed_membership = base.model_copy(update={"personal_criteria": base.personal_criteria[:-1]})

    identities = {
        build_prompt_release(configuration).id
        for configuration in (base, changed_instructions, reordered, changed_membership)
    }

    assert len(identities) == 4


def test_display_and_discovery_configuration_do_not_change_release_identity() -> None:
    base = DEFAULT_SEARCH_CONFIGURATION
    display_only = base.model_copy(
        update={
            "search_keywords": ("different search",),
            "enabled_sources": (SupportedSearchSource.LEVER,),
            "personal_criteria": (
                base.personal_criteria[0].model_copy(update={"name": "Renamed criterion"}),
                *base.personal_criteria[1:],
            ),
            "target_profiles": (
                base.target_profiles[0].model_copy(update={"name": "Renamed profile"}),
                *base.target_profiles[1:],
            ),
        }
    )

    assert build_prompt_release(display_only) == build_prompt_release(base)


def test_treats_generic_instruction_braces_as_literal_text() -> None:
    instructions = 'Accept objects like {"pass": true}.'
    configuration = DEFAULT_SEARCH_CONFIGURATION.model_copy(
        update={
            "personal_criteria": (
                PersonalCriterion(
                    key="literal-json",
                    name="Literal JSON",
                    instructions=instructions,
                ),
            )
        }
    )

    version = build_prompt_release(configuration).version(
        "job-finder-configured-filter-literal-json"
    )

    assert version.messages[0]["content"].format_map({"job": "unused"}) == instructions


def test_preserves_rates_placeholder_in_edited_compensation_instructions() -> None:
    instructions = 'Use these rates: {rates}. Return {"pass": true}.'
    compensation = DEFAULT_SEARCH_CONFIGURATION.personal_criteria[1].model_copy(
        update={"instructions": instructions}
    )
    configuration = DEFAULT_SEARCH_CONFIGURATION.model_copy(
        update={
            "personal_criteria": (
                DEFAULT_SEARCH_CONFIGURATION.personal_criteria[0],
                compensation,
                *DEFAULT_SEARCH_CONFIGURATION.personal_criteria[2:],
            )
        }
    )

    version = build_prompt_release(configuration).version("job-finder-filter-compensation")

    assert (
        version.messages[0]["content"].format_map({"job": "unused", "rates": "1 EUR ~= 1.10 USD"})
        == 'Use these rates: 1 EUR ~= 1.10 USD. Return {"pass": true}.'
    )


def test_enrichment_version_overrides_the_model() -> None:
    release = build_prompt_release()

    version = release.version("job-finder-enrichment")

    assert version.model == "google/gemini-2.5-flash-lite"


def test_filter_version_keeps_the_default_model() -> None:
    release = build_prompt_release()

    version = release.version("job-finder-filter-location-eligibility")

    assert version.model == MODEL


def test_resolves_missing_model_to_the_default_and_keeps_explicit_overrides() -> None:
    default = PromptDefinition(
        name="job-finder-test-default",
        criterion="test",
        phase="filter",
        system_message="test",
    )
    override = PromptDefinition(
        name="job-finder-test-override",
        criterion="test",
        phase="filter",
        system_message="test",
        model="test/model",
    )

    assert build_prompt_version(default).model == MODEL
    assert build_prompt_version(override).model == "test/model"
