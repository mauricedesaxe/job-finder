from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from job_finder.evaluation.models import ReleaseTarget
from job_finder.evaluation.prompt_releases import (
    build_prompt_release,
    build_work_culture_candidate_release,
)
from job_finder.evaluation.relevance_releases import (
    CodeArtifactIdentity,
    GeminiExecutionPolicy,
    JevAtomicExecutionPolicy,
    JevFaithfulExecutionPolicy,
    RelevanceRelease,
    RelevanceReleaseError,
    build_gemini_policy,
    build_jev_atomic_policy,
    build_jev_faithful_policy,
    build_relevance_release,
    build_work_culture_candidate_policy,
    source_artifact_identity,
    validate_release_target,
)


def test_builds_deterministic_content_identities_for_each_policy_variant() -> None:
    prompt_release = build_prompt_release()
    policies = (
        build_gemini_policy(prompt_release),
        build_jev_faithful_policy(prompt_release),
        build_jev_atomic_policy(),
    )

    releases = tuple(build_relevance_release(policy) for policy in policies)

    assert releases == tuple(build_relevance_release(policy) for policy in policies)
    assert len({release.id for release in releases}) == 3
    assert all(release.id == release.content_digest for release in releases)
    assert isinstance(releases[0].policy, GeminiExecutionPolicy)
    assert isinstance(releases[1].policy, JevFaithfulExecutionPolicy)
    assert isinstance(releases[2].policy, JevAtomicExecutionPolicy)
    assert tuple(
        type(RelevanceRelease.model_validate(release.model_dump()).policy) for release in releases
    ) == (GeminiExecutionPolicy, JevFaithfulExecutionPolicy, JevAtomicExecutionPolicy)


def test_exchange_rates_are_not_part_of_relevance_release_identity() -> None:
    first = build_relevance_release(build_jev_atomic_policy())
    second = build_relevance_release(build_jev_atomic_policy())

    assert first == second
    assert "rates" not in first.policy.model_dump(mode="json")


def test_model_development_question_creates_a_policy_only_candidate_release() -> None:
    candidate_policy = build_jev_atomic_policy()
    training_question = candidate_policy.questions["role-quality"]["primary_model_training"]
    research_question = candidate_policy.questions["role-quality"]["model_architecture_research"]
    baseline_questions = {
        criterion: dict(questions) for criterion, questions in candidate_policy.questions.items()
    }
    del baseline_questions["role-quality"]["primary_model_training"]
    del baseline_questions["role-quality"]["model_architecture_research"]
    baseline_policy = candidate_policy.model_copy(update={"questions": baseline_questions})

    baseline = build_relevance_release(baseline_policy)
    candidate = build_relevance_release(candidate_policy)

    assert "Treat fine-tuning an existing model as false" in training_question.instructions
    assert "pretraining strategies" in research_question.instructions
    assert "optimizes fine-tuning methods" in research_question.instructions
    assert training_question.true.startswith("Training new base or foundation models")
    assert baseline.id != candidate.id
    assert baseline_policy.input_serialization == candidate_policy.input_serialization
    assert baseline_policy.provider_adapter == candidate_policy.provider_adapter
    assert baseline_policy.decision_composition == candidate_policy.decision_composition


def test_hype_and_permanent_availability_are_a_conjunctive_rejection() -> None:
    baseline = build_jev_atomic_policy()
    policy = build_work_culture_candidate_policy(baseline)
    prompt_release = build_work_culture_candidate_release(build_prompt_release())
    relevance_release = build_relevance_release(policy)
    work_culture = policy.questions["work-culture"]

    assert set(work_culture) == {
        "extreme_intensity_culture",
        "permanent_personal_availability",
    }
    assert policy.provider_adapter == baseline.provider_adapter
    assert policy.decision_composition == baseline.decision_composition
    assert relevance_release.id == (
        "9e5135438df963af663db714da5138cd52c08e7b4f9b50f8cff2944859abfba1"
    )
    validate_release_target(
        ReleaseTarget(
            prompt_release_id=prompt_release.id,
            relevance_release_id=relevance_release.id,
        ),
        prompt_release,
        relevance_release,
    )


def test_policies_identify_the_actual_checked_in_execution_sources() -> None:
    prompt_release = build_prompt_release()
    evaluation_directory = Path(__file__).parent
    evaluate_digest = hashlib.sha256(
        (evaluation_directory / "evaluate.py").read_bytes()
    ).hexdigest()
    openrouter_digest = hashlib.sha256(
        (evaluation_directory / "openrouter.py").read_bytes()
    ).hexdigest()
    jev_digest = hashlib.sha256((evaluation_directory / "jev.py").read_bytes()).hexdigest()

    gemini = build_gemini_policy(prompt_release)
    faithful = build_jev_faithful_policy(prompt_release)
    atomic = build_jev_atomic_policy()

    assert gemini.input_serialization == CodeArtifactIdentity(
        entrypoint="job_finder.evaluation.evaluate:job_message",
        content_digest=evaluate_digest,
    )
    assert gemini.provider_adapter.content_digest == openrouter_digest
    assert gemini.decision_composition == (
        CodeArtifactIdentity(
            entrypoint="job_finder.evaluation.evaluate:evaluate_job",
            content_digest=evaluate_digest,
        ),
    )
    assert faithful.input_serialization.content_digest == evaluate_digest
    assert faithful.provider_adapter.content_digest == jev_digest
    assert {artifact.content_digest for artifact in faithful.decision_composition} == {
        evaluate_digest,
        jev_digest,
    }
    assert atomic.provider_adapter.content_digest == jev_digest
    assert {artifact.entrypoint for artifact in atomic.decision_composition} == {
        "job_finder.evaluation.evaluate:evaluate_job",
        "job_finder.evaluation.jev:_compose_release_atomic",
    }


def test_source_content_changes_release_identity_without_rewriting_project_sources(
    tmp_path: Path,
) -> None:
    source = tmp_path / "adapter.py"
    source.write_bytes(b"def execute():\n    return 1\n")
    first_artifact = source_artifact_identity("example.adapter:execute", source)
    source.write_bytes(b"def execute():\n    return 2\n")
    second_artifact = source_artifact_identity("example.adapter:execute", source)
    policy = build_gemini_policy(build_prompt_release())

    first = build_relevance_release(policy.model_copy(update={"provider_adapter": first_artifact}))
    second = build_relevance_release(
        policy.model_copy(update={"provider_adapter": second_artifact})
    )

    assert first_artifact.content_digest != second_artifact.content_digest
    assert first.id != second.id


def test_faithful_jev_allows_candidate_questions_with_exact_criterion_coverage() -> None:
    prompt_release = build_prompt_release()
    policy = build_jev_faithful_policy(prompt_release)
    questions = dict(policy.questions)
    criterion = next(iter(questions))
    questions[criterion] = questions[criterion].model_copy(
        update={"instructions": "Candidate question using {job}."}
    )
    relevance_release = build_relevance_release(policy.model_copy(update={"questions": questions}))
    target = ReleaseTarget(
        prompt_release_id=prompt_release.id,
        relevance_release_id=relevance_release.id,
    )

    validate_release_target(target, prompt_release, relevance_release)


@pytest.mark.parametrize(
    "artifact_name",
    ("input_serialization", "provider_adapter", "decision_composition"),
)
def test_gemini_rejects_source_artifact_mismatches(artifact_name: str) -> None:
    prompt_release = build_prompt_release()
    policy = build_gemini_policy(prompt_release)
    if artifact_name == "input_serialization":
        changed = policy.model_copy(
            update={"input_serialization": _stale_artifact(policy.input_serialization)}
        )
    elif artifact_name == "provider_adapter":
        changed = policy.model_copy(
            update={"provider_adapter": _stale_artifact(policy.provider_adapter)}
        )
    else:
        changed = policy.model_copy(
            update={
                "decision_composition": (
                    _stale_artifact(policy.decision_composition[0]),
                    *policy.decision_composition[1:],
                )
            }
        )
    relevance_release = build_relevance_release(changed)
    target = ReleaseTarget(
        prompt_release_id=prompt_release.id,
        relevance_release_id=relevance_release.id,
    )

    with pytest.raises(RelevanceReleaseError, match="current source artifacts"):
        validate_release_target(target, prompt_release, relevance_release)


@pytest.mark.parametrize(
    "artifact_name",
    ("input_serialization", "provider_adapter", "decision_composition"),
)
def test_jev_rejects_source_artifact_mismatches(artifact_name: str) -> None:
    prompt_release = build_prompt_release()
    policy = build_jev_atomic_policy()
    if artifact_name == "input_serialization":
        changed = policy.model_copy(
            update={"input_serialization": _stale_artifact(policy.input_serialization)}
        )
    elif artifact_name == "provider_adapter":
        changed = policy.model_copy(
            update={"provider_adapter": _stale_artifact(policy.provider_adapter)}
        )
    else:
        changed = policy.model_copy(
            update={
                "decision_composition": (
                    _stale_artifact(policy.decision_composition[0]),
                    *policy.decision_composition[1:],
                )
            }
        )
    relevance_release = build_relevance_release(changed)
    target = ReleaseTarget(
        prompt_release_id=prompt_release.id,
        relevance_release_id=relevance_release.id,
    )

    with pytest.raises(RelevanceReleaseError, match="current source artifacts"):
        validate_release_target(target, prompt_release, relevance_release)


def _stale_artifact(artifact: CodeArtifactIdentity) -> CodeArtifactIdentity:
    return artifact.model_copy(update={"content_digest": "0" * 64})


def test_faithful_jev_requires_exact_evaluation_criterion_coverage() -> None:
    prompt_release = build_prompt_release()
    policy = build_jev_faithful_policy(prompt_release)
    omitted = next(iter(policy.questions))
    incomplete_policy = policy.model_copy(
        update={
            "questions": {key: value for key, value in policy.questions.items() if key != omitted}
        }
    )
    relevance_release = build_relevance_release(incomplete_policy)
    target = ReleaseTarget(
        prompt_release_id=prompt_release.id,
        relevance_release_id=relevance_release.id,
    )

    with pytest.raises(RelevanceReleaseError, match="cover evaluation criteria exactly"):
        validate_release_target(target, prompt_release, relevance_release)


def test_atomic_jev_requires_exact_evaluation_criterion_coverage() -> None:
    prompt_release = build_prompt_release()
    policy = build_jev_atomic_policy()
    incomplete_policy = policy.model_copy(
        update={
            "questions": {
                key: value for key, value in policy.questions.items() if key != "role-quality"
            }
        }
    )

    with pytest.raises(ValueError, match="cover every criterion exactly"):
        build_relevance_release(incomplete_policy)

    relevance_release = build_relevance_release(policy)
    target = ReleaseTarget(
        prompt_release_id=prompt_release.id,
        relevance_release_id=relevance_release.id,
    )
    validate_release_target(target, prompt_release, relevance_release)


def test_gemini_requires_compatible_stored_prompt_models() -> None:
    prompt_release = build_prompt_release()
    policy = build_gemini_policy(prompt_release).model_copy(update={"model": "other/model"})
    relevance_release = build_relevance_release(policy)
    target = ReleaseTarget(
        prompt_release_id=prompt_release.id,
        relevance_release_id=relevance_release.id,
    )

    with pytest.raises(RelevanceReleaseError, match="stored prompt versions"):
        validate_release_target(target, prompt_release, relevance_release)
