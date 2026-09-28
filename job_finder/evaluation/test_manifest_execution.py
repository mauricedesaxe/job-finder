from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import JsonValue, ValidationError

from job_finder.ats.models import ApplicationQuestion, AtsAvailable
from job_finder.benchmarks.manifests import EvaluationCaseInput, EvaluationManifestCase
from job_finder.evaluation.evaluate import evaluate_job
from job_finder.evaluation.manifest_execution import _case_evaluator  # pyright: ignore[reportPrivateUsage]
from job_finder.evaluation.manifest_execution import _ATS_EVIDENCE  # pyright: ignore[reportPrivateUsage]
from job_finder.evaluation.models import (
    CriterionAccepted,
    CriterionResult,
    ProviderRequestObservation,
    Qualified,
    ReleaseTarget,
)
from job_finder.evaluation.prompt_releases import PromptVersion, build_prompt_release
from job_finder.evaluation.relevance_releases import (
    RelevanceExecutionPolicy,
    build_gemini_policy,
    build_jev_atomic_policy,
    build_relevance_release,
)
from job_finder.jobs.listings import JobListing


@pytest.mark.parametrize(
    ("policy", "openrouter_api_key", "typesafe_api_key", "expected_message"),
    (
        (build_gemini_policy(build_prompt_release()), None, "typesafe", "OPENROUTER_API_KEY"),
        (build_jev_atomic_policy(), "openrouter", None, "TYPESAFE_API_KEY"),
    ),
)
def test_case_evaluator_requires_the_selected_provider_key(
    policy: RelevanceExecutionPolicy,
    openrouter_api_key: str | None,
    typesafe_api_key: str | None,
    expected_message: str,
) -> None:
    release = build_prompt_release()
    target = ReleaseTarget(
        prompt_release_id=release.id,
        relevance_release_id=build_relevance_release(policy).id,
    )

    with pytest.raises(ValueError, match=expected_message):
        _case_evaluator(
            release=release,
            target=target,
            relevance_policy=policy,
            rates="",
            openrouter_api_key=openrouter_api_key,
            typesafe_api_key=typesafe_api_key,
            record_request=_ignore_request,
        )


def test_case_evaluator_rejects_release_target_drift_before_provider_work() -> None:
    release = build_prompt_release()
    policy = build_gemini_policy(release)
    target = ReleaseTarget(
        prompt_release_id=release.id,
        relevance_release_id=build_relevance_release(policy).id,
    )
    evaluator = _case_evaluator(
        release=release,
        target=target,
        relevance_policy=policy,
        rates="",
        openrouter_api_key="openrouter",
        typesafe_api_key=None,
        record_request=_ignore_request,
    )

    with pytest.raises(ValueError, match="target changed"):
        evaluator(
            EvaluationManifestCase(
                position=0,
                curation_id=UUID(int=1),
                review_event_id=UUID(int=2),
                expected_outcome="qualified",
                critical=False,
                trial_count=1,
                input=EvaluationCaseInput(
                    title="Senior engineer",
                    company="Example",
                    url="https://example.com/job",
                    source="other",
                    description="Build products.",
                    location="Remote",
                    keywords=("senior engineer",),
                    date_posted=None,
                    observed_at=datetime(2026, 9, 22, tzinfo=UTC),
                    original_outcome="qualified",
                    review_decision="pursue",
                    target_profile=None,
                ),
            ),
            target.model_copy(update={"relevance_release_id": "0" * 64}),
            0,
        )


def test_case_evaluator_validates_ats_evidence_before_any_evaluation() -> None:
    release = build_prompt_release()
    policy = build_gemini_policy(release)
    target = ReleaseTarget(
        prompt_release_id=release.id,
        relevance_release_id=build_relevance_release(policy).id,
    )
    evaluator = _case_evaluator(
        release=release,
        target=target,
        relevance_policy=policy,
        rates="",
        openrouter_api_key="openrouter",
        typesafe_api_key=None,
        record_request=_ignore_request,
    )

    with pytest.raises(ValidationError):
        evaluator(_case(ats_evidence={"source": 42}), target, 0)


def test_case_evaluator_flows_recorded_ats_evidence_into_the_location_criterion_input() -> None:
    release = build_prompt_release()
    evidence = AtsAvailable(
        source="greenhouse",
        location="Remote",
        locations=("Remote",),
        workplace_type="Remote",
        country="United States",
        description="US only",
        application_questions=(
            ApplicationQuestion(
                label="Do you reside in one of these states?",
                required=True,
                choices=("California", "Oregon"),
            ),
        ),
    )
    case = _case(ats_evidence=evidence.model_dump(mode="json"))
    inputs: dict[str, str] = {}

    def evaluate(version: PromptVersion, values: Mapping[str, str]) -> CriterionResult:
        inputs[version.definition.criterion] = values["job"]
        return CriterionAccepted(
            prompt_name=version.definition.name,
            passed=True,
            reason=version.definition.criterion,
        )

    validated = _ATS_EVIDENCE.validate_python(case.input.ats_evidence)
    result = evaluate_job(_job(case), release, evaluate, rates="", ats_evidence=validated)

    assert isinstance(result, Qualified)
    assert (
        "- Application question (required): Do you reside in one of these states?"
        in inputs["remote-europe-eligible"]
    )
    assert "Choices: California, Oregon" in inputs["remote-europe-eligible"]
    assert "ATS job description:\nUS only" in inputs["remote-europe-eligible"]


def _case(ats_evidence: JsonValue | None) -> EvaluationManifestCase:
    return EvaluationManifestCase(
        position=0,
        curation_id=UUID(int=1),
        review_event_id=UUID(int=2),
        expected_outcome="qualified",
        critical=False,
        trial_count=1,
        input=EvaluationCaseInput(
            title="Senior engineer",
            company="Example",
            url="https://example.com/job",
            source="other",
            description="Build products.",
            location="Remote",
            keywords=("senior engineer",),
            date_posted=None,
            observed_at=datetime(2026, 9, 22, tzinfo=UTC),
            original_outcome="qualified",
            review_decision="pursue",
            target_profile=None,
            ats_evidence=ats_evidence,
        ),
    )


def _job(case: EvaluationManifestCase) -> JobListing:
    return JobListing.model_validate(
        {
            "title": case.input.title,
            "company": case.input.company,
            "url": case.input.url,
            "source": case.input.source,
            "keywords_matched": case.input.keywords,
            "date_posted": case.input.date_posted,
            "date_scraped": case.input.observed_at.date(),
            "description": case.input.description,
            "location": case.input.location,
        }
    )


def _ignore_request(_observation: ProviderRequestObservation) -> None:
    pass
