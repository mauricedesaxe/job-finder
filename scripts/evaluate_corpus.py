from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

from job_finder.config import CorpusEvaluationSettings
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
)

Connection = psycopg.Connection[tuple[object, ...]]


class CorpusArguments(argparse.Namespace):
    suite: CorpusSuite = "direct"


def main(argv: Sequence[str] | None = None) -> int:
    suite = _parse_suite(argv)
    settings = CorpusEvaluationSettings.from_environment()
    observed_at = datetime.now(UTC)
    rates = format_compensation_rates(fetch_exchange_rates(observed_at=observed_at).rates)
    cases: tuple[EvaluationCorpusCase, ...] = (
        load_ats_evaluation_corpus() if suite == "ats" else load_evaluation_corpus()
    )
    run_id = uuid4()
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as connection:
        _ = apply_migrations(connection)
        release = bootstrap_prompt_release(connection)
        _start_run(
            connection,
            run_id,
            release,
            settings.implementation_ref,
            suite,
            len(cases),
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


def _parse_suite(argv: Sequence[str] | None) -> CorpusSuite:
    parser = argparse.ArgumentParser(description="Run the Python evaluation corpus gate.")
    _ = parser.add_argument(
        "--suite",
        choices=("direct", "ats"),
        default="direct",
        help="fixture suite to evaluate (default: direct)",
    )
    arguments = parser.parse_args(argv, namespace=CorpusArguments())
    return arguments.suite


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
    case_count: int,
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
            Jsonb({"corpus": f"python-{suite}-markdown-v1", "case_count": case_count}),
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


if __name__ == "__main__":
    raise SystemExit(main())
