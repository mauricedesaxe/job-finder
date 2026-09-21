from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from job_finder.evaluation.manifests import (
    EvaluationCaseInput,
    EvaluationManifest,
    EvaluationManifestCase,
    EvaluationMetrics,
    EvaluationRun,
    EvaluationTrialResult,
    ManifestPolicy,
    score_results,
)
from job_finder.evaluation.models import (
    PromptReleaseId,
    ReleaseTarget,
    RelevanceReleaseId,
)

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


def test_scores_false_positives_false_negatives_and_operational_failures_separately() -> None:
    manifest = _manifest()
    results = (
        _result(0, 0, "rejected", "qualified", "false_positive"),
        _result(0, 1, "rejected", "rejected", None),
        _result(0, 2, "rejected", "rejected", None),
        _result(1, 0, "qualified", "rejected", "false_negative"),
        _result(2, 0, "qualified", None, "operational"),
    )

    metrics = score_results(manifest, results)

    assert metrics.result_count == 5
    assert metrics.false_positive_count == 1
    assert metrics.false_positive_rate == Decimal("0.3333333")
    assert metrics.false_negative_count == 1
    assert metrics.false_negative_rate == Decimal("0.5000000")
    assert metrics.operational_failure_count == 1
    assert metrics.critical_false_positive_count == 1


def test_rejects_results_that_do_not_cover_each_trial_once() -> None:
    manifest = _manifest()
    incomplete = (_result(0, 0, "rejected", "rejected", None),)

    with pytest.raises(ValueError, match="configured trial"):
        score_results(manifest, incomplete)


def test_evaluation_run_exposes_only_complete_release_targets() -> None:
    prompt_release_id = PromptReleaseId("b" * 64)
    target = ReleaseTarget(
        prompt_release_id=prompt_release_id,
        relevance_release_id=RelevanceReleaseId("c" * 64),
    )
    run = EvaluationRun(
        id="d" * 64,
        idempotency_key="run",
        manifest_id="a" * 64,
        prompt_release_id=prompt_release_id,
        target=target,
        implementation_ref="test",
        metrics=EvaluationMetrics(
            result_count=1,
            false_positive_count=0,
            false_negative_count=0,
            operational_failure_count=0,
            critical_false_positive_count=0,
            false_positive_rate=Decimal(0),
            false_negative_rate=Decimal(0),
        ),
        results=(_result(0, 0, "qualified", "qualified", None),),
        completed_at=NOW,
    )

    assert run.target == target
    assert run.model_dump(mode="json")["target"] == target.model_dump(mode="json")
    assert run.model_copy(update={"target": None}).target is None
    with pytest.raises(ValidationError, match="match prompt provenance"):
        EvaluationRun.model_validate(
            {
                **run.model_dump(mode="json"),
                "target": {
                    "prompt_release_id": "e" * 64,
                    "relevance_release_id": "c" * 64,
                },
            }
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
