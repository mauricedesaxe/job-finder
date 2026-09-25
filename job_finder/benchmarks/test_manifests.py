from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from job_finder.benchmarks.manifests import (
    CuratedReviewEvent,
    EvaluationCaseInput,
    EvaluationManifest,
    EvaluationManifestCase,
    ManifestPolicy,
)

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


def test_policy_rejects_critical_trial_counts_that_do_not_exceed_regular_trials() -> None:
    with pytest.raises(ValidationError, match="more trials than regular cases"):
        ManifestPolicy(regular_trial_count=3, critical_trial_count=3)


def test_policy_rejects_a_false_positive_threshold_not_stricter_than_false_negatives() -> None:
    with pytest.raises(ValidationError, match="false-positive threshold must be stricter"):
        ManifestPolicy(max_false_positive_rate=Decimal("0.10"))


def test_manifest_rejects_misordered_case_positions() -> None:
    with pytest.raises(ValidationError, match="ordered and follow the trial policy"):
        _manifest(
            _case(1, "rejected", True, 3),
            _case(0, "qualified", False, 1),
        )


def test_manifest_rejects_case_trial_counts_that_diverge_from_the_policy() -> None:
    with pytest.raises(ValidationError, match="ordered and follow the trial policy"):
        _manifest(_case(0, "rejected", True, 2))
    with pytest.raises(ValidationError, match="ordered and follow the trial policy"):
        _manifest(_case(0, "qualified", False, 3))


def test_curation_rejects_excluded_feedback_that_defines_evaluation_behavior() -> None:
    with pytest.raises(ValidationError, match="cannot define evaluation behavior"):
        _event("exclude", "qualified", False)
    with pytest.raises(ValidationError, match="cannot define evaluation behavior"):
        _event("exclude", None, True)


def test_curation_rejects_included_feedback_without_an_expected_outcome() -> None:
    with pytest.raises(ValidationError, match="requires an expected outcome"):
        _event("include", None, False)


def _manifest(*cases: EvaluationManifestCase) -> EvaluationManifest:
    return EvaluationManifest(
        id="a" * 64,
        policy=ManifestPolicy(),
        cases=tuple(cases),
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


def _event(
    action: str,
    expected_outcome: str | None,
    critical: bool,
) -> CuratedReviewEvent:
    return CuratedReviewEvent.model_validate(
        {
            "id": UUID(int=1),
            "review_event_id": UUID(int=2),
            "action": action,
            "expected_outcome": expected_outcome,
            "critical": critical,
            "reason": "Fixture curation.",
            "actor": "test",
            "created_at": NOW,
        }
    )
