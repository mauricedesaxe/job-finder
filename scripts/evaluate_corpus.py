from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

from job_finder.config import CorpusEvaluationSettings, JevCorpusEvaluationSettings
from job_finder.database import apply_migrations
from job_finder.discovery.exchange_rates import fetch_exchange_rates, format_compensation_rates
from job_finder.evaluation.corpus import (
    MAX_FALSE_NEGATIVE_RATE,
    MAX_FALSE_POSITIVE_RATE,
    CorpusSuite,
    EvaluationCorpusCase,
    EvaluationCorpusReport,
    EvaluationCorpusResult,
    evaluate_corpus_case,
    load_ats_evaluation_corpus,
    load_evaluation_corpus,
    score_evaluation_corpus,
)
from job_finder.evaluation.evaluate import evaluate_job
from job_finder.evaluation.jev import (
    JevCriterionObservation,
    JevRunMetrics,
    evaluate_prompt as evaluate_jev_prompt,
    jev_policy_digest,
    summarize_observations,
)
from job_finder.evaluation.models import (
    CriterionResult,
    ModelCallContext,
    OperationalError,
)
from job_finder.evaluation.openrouter import (
    evaluate_prompt,
    postgres_model_call_persistence,
    prompt_input_digest,
)
from job_finder.evaluation.prompt_releases import (
    PromptRelease,
    PromptVersion,
    bootstrap_prompt_release,
    build_prompt_release,
)

Connection = psycopg.Connection[tuple[object, ...]]


class CorpusArguments(argparse.Namespace):
    suite: CorpusSuite = "direct"
    provider: Literal["openrouter", "jev"] = "openrouter"
    trials: int | None = None


@dataclass(frozen=True)
class JevCaseEvaluation:
    result: EvaluationCorpusResult
    observations: tuple[JevCriterionObservation, ...]
    request_latencies_ms: tuple[int, ...]


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_arguments(argv)
    settings: CorpusEvaluationSettings | JevCorpusEvaluationSettings
    if arguments.provider == "jev":
        settings = JevCorpusEvaluationSettings.from_environment()
    else:
        settings = CorpusEvaluationSettings.from_environment()
    observed_at = datetime.now(UTC)
    rates = format_compensation_rates(fetch_exchange_rates(observed_at=observed_at).rates)
    cases: tuple[EvaluationCorpusCase, ...] = (
        load_ats_evaluation_corpus() if arguments.suite == "ats" else load_evaluation_corpus()
    )
    if arguments.provider == "jev":
        assert isinstance(settings, JevCorpusEvaluationSettings)
        release = build_prompt_release()
        trial_count = arguments.trials or 3
        passed = True
        for trial in range(1, trial_count + 1):
            report, metrics = _run_jev_trial(
                cases,
                release,
                settings.api_key,
                rates,
                settings.worker_count,
            )
            print(f"Jev trial {trial}/{trial_count}")
            print(f"Jev policy: {jev_policy_digest(rates)}")
            _print_report(report)
            _print_jev_metrics(metrics)
            passed = passed and report.passed
        return 0 if passed else 1

    assert isinstance(settings, CorpusEvaluationSettings)
    run_id = uuid4()
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as connection:
        _ = apply_migrations(connection)
        release = bootstrap_prompt_release(connection)
        _start_run(
            connection,
            run_id,
            release,
            settings.implementation_ref,
            arguments.suite,
            "openrouter",
            len(cases),
            rates,
            observed_at,
        )
    with ThreadPoolExecutor(max_workers=settings.worker_count) as executor:
        futures = tuple(
            executor.submit(
                _evaluate_case,
                case,
                release,
                settings.postgres_dsn,
                settings.openrouter_api_key,
                run_id,
                rates,
            )
            for case in cases
        )
        results = tuple(future.result() for future in futures)
    report = score_evaluation_corpus(results)
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as connection:
        _complete_run(connection, run_id, report, datetime.now(UTC))
    _print_report(report)
    return 0 if report.passed else 1


def parse_arguments(argv: Sequence[str] | None) -> CorpusArguments:
    parser = argparse.ArgumentParser(description="Run the Python evaluation corpus gate.")
    _ = parser.add_argument(
        "--suite",
        choices=("direct", "ats"),
        default="direct",
        help="fixture suite to evaluate (default: direct)",
    )
    _ = parser.add_argument(
        "--provider",
        choices=("openrouter", "jev"),
        default="openrouter",
        help="evaluation provider (default: openrouter)",
    )
    _ = parser.add_argument(
        "--trials",
        type=_positive_int,
        help="independent Jev trials (Jev only; default: 3)",
    )
    arguments = parser.parse_args(argv, namespace=CorpusArguments())
    if arguments.provider != "jev" and arguments.trials is not None:
        parser.error("--trials requires --provider jev")
    return arguments


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _run_jev_trial(
    cases: tuple[EvaluationCorpusCase, ...],
    release: PromptRelease,
    api_key: str,
    rates: str,
    worker_count: int,
) -> tuple[EvaluationCorpusReport, JevRunMetrics]:
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = tuple(
            executor.submit(_evaluate_jev_case, case, release, api_key, rates) for case in cases
        )
        evaluations = tuple(future.result() for future in futures)
    report = score_evaluation_corpus(tuple(evaluation.result for evaluation in evaluations))
    observations = tuple(
        observation for evaluation in evaluations for observation in evaluation.observations
    )
    request_latencies_ms = tuple(
        latency for evaluation in evaluations for latency in evaluation.request_latencies_ms
    )
    return report, summarize_observations(observations, request_latencies_ms)


def _evaluate_jev_case(
    case: EvaluationCorpusCase,
    release: PromptRelease,
    api_key: str,
    rates: str,
) -> JevCaseEvaluation:
    observations: list[JevCriterionObservation] = []
    request_latencies_ms: list[int] = []

    def evaluate(prompt: PromptVersion, values: Mapping[str, str]) -> CriterionResult:
        result = evaluate_jev_prompt(
            prompt,
            values,
            api_key=api_key,
            observe_request=request_latencies_ms.append,
        )
        if isinstance(result, JevCriterionObservation):
            observations.append(result)
            return result.result
        return result

    try:
        result = evaluate_corpus_case(
            case,
            lambda job: evaluate_job(job, release, evaluate, rates=rates),
        )
    except Exception as error:
        result = EvaluationCorpusResult(
            name=case.name,
            expected_outcome=case.expected_outcome,
            actual_outcome=None,
            reason=f"{type(error).__name__}: {error}",
        )
    return JevCaseEvaluation(
        result=result,
        observations=tuple(observations),
        request_latencies_ms=tuple(request_latencies_ms),
    )


def _evaluate_case(
    case: EvaluationCorpusCase,
    release: PromptRelease,
    postgres_dsn: str,
    api_key: str,
    pipeline_run_id: UUID,
    rates: str,
) -> EvaluationCorpusResult:
    with psycopg.connect(postgres_dsn, autocommit=True) as connection:

        def evaluate(prompt: PromptVersion, values: Mapping[str, str]) -> CriterionResult:
            return _evaluate_criterion(
                connection,
                pipeline_run_id,
                case,
                release,
                prompt,
                values,
                api_key,
            )

        try:
            return evaluate_corpus_case(
                case,
                lambda job: evaluate_job(job, release, evaluate, rates=rates),
            )
        except Exception as error:
            return EvaluationCorpusResult(
                name=case.name,
                expected_outcome=case.expected_outcome,
                actual_outcome=None,
                reason=f"{type(error).__name__}: {error}",
            )


def _evaluate_criterion(
    connection: Connection,
    pipeline_run_id: UUID,
    case: EvaluationCorpusCase,
    release: PromptRelease,
    prompt: PromptVersion,
    values: Mapping[str, str],
    api_key: str,
) -> CriterionResult:
    attempt_id = uuid4()
    input_digest = prompt_input_digest(values)
    operation_key = f"corpus:{case.relative_path}:{prompt.definition.name}"
    started_at = datetime.now(UTC)
    _ = connection.execute(
        """
        INSERT INTO processing_attempts (
          id, pipeline_run_id, operation_key, attempt_number,
          input_digest, status, started_at
        ) VALUES (%s, %s, %s, 0, %s, 'running', %s)
        """,
        (attempt_id, pipeline_run_id, operation_key, input_digest, started_at),
    )
    context = ModelCallContext(
        processing_attempt_id=attempt_id,
        pipeline_run_id=pipeline_run_id,
        prompt_release_id=release.id,
        operation_key=operation_key,
        input_digest=input_digest,
    )
    try:
        result = evaluate_prompt(
            prompt,
            values,
            context,
            postgres_model_call_persistence(connection),
            api_key=api_key,
        )
    except Exception as error:
        _finish_attempt(connection, attempt_id, error, datetime.now(UTC))
        raise
    _finish_attempt(connection, attempt_id, result, datetime.now(UTC))
    return result


def _start_run(
    connection: Connection,
    run_id: UUID,
    release: PromptRelease,
    implementation_ref: str,
    suite: CorpusSuite,
    provider: Literal["openrouter", "jev"],
    case_count: int,
    rates: str,
    started_at: datetime,
) -> None:
    _ = connection.execute(
        """
        INSERT INTO pipeline_runs (
          id, idempotency_key, kind, implementation_ref, prompt_release_id,
          parameters, status, started_at
        ) VALUES (%s, %s, 'evaluation', %s, %s, %s, 'running', %s)
        """,
        (
            run_id,
            f"corpus:{run_id}",
            implementation_ref,
            release.id,
            Jsonb(
                {
                    "corpus": f"python-{suite}-markdown-v1",
                    "case_count": case_count,
                    "provider": provider,
                    **(
                        {"jev_policy_digest": jev_policy_digest(rates)} if provider == "jev" else {}
                    ),
                }
            ),
            started_at,
        ),
    )


def _complete_run(
    connection: Connection,
    run_id: UUID,
    report: EvaluationCorpusReport,
    completed_at: datetime,
) -> None:
    _ = connection.execute(
        """
        UPDATE pipeline_runs
        SET status = 'completed', completed_at = %s,
            parameters = parameters || %s
        WHERE id = %s AND status = 'running'
        """,
        (
            completed_at,
            Jsonb(
                {
                    "false_positive_rate": str(report.false_positive_rate),
                    "false_negative_rate": str(report.false_negative_rate),
                    "operational_failure_count": report.operational_failure_count,
                }
            ),
            run_id,
        ),
    )


def _finish_attempt(
    connection: Connection,
    attempt_id: UUID,
    result: CriterionResult | Exception,
    completed_at: datetime,
) -> None:
    if isinstance(result, Exception):
        error = {
            "code": type(result).__name__,
            "reason": str(result),
            "retryability": "retryable",
        }
    elif isinstance(result, OperationalError):
        error = {
            "code": result.error_code,
            "reason": result.reason,
            "retryability": result.retryability,
        }
    else:
        error = None
    _ = connection.execute(
        """
        UPDATE processing_attempts
        SET status = %s, completed_at = %s, error = %s
        WHERE id = %s AND status = 'running'
        """,
        (
            "failed" if error is not None else "completed",
            completed_at,
            Jsonb(error) if error is not None else None,
            attempt_id,
        ),
    )


def _print_report(report: EvaluationCorpusReport) -> None:
    for result in report.results:
        if result.actual_outcome != result.expected_outcome:
            print(
                "MISCLASSIFIED {}: expected {}, got {}: {}".format(
                    result.name,
                    result.expected_outcome,
                    result.actual_outcome or "operational failure",
                    result.reason,
                )
            )
    print(f"Overall: {report.correct_count}/{report.result_count}")
    print(
        "FP rate: {} ({:.1%}), maximum {:.0%}".format(
            report.false_positive_count,
            report.false_positive_rate,
            MAX_FALSE_POSITIVE_RATE,
        )
    )
    print(
        "FN rate: {} ({:.1%}), maximum {:.0%}".format(
            report.false_negative_count,
            report.false_negative_rate,
            MAX_FALSE_NEGATIVE_RATE,
        )
    )
    print(f"Operational failures: {report.operational_failure_count}")


def _print_jev_metrics(metrics: JevRunMetrics) -> None:
    print(f"Jev requests: {metrics.request_count}")
    print(f"Jev tokens: {metrics.input_tokens} input, {metrics.output_tokens} output")
    print(f"Jev estimated cost: ${metrics.estimated_cost_usd:.6f}")
    if metrics.p50_latency_ms is None or metrics.p95_latency_ms is None:
        print("Jev request latency: unavailable")
    else:
        print(
            "Jev request latency: p50 {:.1f} ms, p95 {:.1f} ms".format(
                metrics.p50_latency_ms,
                metrics.p95_latency_ms,
            )
        )


if __name__ == "__main__":
    raise SystemExit(main())
