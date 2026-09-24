from __future__ import annotations

import hashlib
from typing import ClassVar, Literal

import psycopg
from pydantic import BaseModel, ConfigDict, Field

import job_finder.benchmarks.executions as _executions
import job_finder.benchmarks.manifests as _benchmark_manifests
import job_finder.benchmarks.scoring as _scoring
from job_finder.evaluation.models import ReleaseTarget

_Connection = psycopg.Connection[tuple[object, ...]]
_Digest = str


class _EvaluationModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class EvaluationTrialTransition(_EvaluationModel):
    case_position: int = Field(ge=0)
    trial_index: int = Field(ge=0)
    baseline: _scoring.EvaluationTrialResult
    candidate: _scoring.EvaluationTrialResult
    transition: Literal["unchanged", "improvement", "regression", "changed_failure"]


class EvaluationCaseTransition(_EvaluationModel):
    case_position: int = Field(ge=0)
    critical: bool
    trials: tuple[EvaluationTrialTransition, ...]


class EvaluationRunComparison(_EvaluationModel):
    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    baseline_run_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    baseline_target: ReleaseTarget
    candidate_run_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_target: ReleaseTarget
    cases: tuple[EvaluationCaseTransition, ...]
    improvement_count: int = Field(ge=0)
    regression_count: int = Field(ge=0)
    eligible: bool
    eligibility_failures: tuple[str, ...]


def compare_runs(
    manifest: _benchmark_manifests.EvaluationManifest,
    baseline: _executions.EvaluationRun,
    candidate: _executions.EvaluationRun,
) -> EvaluationRunComparison:
    if baseline.manifest_id != manifest.id or candidate.manifest_id != manifest.id:
        raise ValueError("Baseline and candidate runs must use the supplied manifest")
    if baseline.target is None or candidate.target is None:
        raise ValueError("Run comparison requires exact release targets")
    if baseline.target == candidate.target:
        raise ValueError("Baseline and candidate release targets must differ")
    baseline_results = _indexed_results(manifest, baseline)
    candidate_results = _indexed_results(manifest, candidate)
    baseline_metrics = _scoring.score_results(manifest, baseline.results)
    candidate_metrics = _scoring.score_results(manifest, candidate.results)
    if baseline.metrics != baseline_metrics or candidate.metrics != candidate_metrics:
        raise ValueError("Run metrics must match case-level evidence")
    cases: list[EvaluationCaseTransition] = []
    improvements = 0
    regressions = 0
    for case in manifest.cases:
        trials: list[EvaluationTrialTransition] = []
        for trial_index in range(case.trial_count):
            key = (case.position, trial_index)
            baseline_result = baseline_results[key]
            candidate_result = candidate_results[key]
            transition = _transition(baseline_result, candidate_result)
            improvements += transition == "improvement"
            regressions += transition == "regression"
            trials.append(
                EvaluationTrialTransition(
                    case_position=case.position,
                    trial_index=trial_index,
                    baseline=baseline_result,
                    candidate=candidate_result,
                    transition=transition,
                )
            )
        cases.append(
            EvaluationCaseTransition(
                case_position=case.position,
                critical=case.critical,
                trials=tuple(trials),
            )
        )
    failures = promotion_eligibility_failures(manifest.policy, baseline_metrics, candidate_metrics)
    comparison_id = hashlib.sha256(
        (f"evaluation_run_comparison_v1:{manifest.id}:" f"{baseline.id}:{candidate.id}").encode()
    ).hexdigest()
    return EvaluationRunComparison(
        id=comparison_id,
        manifest_id=manifest.id,
        baseline_run_id=baseline.id,
        baseline_target=baseline.target,
        candidate_run_id=candidate.id,
        candidate_target=candidate.target,
        cases=tuple(cases),
        improvement_count=improvements,
        regression_count=regressions,
        eligible=not failures,
        eligibility_failures=failures,
    )


def preview_run_comparison(
    connection: _Connection,
    baseline_run_id: _Digest,
    candidate_run_id: _Digest,
) -> EvaluationRunComparison:
    baseline = _executions.load_run(connection, baseline_run_id)
    candidate = _executions.load_run(connection, candidate_run_id)
    if baseline.manifest_id != candidate.manifest_id:
        raise ValueError("Baseline and candidate runs must use the same manifest")
    return compare_runs(
        _benchmark_manifests.load_manifest(connection, baseline.manifest_id), baseline, candidate
    )


def promotion_eligibility_failures(
    policy: _benchmark_manifests.ManifestPolicy,
    baseline: _scoring.EvaluationMetrics,
    candidate: _scoring.EvaluationMetrics,
) -> tuple[str, ...]:
    failures: list[str] = []
    if baseline.operational_failure_count:
        failures.append("Baseline has operational failures")
    if candidate.operational_failure_count:
        failures.append("Candidate has operational failures")
    if candidate.critical_false_positive_count:
        failures.append("Candidate qualified a critical expected-negative trial")
    if candidate.false_positive_rate > policy.max_false_positive_rate:
        failures.append("Candidate exceeds the false-positive threshold")
    if candidate.false_negative_rate > policy.max_false_negative_rate:
        failures.append("Candidate exceeds the false-negative threshold")
    if candidate.false_positive_rate > baseline.false_positive_rate:
        failures.append("Candidate regresses against baseline false positives")
    if candidate.false_negative_rate > baseline.false_negative_rate:
        failures.append("Candidate regresses against baseline false negatives")
    return tuple(failures)


def _indexed_results(
    manifest: _benchmark_manifests.EvaluationManifest, run: _executions.EvaluationRun
) -> dict[tuple[int, int], _scoring.EvaluationTrialResult]:
    indexed = {(result.case_position, result.trial_index): result for result in run.results}
    required = {
        (case.position, trial_index)
        for case in manifest.cases
        for trial_index in range(case.trial_count)
    }
    if len(indexed) != len(run.results) or set(indexed) != required:
        raise ValueError("Run results must cover every manifest trial exactly once")
    expected = {case.position: case.expected_outcome for case in manifest.cases}
    if any(result.expected_outcome != expected[result.case_position] for result in run.results):
        raise ValueError("Run results must match manifest expectations")
    return indexed


def _transition(
    baseline: _scoring.EvaluationTrialResult,
    candidate: _scoring.EvaluationTrialResult,
) -> Literal["unchanged", "improvement", "regression", "changed_failure"]:
    if baseline.failure_kind == candidate.failure_kind:
        return "unchanged"
    if baseline.failure_kind is not None and candidate.failure_kind is None:
        return "improvement"
    if baseline.failure_kind is None and candidate.failure_kind is not None:
        return "regression"
    return "changed_failure"
