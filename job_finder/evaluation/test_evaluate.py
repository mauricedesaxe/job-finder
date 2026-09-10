from __future__ import annotations

from collections.abc import Mapping
from datetime import date

from job_finder.evaluation.evaluate import evaluate_job, job_message
from job_finder.evaluation.models import (
    CriterionAccepted,
    CriterionResult,
    RetryableOperationalError,
    TerminalOperationalError,
    Qualified,
    Rejected,
)
from job_finder.evaluation.prompt_releases import PromptVersion, build_prompt_release
from job_finder.jobs.models import JobListing

JOB = JobListing(
    title="Senior Engineer",
    company="TestCo",
    url="https://example.test/job",
    source="other",
    keywords_matched=(),
    date_posted=None,
    date_scraped=date(2026, 9, 10),
    description="body",
)
RATES = "1 EUR ~= 1.10 USD"


def test_keeps_filters_and_profiles_eager_and_ordered() -> None:
    release = build_prompt_release()
    calls: list[str] = []

    def evaluate(version: PromptVersion, values: Mapping[str, str]) -> CriterionResult:
        calls.append(version.definition.criterion)
        if version.definition.criterion in ("remote-europe-eligible", "compensation-minimum"):
            assert values["job"].endswith("Description:\nNo relevant evidence stated.")
        else:
            assert values["job"] == job_message(JOB)
        if version.definition.criterion == "applied-ai-product-engineer":
            return _accepted(version, passed=True)
        return _accepted(version, passed=version.definition.phase == "filter")

    result = evaluate_job(JOB, release, evaluate, rates=RATES)

    assert calls == [
        "remote-europe-eligible",
        "compensation-minimum",
        "role-quality",
        "cheap-shop-placement",
        "early-stage-product-engineer",
        "applied-ai-product-engineer",
    ]
    assert result == Qualified(
        reason="applied-ai-product-engineer",
        profile_name="applied-ai-product-engineer",
    )


def test_isolates_location_and_compensation_evidence() -> None:
    release = build_prompt_release()
    job = JOB.model_copy(
        update={
            "description": """Remote across Europe.
Our compensation reflects labor costs across U.S. geographic markets.
Build customer-facing AI features."""
        }
    )
    inputs: dict[str, str] = {}

    def evaluate(version: PromptVersion, values: Mapping[str, str]) -> CriterionResult:
        inputs[version.definition.criterion] = values["job"]
        return _accepted(version, passed=True)

    _ = evaluate_job(job, release, evaluate, rates=RATES)

    assert "Remote across Europe." in inputs["remote-europe-eligible"]
    assert "compensation reflects" not in inputs["remote-europe-eligible"]
    assert "compensation reflects" in inputs["compensation-minimum"]
    assert "Remote across Europe." not in inputs["compensation-minimum"]
    assert "Build customer-facing AI features." in inputs["early-stage-product-engineer"]


def test_returns_the_first_filter_result_in_catalog_order() -> None:
    release = build_prompt_release()
    calls: list[str] = []

    def evaluate(version: PromptVersion, _values: Mapping[str, str]) -> CriterionResult:
        calls.append(version.definition.criterion)
        if version.definition.criterion == "remote-europe-eligible":
            return RetryableOperationalError(
                prompt_name=version.definition.name,
                error_code="timeout",
                reason="first failed",
            )
        if version.definition.criterion == "compensation-minimum":
            return _accepted(version, passed=False)
        return _accepted(version, passed=True)

    result = evaluate_job(JOB, release, evaluate, rates=RATES)

    assert len(calls) == 4
    assert result == RetryableOperationalError(
        prompt_name=release.versions[0].definition.name,
        error_code="timeout",
        reason="first failed",
    )


def test_preserves_a_terminal_filter_error() -> None:
    release = build_prompt_release()

    def evaluate(version: PromptVersion, _values: Mapping[str, str]) -> CriterionResult:
        if version.definition.criterion == "remote-europe-eligible":
            return TerminalOperationalError(
                prompt_name=version.definition.name,
                error_code="http_400",
                reason="invalid request",
            )
        return _accepted(version, passed=True)

    result = evaluate_job(JOB, release, evaluate, rates=RATES)

    assert isinstance(result, TerminalOperationalError)
    assert result.error_code == "http_400"


def test_stops_before_profiles_after_a_filter_rejection() -> None:
    release = build_prompt_release()
    calls: list[str] = []

    def evaluate(version: PromptVersion, _values: Mapping[str, str]) -> CriterionResult:
        calls.append(version.definition.criterion)
        return _accepted(
            version,
            passed=version.definition.criterion != "role-quality",
        )

    result = evaluate_job(JOB, release, evaluate, rates=RATES)

    assert result == Rejected(reason="role-quality")
    assert calls == [version.definition.criterion for version in release.versions[:4]]


def test_uses_the_first_passing_profile_in_catalog_order() -> None:
    release = build_prompt_release()

    def evaluate(version: PromptVersion, _values: Mapping[str, str]) -> CriterionResult:
        return _accepted(version, passed=True)

    result = evaluate_job(JOB, release, evaluate, rates=RATES)

    assert result == Qualified(
        reason="early-stage-product-engineer",
        profile_name="early-stage-product-engineer",
    )


def test_uses_the_last_fulfilled_profile_rejection() -> None:
    release = build_prompt_release()

    def evaluate(version: PromptVersion, _values: Mapping[str, str]) -> CriterionResult:
        return _accepted(version, passed=version.definition.phase == "filter")

    result = evaluate_job(JOB, release, evaluate, rates=RATES)

    assert result == Rejected(reason="applied-ai-product-engineer")


def test_returns_a_rejection_when_the_other_profile_is_unavailable() -> None:
    release = build_prompt_release()

    def evaluate(version: PromptVersion, _values: Mapping[str, str]) -> CriterionResult:
        if version.definition.criterion == "early-stage-product-engineer":
            return RetryableOperationalError(
                prompt_name=version.definition.name,
                error_code="timeout",
                reason="profile failed",
            )
        return _accepted(version, passed=version.definition.phase == "filter")

    result = evaluate_job(JOB, release, evaluate, rates=RATES)

    assert result == Rejected(reason="applied-ai-product-engineer")


def test_returns_the_first_error_when_all_profiles_are_unavailable() -> None:
    release = build_prompt_release()

    def evaluate(version: PromptVersion, _values: Mapping[str, str]) -> CriterionResult:
        if version.definition.phase == "filter":
            return _accepted(version, passed=True)
        return RetryableOperationalError(
            prompt_name=version.definition.name,
            error_code="timeout",
            reason=version.definition.criterion,
        )

    result = evaluate_job(JOB, release, evaluate, rates=RATES)

    assert result == RetryableOperationalError(
        prompt_name=release.versions[4].definition.name,
        error_code="timeout",
        reason="early-stage-product-engineer",
    )


def _accepted(version: PromptVersion, *, passed: bool) -> CriterionAccepted:
    return CriterionAccepted(
        prompt_name=version.definition.name,
        passed=passed,
        reason=version.definition.criterion,
    )
