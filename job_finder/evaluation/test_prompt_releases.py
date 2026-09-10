from __future__ import annotations

from job_finder.evaluation.prompt_releases import (
    DEDUPLICATION_OUTPUT_SCHEMA,
    ENRICHMENT_OUTPUT_SCHEMA,
    EVALUATION_OUTPUT_SCHEMA,
    MODEL,
    build_prompt_release,
)


def test_builds_the_complete_prompt_release() -> None:
    release = build_prompt_release()

    assert release.name == "release-2026-09-10-2"
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
    assert release.id == "d4bcb6252f72ccbc4a80db33334f08676f0987824f1dc4d06d30af7aa8a98d7c"


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
