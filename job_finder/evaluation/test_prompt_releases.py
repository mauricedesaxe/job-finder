from __future__ import annotations

from job_finder.evaluation.prompt_releases import (
    MODEL,
    OUTPUT_SCHEMA,
    PARAMETERS,
    build_evaluation_prompt_release,
)


def test_builds_the_complete_evaluation_release() -> None:
    release = build_evaluation_prompt_release()

    assert release.name == "release-2026-09-10-1"
    assert len(release.versions) == 6
    assert [version.definition.phase for version in release.versions] == [
        "filter",
        "filter",
        "filter",
        "filter",
        "profile",
        "profile",
    ]
    assert len({version.id for version in release.versions}) == 6
    assert release.id == release.content_digest


def test_preserves_the_prompt_execution_contract() -> None:
    release = build_evaluation_prompt_release()

    assert MODEL == "google/gemini-2.5-flash"
    assert PARAMETERS == {"temperature": 0, "max_tokens": 256}
    assert OUTPUT_SCHEMA["required"] == ["pass", "reason"]
    assert [version.definition.inputs for version in release.versions] == [
        ("job",),
        ("job", "rates"),
        ("job",),
        ("job",),
        ("job",),
        ("job",),
    ]


def test_derives_stable_content_identities() -> None:
    first = build_evaluation_prompt_release()
    second = build_evaluation_prompt_release()

    assert first == second
    assert all(len(version.id) == 64 for version in first.versions)
