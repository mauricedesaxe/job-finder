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
import job_finder.benchmarks.scoring as _scoring
from job_finder.discovery.exchange_rates import ExchangeRateSnapshot
from job_finder.evaluation.models import (
    EvaluationResult,
    ProviderRequestObservation,
    PromptReleaseId,
    ReleaseTarget,
    RelevanceReleaseId,
)
from job_finder.evaluation.prompt_releases import load_prompt_release
from job_finder.evaluation.relevance_releases import (
    load_relevance_release,
    validate_release_target,
)

_Connection = psycopg.Connection[tuple[object, ...]]
_Digest = str
_RUNNING_CONNECTIONS: set[int] = set()
_RUNNING_CONNECTIONS_LOCK = Lock()


class _EvaluationModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class EvaluationRun(_EvaluationModel):
    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str
    manifest_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt_release_id: PromptReleaseId = Field(pattern=r"^[0-9a-f]{64}$")
    target: ReleaseTarget | None = None
    implementation_ref: str
    metrics: _scoring.EvaluationMetrics
    results: tuple[_scoring.EvaluationTrialResult, ...]
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
    connection: _Connection,
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
    connection: _Connection,
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
                _scoring.score_trial(
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
                metrics=_scoring.score_results(manifest, results),
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
    connection: _Connection,
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
    connection: _Connection,
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
    connection: _Connection,
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
    connection: _Connection, idempotency_key: str
) -> EvaluationExecutionState | None:
    row = connection.execute(
        "SELECT id FROM evaluation_run_executions WHERE idempotency_key = %s",
        (idempotency_key,),
    ).fetchone()
    return None if row is None else load_evaluation_execution(connection, str(row[0]))


def load_evaluation_execution(
    connection: _Connection, execution_id: str
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


def _insert_run(connection: _Connection, run: EvaluationRun) -> None:
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


def load_run(connection: _Connection, run_id: _Digest) -> EvaluationRun:
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
        metrics=_scoring.EvaluationMetrics(
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
            _scoring.EvaluationTrialResult.model_validate(
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


def _digest(value: object) -> _Digest:
    content = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(content.encode()).hexdigest()


def _require_autocommit(connection: _Connection) -> None:
    if not connection.autocommit:
        raise ValueError("Evaluation manifest operations require an autocommit connection")
