from __future__ import annotations

import re
from collections.abc import Callable, Mapping

from job_finder.evaluation.models import (
    CriterionAccepted,
    CriterionResult,
    EvaluationResult,
    OperationalError,
    OperationalFailure,
    Qualified,
    Rejected,
)
from job_finder.evaluation.prompt_releases import PromptRelease, PromptVersion
from job_finder.jobs.models import JobListing

CriterionEvaluator = Callable[[PromptVersion, Mapping[str, str]], CriterionResult]
_FILTER_EVIDENCE_PATTERNS = {
    "remote-europe-eligible": re.compile(
        r"""remote|hybrid|on.?site|location|residen|work\ authori[sz]ation|work\ permit|visa|
        time.?zone|working\ hours|based\ in|located\ in|office|EMEA|Europe|UK|EEA|global|
        worldwide|country""",
        re.I | re.X,
    ),
    "compensation-minimum": re.compile(
        r"""compensation|salary|pay\ range|hourly|monthly|annual|per\ (?:year|hour|month)|
        [/\ ](?:yr|hr)|[€$£]|\b(?:USD|EUR|GBP)\b""",
        re.I | re.X,
    ),
}


def evaluate_job(
    job: JobListing,
    release: PromptRelease,
    evaluate: CriterionEvaluator,
    *,
    rates: str,
) -> EvaluationResult:
    job_input = job_message(job)
    filters = tuple(version for version in release.versions if version.definition.phase == "filter")
    profiles = tuple(
        version for version in release.versions if version.definition.phase == "profile"
    )
    if len(filters) != 4 or len(profiles) != 2:
        raise ValueError("Evaluation requires four filters and two profiles")

    filter_decision = _evaluate_filters(job_input, rates, filters, evaluate)
    if filter_decision is not None:
        return filter_decision
    return _evaluate_profiles(job_input, rates, profiles, evaluate)


def job_message(job: JobListing) -> str:
    return f"""Job Title: {job.title}
Company: {job.company}
Source: {job.source}
URL: {job.url}

Description:
{job.description}"""


def _evaluate_filters(
    job_input: str,
    rates: str,
    filters: tuple[PromptVersion, ...],
    evaluate: CriterionEvaluator,
) -> Rejected | OperationalFailure | None:
    filter_results = tuple(
        evaluate(version, _prompt_values(version, job_input, rates)) for version in filters
    )
    for result in filter_results:
        if isinstance(result, OperationalError):
            return result
        if not result.passed:
            return Rejected(reason=result.reason)

    return None


def _evaluate_profiles(
    job_input: str,
    rates: str,
    profiles: tuple[PromptVersion, ...],
    evaluate: CriterionEvaluator,
) -> EvaluationResult:
    profile_results = tuple(
        evaluate(version, _prompt_values(version, job_input, rates)) for version in profiles
    )
    last_rejection: CriterionAccepted | None = None
    first_error: OperationalFailure | None = None
    for version, result in zip(profiles, profile_results, strict=True):
        if isinstance(result, CriterionAccepted):
            if result.passed:
                return Qualified(reason=result.reason, profile_name=version.definition.criterion)
            last_rejection = result
        elif first_error is None:
            first_error = result
    if last_rejection is not None:
        return Rejected(reason=last_rejection.reason)
    if first_error is not None:
        return first_error
    return Rejected(reason="No profiles matched")


def _prompt_values(version: PromptVersion, job_input: str, rates: str) -> Mapping[str, str]:
    values = {"job": _filter_evidence(version.definition.criterion, job_input)}
    if "rates" in version.definition.inputs:
        values["rates"] = rates
    return values


def _filter_evidence(criterion: str, job_input: str) -> str:
    pattern = _FILTER_EVIDENCE_PATTERNS.get(criterion)
    if pattern is None:
        return job_input
    lines = job_input.splitlines()
    header = "\n".join(lines[:2])
    evidence = "\n".join(line for line in lines[2:] if pattern.search(line))
    return f"{header}\n\nDescription:\n{evidence or 'No relevant evidence stated.'}"
