from __future__ import annotations

from decimal import Decimal
from typing import ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

import job_finder.benchmarks.manifests as _benchmark_manifests
from job_finder.benchmarks.identity import canonical_digest
from job_finder.evaluation.models import (
    EvaluationOutcome,
    EvaluationResult,
    evaluation_outcome,
)

_Digest = str


class _EvaluationModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class EvaluationTrialResult(_EvaluationModel):
    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    case_position: int = Field(ge=0)
    trial_index: int = Field(ge=0)
    expected_outcome: EvaluationOutcome
    actual_outcome: EvaluationOutcome | None
    failure_kind: Literal["false_positive", "false_negative", "operational"] | None
    reason: str

    @model_validator(mode="after")
    def classification_matches_outcomes(self) -> Self:
        expected_failure = _failure_kind(self.expected_outcome, self.actual_outcome)
        if self.failure_kind != expected_failure:
            raise ValueError("Trial failure kind must match its expected and actual outcomes")
        return self


class EvaluationMetrics(_EvaluationModel):
    result_count: int = Field(ge=0)
    false_positive_count: int = Field(ge=0)
    false_negative_count: int = Field(ge=0)
    operational_failure_count: int = Field(ge=0)
    critical_false_positive_count: int = Field(ge=0)
    false_positive_rate: Decimal = Field(ge=0, le=1)
    false_negative_rate: Decimal = Field(ge=0, le=1)

    @field_serializer("false_positive_rate", "false_negative_rate", when_used="json")
    def serialize_rates(self, value: Decimal) -> str:
        return format(value, "f")


def score_trial(
    run_id: _Digest,
    case: _benchmark_manifests.EvaluationManifestCase,
    trial_index: int,
    result: EvaluationResult,
) -> EvaluationTrialResult:
    actual = evaluation_outcome(result)
    failure = _failure_kind(case.expected_outcome, actual)
    result_id = canonical_digest(
        {"run_id": run_id, "case_position": case.position, "trial_index": trial_index}
    )
    return EvaluationTrialResult(
        id=result_id,
        case_position=case.position,
        trial_index=trial_index,
        expected_outcome=case.expected_outcome,
        actual_outcome=actual,
        failure_kind=failure,
        reason=result.reason,
    )


def score_results(
    manifest: _benchmark_manifests.EvaluationManifest,
    results: tuple[EvaluationTrialResult, ...],
) -> EvaluationMetrics:
    expected_count = sum(case.trial_count for case in manifest.cases)
    if len(results) != expected_count:
        raise ValueError("Results must account for every configured trial")
    expected_by_position = {case.position: case for case in manifest.cases}
    seen = {(result.case_position, result.trial_index) for result in results}
    required = {
        (case.position, trial_index)
        for case in manifest.cases
        for trial_index in range(case.trial_count)
    }
    if seen != required:
        raise ValueError("Results must cover each trial exactly once")
    if any(
        result.expected_outcome != expected_by_position[result.case_position].expected_outcome
        for result in results
    ):
        raise ValueError("Result expectations must match the manifest")
    false_positives = sum(result.failure_kind == "false_positive" for result in results)
    false_negatives = sum(result.failure_kind == "false_negative" for result in results)
    operational = sum(result.failure_kind == "operational" for result in results)
    negative_trials = sum(
        case.trial_count for case in manifest.cases if case.expected_outcome == "rejected"
    )
    positive_trials = expected_count - negative_trials
    critical_false_positives = sum(
        result.failure_kind == "false_positive"
        and expected_by_position[result.case_position].critical
        for result in results
    )
    return EvaluationMetrics(
        result_count=len(results),
        false_positive_count=false_positives,
        false_negative_count=false_negatives,
        operational_failure_count=operational,
        critical_false_positive_count=critical_false_positives,
        false_positive_rate=_rate(false_positives, negative_trials),
        false_negative_rate=_rate(false_negatives, positive_trials),
    )


def _failure_kind(
    expected: EvaluationOutcome, actual: EvaluationOutcome | None
) -> Literal["false_positive", "false_negative", "operational"] | None:
    if actual is None:
        return "operational"
    if actual == expected:
        return None
    return "false_positive" if actual == "qualified" else "false_negative"


def _rate(count: int, denominator: int) -> Decimal:
    if denominator == 0:
        return Decimal(0)
    return (Decimal(count) / Decimal(denominator)).quantize(Decimal("0.0000001"))
