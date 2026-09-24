from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from job_finder.benchmarks.comparisons import compare_runs
from job_finder.benchmarks.executions import EvaluationRun
from job_finder.benchmarks.manifests import (
    EvaluationCaseInput,
    EvaluationManifest,
    EvaluationManifestCase,
    ManifestPolicy,
)
from job_finder.benchmarks.scoring import EvaluationTrialResult, score_results
from job_finder.evaluation.models import PromptReleaseId, ReleaseTarget, RelevanceReleaseId

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


def test_compares_case_transitions_deterministically_for_distinct_release_targets() -> None:
    manifest = _manifest()
    baseline_results = (
        _result(0, 0, "rejected", "qualified", "false_positive"),
        _result(0, 1, "rejected", "rejected", None),
        _result(0, 2, "rejected", "rejected", None),
        _result(1, 0, "qualified", None, "operational"),
        _result(2, 0, "qualified", "qualified", None),
    )
    candidate_results = (
        _result(2, 0, "qualified", "qualified", None),
        _result(1, 0, "qualified", "qualified", None),
        _result(0, 2, "rejected", "rejected", None),
        _result(0, 1, "rejected", "rejected", None),
        _result(0, 0, "rejected", "rejected", None),
    )
    baseline = _run("b" * 64, "c" * 64, manifest, baseline_results)
    candidate = _run("d" * 64, "e" * 64, manifest, candidate_results)

    comparison = compare_runs(manifest, baseline, candidate)

    assert baseline.target is not None
    assert candidate.target is not None
    assert baseline.target.prompt_release_id == candidate.target.prompt_release_id
    assert comparison.baseline_target == baseline.target
    assert comparison.candidate_target == candidate.target
    assert comparison.improvement_count == 2
    assert comparison.regression_count == 0
    assert [trial.transition for case in comparison.cases for trial in case.trials] == [
        "improvement",
        "unchanged",
        "unchanged",
        "improvement",
        "unchanged",
    ]
    assert compare_runs(manifest, baseline, candidate) == comparison
    with pytest.raises(ValueError, match="metrics must match"):
        compare_runs(
            manifest,
            baseline,
            candidate.model_copy(update={"metrics": baseline.metrics}),
        )
    with pytest.raises(ValueError, match="release targets must differ"):
        compare_runs(manifest, baseline, baseline.model_copy(update={"id": "f" * 64}))


def test_a_candidate_under_the_absolute_threshold_can_still_regress_against_baseline() -> None:
    policy = ManifestPolicy(
        regular_trial_count=4,
        critical_trial_count=5,
        max_false_positive_rate=Decimal("0.4"),
        max_false_negative_rate=Decimal("0.5"),
    )
    manifest = EvaluationManifest(
        id="a" * 64,
        policy=policy,
        cases=(
            _case(0, "rejected", False, 4),
            _case(1, "rejected", True, 5),
            _case(2, "qualified", False, 4),
        ),
        created_at=NOW,
        created_by="test",
    )
    perfect = tuple(
        _result(position, trial, expected, expected, None)
        for position, expected, trials in (
            (0, "rejected", 4),
            (1, "rejected", 5),
            (2, "qualified", 4),
        )
        for trial in range(trials)
    )
    regressed = list(perfect)
    regressed[0] = _result(0, 0, "rejected", "qualified", "false_positive")

    comparison = compare_runs(
        manifest,
        _run("b" * 64, "c" * 64, manifest, perfect),
        _run("d" * 64, "e" * 64, manifest, tuple(regressed)),
    )

    assert comparison.eligible is False
    assert comparison.eligibility_failures == (
        "Candidate regresses against baseline false positives",
    )


def _manifest() -> EvaluationManifest:
    return EvaluationManifest(
        id="a" * 64,
        policy=ManifestPolicy(),
        cases=(
            _case(0, "rejected", True, 3),
            _case(1, "qualified", False, 1),
            _case(2, "qualified", False, 1),
        ),
        created_at=NOW,
        created_by="test",
    )


def _case(
    position: int,
    expected: str,
    critical: bool,
    trial_count: int,
) -> EvaluationManifestCase:
    return EvaluationManifestCase.model_validate(
        {
            "position": position,
            "curation_id": UUID(int=position + 1),
            "review_event_id": UUID(int=position + 10),
            "expected_outcome": expected,
            "critical": critical,
            "trial_count": trial_count,
            "input": EvaluationCaseInput(
                title="Engineer",
                company="Acme",
                url=f"https://example.com/{position}",
                source="other",
                description="Build useful tools.",
                location="Remote",
                keywords=("python",),
                date_posted=date(2026, 9, 10),
                observed_at=NOW,
                original_outcome="qualified",
                review_decision="pursue",
                target_profile="applied-ai-product-engineer",
            ),
        }
    )


def _result(
    position: int,
    trial: int,
    expected: str,
    actual: str | None,
    failure: str | None,
) -> EvaluationTrialResult:
    return EvaluationTrialResult.model_validate(
        {
            "id": f"{position * 10 + trial + 1:064x}",
            "case_position": position,
            "trial_index": trial,
            "expected_outcome": expected,
            "actual_outcome": actual,
            "failure_kind": failure,
            "reason": "Fixture result.",
        }
    )


def _run(
    run_id: str,
    relevance_release_id: str,
    manifest: EvaluationManifest,
    results: tuple[EvaluationTrialResult, ...],
) -> EvaluationRun:
    prompt_release_id = PromptReleaseId("9" * 64)
    return EvaluationRun(
        id=run_id,
        idempotency_key=f"run:{run_id}",
        manifest_id=manifest.id,
        prompt_release_id=prompt_release_id,
        target=ReleaseTarget(
            prompt_release_id=prompt_release_id,
            relevance_release_id=RelevanceReleaseId(relevance_release_id),
        ),
        implementation_ref="test",
        metrics=score_results(manifest, results),
        results=results,
        completed_at=NOW,
    )
