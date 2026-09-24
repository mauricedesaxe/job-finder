from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from threading import Lock
from typing import Annotated, ClassVar, Literal, Self

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_serializer, model_validator

import job_finder.benchmarks.manifests as _benchmark_manifests
from job_finder.discovery.exchange_rates import ExchangeRateSnapshot
from job_finder.evaluation.models import (
    EvaluationOutcome,
    EvaluationResult,
    ProviderRequestObservation,
    PromptReleaseId,
    ReleaseTarget,
    RelevanceReleaseId,
    evaluation_outcome,
)
from job_finder.evaluation.prompt_releases import load_prompt_release
from job_finder.evaluation.relevance_releases import (
    load_relevance_release,
    validate_release_target,
)

Connection = psycopg.Connection[tuple[object, ...]]
_Digest = str
_RUNNING_CONNECTIONS: set[int] = set()
_RUNNING_CONNECTIONS_LOCK = Lock()


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


class EvaluationRun(_EvaluationModel):
    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str
    manifest_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt_release_id: PromptReleaseId = Field(pattern=r"^[0-9a-f]{64}$")
    target: ReleaseTarget | None = None
    implementation_ref: str
    metrics: EvaluationMetrics
    results: tuple[EvaluationTrialResult, ...]
    completed_at: datetime

    @model_validator(mode="after")
    def target_matches_prompt_provenance(self) -> Self:
        if self.target is not None and self.target.prompt_release_id != self.prompt_release_id:
            raise ValueError("Evaluation run target must match prompt provenance")
        return self


class EvaluateManifestCommand(_EvaluationModel):
    idempotency_key: str = Field(min_length=1)
    manifest_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    target: ReleaseTarget
    implementation_ref: str = Field(min_length=1)


class LegacyEvaluateManifestCommand(_EvaluationModel):
    idempotency_key: str = Field(min_length=1)
    manifest_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt_release_id: PromptReleaseId = Field(pattern=r"^[0-9a-f]{64}$")
    target: ReleaseTarget | None
    implementation_ref: str = Field(min_length=1)


class EvaluationRunTelemetry(_EvaluationModel):
    request_count: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_usd: Decimal = Field(ge=0)
    usage_complete: bool = True
    p50_latency_ms: Decimal | None = Field(default=None, ge=0)
    p95_latency_ms: Decimal | None = Field(default=None, ge=0)

    @field_serializer("cost_usd", "p50_latency_ms", "p95_latency_ms", when_used="json")
    def serialize_decimals(self, value: Decimal | None) -> str | None:
        return None if value is None else format(value, "f")

    @model_validator(mode="after")
    def percentiles_match_request_count(self) -> Self:
        both_present = self.p50_latency_ms is not None and self.p95_latency_ms is not None
        both_absent = self.p50_latency_ms is None and self.p95_latency_ms is None
        if not (both_present or both_absent):
            raise ValueError("Latency percentiles must both be present or absent")
        if both_present != (self.request_count > 0):
            raise ValueError("Latency percentiles require at least one request")
        if (
            self.p50_latency_ms is not None
            and self.p95_latency_ms is not None
            and self.p50_latency_ms > self.p95_latency_ms
        ):
            raise ValueError("p50 latency cannot exceed p95 latency")
        return self


class EvaluationExecutionFailure(_EvaluationModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    error_type: str | None = None


class EvaluationExecution(_EvaluationModel):
    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    command: EvaluateManifestCommand | LegacyEvaluateManifestCommand
    exchange_rates: ExchangeRateSnapshot | None
    exchange_rate_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    created_at: datetime

    @model_validator(mode="after")
    def rate_digest_matches_snapshot(self) -> Self:
        if (self.exchange_rates is None) != (self.exchange_rate_digest is None):
            raise ValueError("Exchange-rate snapshot and digest must be present together")
        if (
            self.exchange_rates is not None
            and self.exchange_rate_digest != exchange_rate_snapshot_digest(self.exchange_rates)
        ):
            raise ValueError("Exchange-rate digest does not match its snapshot")
        return self


class RunningEvaluationExecution(EvaluationExecution):
    state: Literal["running"] = "running"


class CompletedEvaluationExecution(EvaluationExecution):
    state: Literal["completed"] = "completed"
    telemetry: EvaluationRunTelemetry | None = None
    run: EvaluationRun
    completed_at: datetime

    @model_validator(mode="after")
    def legacy_provenance_is_consistently_unknown(self) -> Self:
        if isinstance(self.command, LegacyEvaluateManifestCommand):
            if self.telemetry is not None or self.exchange_rates is not None:
                raise ValueError("Legacy execution cannot invent telemetry or rate provenance")
        elif self.telemetry is None or self.exchange_rates is None:
            raise ValueError("Current completed execution requires telemetry and rates")
        return self


class FailedEvaluationExecution(EvaluationExecution):
    state: Literal["failed"] = "failed"
    telemetry: EvaluationRunTelemetry | None = None
    failure: EvaluationExecutionFailure
    failed_at: datetime


EvaluationExecutionState = Annotated[
    RunningEvaluationExecution | CompletedEvaluationExecution | FailedEvaluationExecution,
    Field(discriminator="state"),
]
_EXECUTION_ADAPTER: TypeAdapter[EvaluationExecutionState] = TypeAdapter(EvaluationExecutionState)


class EvaluationTrialTransition(_EvaluationModel):
    case_position: int = Field(ge=0)
    trial_index: int = Field(ge=0)
    baseline: EvaluationTrialResult
    candidate: EvaluationTrialResult
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


class PromptPromotionDecision(_EvaluationModel):
    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    baseline_run_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    baseline_prompt_release_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    baseline_target: ReleaseTarget | None = None
    candidate_run_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_prompt_release_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_target: ReleaseTarget | None = None
    comparison_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    eligible: bool | None = None
    eligibility_failures: tuple[str, ...] = ()
    decision: Literal["approved", "rejected"]
    reason: str
    actor: str
    created_at: datetime

    @model_validator(mode="after")
    def targets_match_prompt_provenance(self) -> Self:
        for prompt_release_id, target in (
            (self.baseline_prompt_release_id, self.baseline_target),
            (self.candidate_prompt_release_id, self.candidate_target),
        ):
            if target is not None and target.prompt_release_id != prompt_release_id:
                raise ValueError("Promotion target must match prompt provenance")
        return self


CaseEvaluator = Callable[
    [_benchmark_manifests.EvaluationManifestCase, ReleaseTarget, int],
    EvaluationResult,
]
RequestObservationRecorder = Callable[[ProviderRequestObservation], None]
CaseEvaluatorFactory = Callable[
    [ExchangeRateSnapshot, RequestObservationRecorder],
    CaseEvaluator,
]


def run_manifest(
    connection: Connection,
    *,
    command: EvaluateManifestCommand,
    create_exchange_rates: Callable[[], ExchangeRateSnapshot],
    create_evaluator: CaseEvaluatorFactory,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> EvaluationExecutionState:
    _require_autocommit(connection)
    connection_identity = id(connection)
    with _RUNNING_CONNECTIONS_LOCK:
        if connection_identity in _RUNNING_CONNECTIONS:
            raise RuntimeError("Evaluation execution is already active on this connection")
        _RUNNING_CONNECTIONS.add(connection_identity)
    try:
        return _run_manifest_exclusive(
            connection,
            command=command,
            create_exchange_rates=create_exchange_rates,
            create_evaluator=create_evaluator,
            now=now,
        )
    finally:
        with _RUNNING_CONNECTIONS_LOCK:
            _RUNNING_CONNECTIONS.remove(connection_identity)


def _run_manifest_exclusive(
    connection: Connection,
    *,
    command: EvaluateManifestCommand,
    create_exchange_rates: Callable[[], ExchangeRateSnapshot],
    create_evaluator: CaseEvaluatorFactory,
    now: Callable[[], datetime],
) -> EvaluationExecutionState:
    lock_key = f"evaluation_execution:{command.idempotency_key}"
    _ = connection.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (lock_key,))
    try:
        existing = load_evaluation_execution_by_key(connection, command.idempotency_key)
        if existing is not None:
            _require_matching_execution_command(existing, command)
            if not isinstance(existing, RunningEvaluationExecution):
                return existing
            failed_at = now()
            failure = EvaluationExecutionFailure(
                code="interrupted_execution",
                message=(
                    "The prior execution lost its database session; provider calls will not be replayed."
                ),
            )
            with connection.transaction():
                _fail_execution(connection, existing.id, None, failure, failed_at)
            return load_evaluation_execution(connection, existing.id)

        prompt_release = load_prompt_release(connection, command.target.prompt_release_id)
        relevance_release = load_relevance_release(connection, command.target.relevance_release_id)
        validate_release_target(command.target, prompt_release, relevance_release)
        manifest = _benchmark_manifests.load_manifest(connection, command.manifest_id)
        exchange_rates = create_exchange_rates()
        execution_id = _execution_id(command.idempotency_key)
        rate_digest = exchange_rate_snapshot_digest(exchange_rates)
        created_at = now()
        with connection.transaction():
            _insert_running_execution(
                connection,
                execution_id,
                command,
                exchange_rates,
                rate_digest,
                created_at,
            )

        observations: list[ProviderRequestObservation] = []
        try:
            evaluator = create_evaluator(exchange_rates, observations.append)
            run_id = _digest({"kind": "evaluation_run", "idempotency_key": command.idempotency_key})
            results = tuple(
                _trial_result(
                    run_id,
                    case,
                    trial_index,
                    evaluator(case, command.target, trial_index),
                )
                for case in manifest.cases
                for trial_index in range(case.trial_count)
            )
            completed_at = now()
            run = EvaluationRun(
                id=run_id,
                idempotency_key=command.idempotency_key,
                manifest_id=command.manifest_id,
                prompt_release_id=command.target.prompt_release_id,
                target=command.target,
                implementation_ref=command.implementation_ref,
                metrics=score_results(manifest, results),
                results=results,
                completed_at=completed_at,
            )
            telemetry = aggregate_evaluation_telemetry(observations)
            with connection.transaction():
                _insert_run(connection, run)
                _complete_execution(connection, execution_id, run.id, telemetry, completed_at)
                _benchmark_manifests.enqueue_projection(
                    connection, "evaluation_run", run.id, run, completed_at
                )
        except Exception as error:
            failed_at = now()
            telemetry = aggregate_evaluation_telemetry(observations)
            failure = EvaluationExecutionFailure(
                code="unexpected_exception",
                message="Evaluation stopped after an unexpected exception.",
                error_type=type(error).__name__,
            )
            with connection.transaction():
                _fail_execution(connection, execution_id, telemetry, failure, failed_at)
        return load_evaluation_execution(connection, execution_id)
    finally:
        unlocked = connection.execute(
            "SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (lock_key,)
        ).fetchone()
        if unlocked != (True,):
            raise RuntimeError("Evaluation execution advisory lock was not held")


def exchange_rate_snapshot_digest(snapshot: ExchangeRateSnapshot) -> str:
    content = json.dumps(
        snapshot.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(content.encode()).hexdigest()


def aggregate_evaluation_telemetry(
    observations: Sequence[ProviderRequestObservation],
) -> EvaluationRunTelemetry:
    latencies = sorted(observation.latency_ms for observation in observations)
    return EvaluationRunTelemetry(
        request_count=len(observations),
        input_tokens=sum(observation.input_tokens for observation in observations),
        output_tokens=sum(observation.output_tokens for observation in observations),
        cost_usd=sum((observation.cost_usd for observation in observations), start=Decimal(0)),
        usage_complete=sum(not observation.usage_complete for observation in observations)
        <= sum(observation.resolves_prior_usage for observation in observations),
        p50_latency_ms=_percentile(latencies, Decimal("0.50")),
        p95_latency_ms=_percentile(latencies, Decimal("0.95")),
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


def compare_runs(
    manifest: _benchmark_manifests.EvaluationManifest,
    baseline: EvaluationRun,
    candidate: EvaluationRun,
) -> EvaluationRunComparison:
    if baseline.manifest_id != manifest.id or candidate.manifest_id != manifest.id:
        raise ValueError("Baseline and candidate runs must use the supplied manifest")
    if baseline.target is None or candidate.target is None:
        raise ValueError("Run comparison requires exact release targets")
    if baseline.target == candidate.target:
        raise ValueError("Baseline and candidate release targets must differ")
    baseline_results = _indexed_results(manifest, baseline)
    candidate_results = _indexed_results(manifest, candidate)
    baseline_metrics = score_results(manifest, baseline.results)
    candidate_metrics = score_results(manifest, candidate.results)
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
    failures = _promotion_failures(manifest.policy, baseline_metrics, candidate_metrics)
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
    connection: Connection,
    baseline_run_id: _Digest,
    candidate_run_id: _Digest,
) -> EvaluationRunComparison:
    baseline = load_run(connection, baseline_run_id)
    candidate = load_run(connection, candidate_run_id)
    if baseline.manifest_id != candidate.manifest_id:
        raise ValueError("Baseline and candidate runs must use the same manifest")
    return compare_runs(
        _benchmark_manifests.load_manifest(connection, baseline.manifest_id), baseline, candidate
    )


def record_prompt_promotion_decision(
    connection: Connection,
    *,
    baseline_run_id: _Digest,
    candidate_run_id: _Digest,
    expected_comparison_id: _Digest,
    decision: Literal["approved", "rejected"],
    reason: str,
    actor: str,
    created_at: datetime,
    idempotency_key: str,
) -> PromptPromotionDecision:
    _require_autocommit(connection)
    if not reason.strip():
        raise ValueError("Promotion decision reason must not be blank")
    with connection.transaction():
        _ = connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (f"prompt_promotion:{idempotency_key}",),
        )
        existing = load_promotion_decision(connection, idempotency_key)
        if existing is not None:
            if (
                existing.baseline_run_id != baseline_run_id
                or existing.candidate_run_id != candidate_run_id
                or existing.comparison_id != expected_comparison_id
                or existing.decision != decision
                or existing.reason != reason
                or existing.actor != actor
            ):
                raise ValueError("Idempotency key belongs to a different promotion decision")
            return existing
        _ = connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"prompt_promotion_pair:{baseline_run_id}:{candidate_run_id}",),
        )
        comparison = preview_run_comparison(connection, baseline_run_id, candidate_run_id)
        if comparison.id != expected_comparison_id:
            raise ValueError("Promotion decision evidence is stale")
        if decision == "approved" and not comparison.eligible:
            raise ValueError("Ineligible release target cannot be approved")
        duplicate = connection.execute(
            """
            SELECT 1 FROM prompt_promotion_decisions
            WHERE baseline_run_id = %s AND candidate_run_id = %s
            """,
            (baseline_run_id, candidate_run_id),
        ).fetchone()
        if duplicate is not None:
            raise ValueError("This run comparison already has a promotion decision")
        baseline = load_run(connection, baseline_run_id)
        candidate = load_run(connection, candidate_run_id)
        promotion_id = _digest({"kind": "prompt_promotion", "idempotency_key": idempotency_key})
        promotion = PromptPromotionDecision(
            id=promotion_id,
            manifest_id=comparison.manifest_id,
            baseline_run_id=baseline.id,
            baseline_prompt_release_id=comparison.baseline_target.prompt_release_id,
            baseline_target=comparison.baseline_target,
            candidate_run_id=candidate.id,
            candidate_prompt_release_id=comparison.candidate_target.prompt_release_id,
            candidate_target=comparison.candidate_target,
            comparison_id=comparison.id,
            eligible=comparison.eligible,
            eligibility_failures=comparison.eligibility_failures,
            decision=decision,
            reason=reason,
            actor=actor,
            created_at=created_at,
        )
        _ = connection.execute(
            """
            INSERT INTO prompt_promotion_decisions (
              id, idempotency_key, manifest_id, baseline_run_id,
              baseline_prompt_release_id, candidate_run_id,
              candidate_prompt_release_id, baseline_relevance_release_id,
              candidate_relevance_release_id, comparison_id, decision, reason,
              baseline_metrics, candidate_metrics, actor, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                promotion.id,
                idempotency_key,
                promotion.manifest_id,
                promotion.baseline_run_id,
                comparison.baseline_target.prompt_release_id,
                promotion.candidate_run_id,
                comparison.candidate_target.prompt_release_id,
                comparison.baseline_target.relevance_release_id,
                comparison.candidate_target.relevance_release_id,
                promotion.comparison_id,
                promotion.decision,
                promotion.reason,
                Jsonb(baseline.metrics.model_dump(mode="json")),
                Jsonb(candidate.metrics.model_dump(mode="json")),
                actor,
                created_at,
            ),
        )
        _benchmark_manifests.enqueue_projection(
            connection, "prompt_promotion", promotion.id, promotion, created_at
        )
    return promotion


def _indexed_results(
    manifest: _benchmark_manifests.EvaluationManifest, run: EvaluationRun
) -> dict[tuple[int, int], EvaluationTrialResult]:
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
    baseline: EvaluationTrialResult, candidate: EvaluationTrialResult
) -> Literal["unchanged", "improvement", "regression", "changed_failure"]:
    if baseline.failure_kind == candidate.failure_kind:
        return "unchanged"
    if baseline.failure_kind is not None and candidate.failure_kind is None:
        return "improvement"
    if baseline.failure_kind is None and candidate.failure_kind is not None:
        return "regression"
    return "changed_failure"


def _trial_result(
    run_id: _Digest,
    case: _benchmark_manifests.EvaluationManifestCase,
    trial_index: int,
    result: EvaluationResult,
) -> EvaluationTrialResult:
    actual = evaluation_outcome(result)
    failure = _failure_kind(case.expected_outcome, actual)
    result_id = _digest(
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


def _failure_kind(
    expected: EvaluationOutcome, actual: EvaluationOutcome | None
) -> Literal["false_positive", "false_negative", "operational"] | None:
    if actual is None:
        return "operational"
    if actual == expected:
        return None
    return "false_positive" if actual == "qualified" else "false_negative"


def _promotion_failures(
    policy: _benchmark_manifests.ManifestPolicy,
    baseline: EvaluationMetrics,
    candidate: EvaluationMetrics,
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


def _execution_id(idempotency_key: str) -> str:
    return hashlib.sha256(f"evaluation_execution:{idempotency_key}".encode()).hexdigest()


def _percentile(values: Sequence[int], percentile: Decimal) -> Decimal | None:
    if not values:
        return None
    position = Decimal(len(values) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    return Decimal(values[lower]) + Decimal(values[upper] - values[lower]) * (position - lower)


def _require_matching_execution_command(
    execution: EvaluationExecutionState,
    command: EvaluateManifestCommand,
) -> None:
    stored = execution.command
    if isinstance(stored, LegacyEvaluateManifestCommand):
        matches = (
            stored.idempotency_key == command.idempotency_key
            and stored.manifest_id == command.manifest_id
            and stored.target == command.target
            and stored.implementation_ref == command.implementation_ref
        )
    else:
        matches = stored == command
    if not matches:
        raise ValueError("Idempotency key belongs to a different evaluation execution")


def _insert_running_execution(
    connection: Connection,
    execution_id: str,
    command: EvaluateManifestCommand,
    exchange_rates: ExchangeRateSnapshot,
    rate_digest: str,
    created_at: datetime,
) -> None:
    _ = connection.execute(
        """
        INSERT INTO evaluation_run_executions (
          id, idempotency_key, manifest_id, prompt_release_id,
          relevance_release_id, implementation_ref, state,
          exchange_rate_snapshot, exchange_rate_digest, created_at
        ) VALUES (%s, %s, %s, %s, %s, %s, 'running', %s, %s, %s)
        """,
        (
            execution_id,
            command.idempotency_key,
            command.manifest_id,
            command.target.prompt_release_id,
            command.target.relevance_release_id,
            command.implementation_ref,
            Jsonb(exchange_rates.model_dump(mode="json")),
            rate_digest,
            created_at,
        ),
    )


def _telemetry_values(telemetry: EvaluationRunTelemetry | None) -> tuple[object, ...]:
    if telemetry is None:
        return (None, None, None, None, None, None, None)
    return (
        telemetry.request_count,
        telemetry.input_tokens,
        telemetry.output_tokens,
        telemetry.cost_usd,
        telemetry.usage_complete,
        telemetry.p50_latency_ms,
        telemetry.p95_latency_ms,
    )


def _complete_execution(
    connection: Connection,
    execution_id: str,
    run_id: str,
    telemetry: EvaluationRunTelemetry,
    completed_at: datetime,
) -> None:
    cursor = connection.execute(
        """
        UPDATE evaluation_run_executions
        SET state = 'completed', request_count = %s, input_tokens = %s,
            output_tokens = %s, cost_usd = %s, usage_complete = %s,
            p50_latency_ms = %s, p95_latency_ms = %s, run_id = %s, terminal_at = %s
        WHERE id = %s AND state = 'running'
        """,
        (*_telemetry_values(telemetry), run_id, completed_at, execution_id),
    )
    if cursor.rowcount != 1:
        raise RuntimeError("Evaluation execution was not running during completion")


def _fail_execution(
    connection: Connection,
    execution_id: str,
    telemetry: EvaluationRunTelemetry | None,
    failure: EvaluationExecutionFailure,
    failed_at: datetime,
) -> None:
    cursor = connection.execute(
        """
        UPDATE evaluation_run_executions
        SET state = 'failed', request_count = %s, input_tokens = %s,
            output_tokens = %s, cost_usd = %s, usage_complete = %s,
            p50_latency_ms = %s, p95_latency_ms = %s, failure = %s, terminal_at = %s
        WHERE id = %s AND state = 'running'
        """,
        (
            *_telemetry_values(telemetry),
            Jsonb(failure.model_dump(mode="json", exclude_none=True)),
            failed_at,
            execution_id,
        ),
    )
    if cursor.rowcount != 1:
        raise RuntimeError("Evaluation execution was not running during failure")


def load_evaluation_execution_by_key(
    connection: Connection, idempotency_key: str
) -> EvaluationExecutionState | None:
    row = connection.execute(
        "SELECT id FROM evaluation_run_executions WHERE idempotency_key = %s",
        (idempotency_key,),
    ).fetchone()
    return None if row is None else load_evaluation_execution(connection, str(row[0]))


def load_evaluation_execution(
    connection: Connection, execution_id: str
) -> EvaluationExecutionState:
    row = connection.execute(
        """
        SELECT idempotency_key, manifest_id, prompt_release_id,
               relevance_release_id, implementation_ref, state,
               exchange_rate_snapshot, exchange_rate_digest, request_count,
               input_tokens, output_tokens, cost_usd, usage_complete,
               p50_latency_ms, p95_latency_ms, run_id, failure, created_at, terminal_at
        FROM evaluation_run_executions WHERE id = %s
        """,
        (execution_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Evaluation execution does not exist")
    state = str(row[5])
    target = (
        None
        if row[3] is None
        else ReleaseTarget(
            prompt_release_id=PromptReleaseId(str(row[2])),
            relevance_release_id=RelevanceReleaseId(str(row[3])),
        )
    )
    command: dict[str, object]
    legacy = state == "completed" and row[6] is None
    if legacy:
        command = {
            "idempotency_key": row[0],
            "manifest_id": row[1],
            "prompt_release_id": row[2],
            "target": target,
            "implementation_ref": row[4],
        }
    else:
        if target is None:
            raise ValueError("Current evaluation execution has no release target")
        command = {
            "idempotency_key": row[0],
            "manifest_id": row[1],
            "target": target,
            "implementation_ref": row[4],
        }
    payload: dict[str, object] = {
        "id": execution_id,
        "command": command,
        "state": state,
        "exchange_rates": row[6],
        "exchange_rate_digest": row[7],
        "created_at": row[17],
    }
    if state in ("completed", "failed") and not legacy and row[8] is not None:
        payload["telemetry"] = {
            "request_count": row[8],
            "input_tokens": row[9],
            "output_tokens": row[10],
            "cost_usd": row[11],
            "usage_complete": row[12],
            "p50_latency_ms": row[13],
            "p95_latency_ms": row[14],
        }
    if state == "completed":
        payload["run"] = load_run(connection, str(row[15]))
        payload["completed_at"] = row[18]
    elif state == "failed":
        payload["failure"] = row[16]
        payload["failed_at"] = row[18]
    return _EXECUTION_ADAPTER.validate_python(payload)


def _insert_run(connection: Connection, run: EvaluationRun) -> None:
    if run.target is None:
        raise ValueError("New evaluation runs require a complete release target")
    metrics = run.metrics
    _ = connection.execute(
        """
        INSERT INTO evaluation_runs (
          id, idempotency_key, manifest_id, prompt_release_id, relevance_release_id,
          expected_result_count, result_count, false_positive_count,
          false_negative_count, operational_failure_count,
          critical_false_positive_count, false_positive_rate,
          false_negative_rate, implementation_ref, completed_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            run.id,
            run.idempotency_key,
            run.manifest_id,
            run.prompt_release_id,
            run.target.relevance_release_id,
            metrics.result_count,
            metrics.result_count,
            metrics.false_positive_count,
            metrics.false_negative_count,
            metrics.operational_failure_count,
            metrics.critical_false_positive_count,
            metrics.false_positive_rate,
            metrics.false_negative_rate,
            run.implementation_ref,
            run.completed_at,
        ),
    )
    for result in run.results:
        _ = connection.execute(
            """
            INSERT INTO evaluation_case_results (
              id, run_id, manifest_id, prompt_release_id, relevance_release_id, case_position,
              trial_index, expected_outcome, actual_outcome, failure_kind, reason
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                result.id,
                run.id,
                run.manifest_id,
                run.prompt_release_id,
                run.target.relevance_release_id,
                result.case_position,
                result.trial_index,
                result.expected_outcome,
                result.actual_outcome,
                result.failure_kind,
                result.reason,
            ),
        )


def load_run(connection: Connection, run_id: _Digest) -> EvaluationRun:
    row = connection.execute(
        """
        SELECT idempotency_key, manifest_id, prompt_release_id, relevance_release_id, result_count,
               false_positive_count, false_negative_count, operational_failure_count,
               critical_false_positive_count, false_positive_rate,
               false_negative_rate, implementation_ref, completed_at
        FROM evaluation_runs WHERE id = %s
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Evaluation run does not exist")
    result_rows = connection.execute(
        """
        SELECT id, case_position, trial_index, expected_outcome, actual_outcome,
               failure_kind, reason
        FROM evaluation_case_results WHERE run_id = %s
        ORDER BY case_position, trial_index
        """,
        (run_id,),
    ).fetchall()
    return EvaluationRun(
        id=run_id,
        idempotency_key=str(row[0]),
        manifest_id=str(row[1]),
        prompt_release_id=PromptReleaseId(str(row[2])),
        target=(
            None
            if row[3] is None
            else ReleaseTarget(
                prompt_release_id=PromptReleaseId(str(row[2])),
                relevance_release_id=RelevanceReleaseId(str(row[3])),
            )
        ),
        metrics=EvaluationMetrics(
            result_count=int(str(row[4])),
            false_positive_count=int(str(row[5])),
            false_negative_count=int(str(row[6])),
            operational_failure_count=int(str(row[7])),
            critical_false_positive_count=int(str(row[8])),
            false_positive_rate=Decimal(str(row[9])),
            false_negative_rate=Decimal(str(row[10])),
        ),
        implementation_ref=str(row[11]),
        results=tuple(
            EvaluationTrialResult.model_validate(
                {
                    "id": result[0],
                    "case_position": result[1],
                    "trial_index": result[2],
                    "expected_outcome": result[3],
                    "actual_outcome": result[4],
                    "failure_kind": result[5],
                    "reason": result[6],
                }
            )
            for result in result_rows
        ),
        completed_at=datetime.fromisoformat(str(row[12])),
    )


def load_promotion_decision(
    connection: Connection, idempotency_key: str
) -> PromptPromotionDecision | None:
    row = connection.execute(
        """
        SELECT id, manifest_id, baseline_run_id, baseline_prompt_release_id,
               baseline_relevance_release_id, candidate_run_id,
               candidate_prompt_release_id, candidate_relevance_release_id,
               comparison_id, decision, reason, baseline_metrics, candidate_metrics,
               actor, created_at
        FROM prompt_promotion_decisions WHERE idempotency_key = %s
        """,
        (idempotency_key,),
    ).fetchone()
    if row is None:
        return None
    return PromptPromotionDecision.model_validate(
        {
            "id": row[0],
            "manifest_id": row[1],
            "baseline_run_id": row[2],
            "baseline_prompt_release_id": row[3],
            "baseline_target": None
            if row[4] is None
            else {"prompt_release_id": row[3], "relevance_release_id": row[4]},
            "candidate_run_id": row[5],
            "candidate_prompt_release_id": row[6],
            "candidate_target": None
            if row[7] is None
            else {"prompt_release_id": row[6], "relevance_release_id": row[7]},
            "comparison_id": row[8],
            "decision": row[9],
            "reason": row[10],
            "eligible": not _promotion_failures(
                _benchmark_manifests.load_manifest(connection, str(row[1])).policy,
                EvaluationMetrics.model_validate(row[11]),
                EvaluationMetrics.model_validate(row[12]),
            ),
            "eligibility_failures": _promotion_failures(
                _benchmark_manifests.load_manifest(connection, str(row[1])).policy,
                EvaluationMetrics.model_validate(row[11]),
                EvaluationMetrics.model_validate(row[12]),
            ),
            "actor": row[13],
            "created_at": row[14],
        }
    )


def _rate(count: int, denominator: int) -> Decimal:
    if denominator == 0:
        return Decimal(0)
    return (Decimal(count) / Decimal(denominator)).quantize(Decimal("0.0000001"))


def _digest(value: object) -> _Digest:
    content = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(content.encode()).hexdigest()


def _require_autocommit(connection: Connection) -> None:
    if not connection.autocommit:
        raise ValueError("Evaluation manifest operations require an autocommit connection")
