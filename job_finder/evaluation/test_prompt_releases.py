from __future__ import annotations

from job_finder.evaluation.prompt_releases import (
    DEDUPLICATION_OUTPUT_SCHEMA,
    ENRICHMENT_OUTPUT_SCHEMA,
    EVALUATION_OUTPUT_SCHEMA,
    MODEL,
    _build_version,
    build_prompt_release,
)
from job_finder.evaluation.prompts import PromptDefinition


def test_builds_the_complete_prompt_release() -> None:
    release = build_prompt_release()

    assert release.name == "release-2026-09-12-1"
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
    assert release.id == "a42fe63e013fa853684484cd08dcbc5ed2f7f8cda47069bb81b520c4e7164c1b"


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

    assert _build_version(default).model == MODEL
    assert _build_version(override).model == "test/model"
