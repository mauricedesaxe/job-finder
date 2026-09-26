from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from datetime import date
from typing import Any, cast

from langfuse import Evaluation, Langfuse
from langfuse.api import DatasetItem
from langfuse.experiment import ExperimentItem, ExperimentItemResult
from pydantic import BaseModel, JsonValue, TypeAdapter

from job_finder.ats.models import AtsEvidence, AtsNotApplicable
from job_finder.ats.policy import ats_structural_filter, format_location_context
from job_finder.evaluation.evaluate import _filter_evidence, job_message
from job_finder.evaluation.jev import JevCriterionObservation, evaluate_prompt
from job_finder.evaluation.prompt_releases import PromptVersion, build_prompt_release
from job_finder.evaluation.relevance_releases import (
    JevAtomicExecutionPolicy,
    build_jev_atomic_policy,
)
from job_finder.jobs.listings import JobListing
from job_finder.jobs.structural_filter import StructuralRejection

_ATS_EVIDENCE: TypeAdapter[AtsEvidence] = TypeAdapter(AtsEvidence)
_CRITERION = "remote-europe-eligible"


class Arguments(argparse.Namespace):
    dataset: str = ""
    run_name: str = ""
    minimum_rejection_rate: float = 0.8


class LocationDatasetInput(BaseModel):
    title: str
    company: str
    url: str
    location: str
    description: str
    ats_evidence: JsonValue | None


class ExpectedLocationOutcome(BaseModel):
    eligible_remote_from_romania: bool


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate remote-from-Romania Langfuse cases")
    _ = parser.add_argument("--dataset", required=True)
    _ = parser.add_argument("--run-name", required=True)
    _ = parser.add_argument("--minimum-rejection-rate", type=float, default=0.8)
    arguments = parser.parse_args(argv, namespace=Arguments())
    if not 0 <= arguments.minimum_rejection_rate <= 1:
        parser.error("--minimum-rejection-rate must be between 0 and 1")

    client = Langfuse(
        public_key=_required_environment("LANGFUSE_PUBLIC_KEY"),
        secret_key=_required_environment("LANGFUSE_SECRET_KEY"),
        host=_required_environment("LANGFUSE_BASE_URL"),
    )
    api_key = _required_environment("TYPESAFE_API_KEY")
    prompt = next(
        version
        for version in build_prompt_release().versions
        if version.definition.criterion == _CRITERION
    )
    policy = build_jev_atomic_policy()
    dataset = client.get_dataset(arguments.dataset)
    gold = [item for item in dataset.items if item.expected_output is not None]
    if not gold:
        raise ValueError("The location dataset has no scored cases")

    def classify(*, item: ExperimentItem, **_kwargs: dict[str, Any]) -> dict[str, object]:
        return _classify(cast(DatasetItem, item), prompt, policy, api_key)

    result = client.run_experiment(
        name="Remote from Romania eligibility",
        run_name=arguments.run_name,
        description="Frozen production feedback; structural ATS and Jev location criterion only",
        data=gold,
        task=classify,
        evaluators=[_exact_match],
        max_concurrency=3,
        metadata={"criterion": _CRITERION, "dataset": arguments.dataset},
    )
    negative_caught, negative_total, positive_kept, positive_total = _counts(result.item_results)
    lines = (
        f"Dataset: {arguments.dataset}",
        f"Run: {result.dataset_run_url}",
        f"Location rejects caught: {negative_caught}/{negative_total}",
        f"Remote-eligible controls kept: {positive_kept}/{positive_total}",
    )
    _ = sys.stdout.write("\n".join(lines) + "\n")
    return int(
        negative_total == 0
        or negative_caught / negative_total < arguments.minimum_rejection_rate
        or positive_kept != positive_total
    )


def _classify(
    item: DatasetItem, prompt: PromptVersion, policy: JevAtomicExecutionPolicy, api_key: str
) -> dict[str, object]:
    data = LocationDatasetInput.model_validate(item.input)
    evidence = (
        _ATS_EVIDENCE.validate_python(data.ats_evidence)
        if data.ats_evidence is not None
        else AtsNotApplicable()
    )
    structural = ats_structural_filter(evidence)
    if isinstance(structural, StructuralRejection):
        return {
            "eligible_remote_from_romania": False,
            "route": "ats_structural",
            "reason": structural.reason,
        }
    job = JobListing(
        title=data.title,
        company=data.company,
        url=data.url,
        source="other",
        keywords_matched=(),
        date_posted=None,
        date_scraped=date.today(),
        description=data.description,
        location=data.location,
    )
    context = (
        format_location_context(evidence, job.description) if evidence.kind == "available" else ""
    )
    result = evaluate_prompt(
        prompt,
        {"job": _filter_evidence(_CRITERION, job_message(job), context)},
        api_key=api_key,
        execution_policy=policy,
    )
    if not isinstance(result, JevCriterionObservation):
        raise RuntimeError(f"Jev location evaluation failed: {result.error_code}")
    return {
        "eligible_remote_from_romania": result.result.passed,
        "route": "jev_atomic",
        "reason": result.result.reason,
    }


def _exact_match(
    *,
    input: Any,
    output: Any,
    expected_output: Any,
    metadata: dict[str, Any] | None,
    **_kwargs: dict[str, Any],
) -> Evaluation:
    observed = cast(dict[str, object], output)["eligible_remote_from_romania"]
    expected = ExpectedLocationOutcome.model_validate(expected_output)
    return Evaluation(
        name="location_eligibility_correct",
        value=float(observed == expected.eligible_remote_from_romania),
    )


def _counts(results: Sequence[ExperimentItemResult]) -> tuple[int, int, int, int]:
    negative_total = positive_total = negative_caught = positive_kept = 0
    for case in results:
        if case.output is None:
            raise RuntimeError("A location evaluation case failed without an output")
        item = cast(DatasetItem, case.item)
        expected = ExpectedLocationOutcome.model_validate(item.expected_output)
        observed = cast(dict[str, bool], case.output)["eligible_remote_from_romania"]
        if expected.eligible_remote_from_romania:
            positive_total += 1
            positive_kept += int(observed)
        else:
            negative_total += 1
            negative_caught += int(not observed)
    return negative_caught, negative_total, positive_kept, positive_total


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"{name} is required")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
