from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal, assert_never
from uuid import uuid4

import psycopg

from job_finder.discovery.exchange_rates import fetch_exchange_rates, format_compensation_rates
from job_finder.evaluation.evaluate import evaluate_job
from job_finder.evaluation.jev import (
    JevCriterionObservation,
    evaluate_prompt as evaluate_jev_prompt,
    summarize_observations,
)
from job_finder.evaluation.manifests import CaseEvaluator, EvaluationManifestCase, run_manifest
from job_finder.evaluation.models import (
    CriterionResult,
    EvaluationResult,
    InputDigest,
    ModelCallAttempt,
    ModelCallContext,
    ReleaseTarget,
)
from job_finder.evaluation.openrouter import (
    ModelCallPersistence,
    evaluate_prompt as evaluate_openrouter_prompt,
    prompt_input_digest,
)
from job_finder.evaluation.prompt_releases import (
    PromptRelease,
    PromptVersion,
    bootstrap_prompt_release,
)
from job_finder.evaluation.relevance_releases import (
    GeminiExecutionPolicy,
    JevAtomicExecutionPolicy,
    JevFaithfulExecutionPolicy,
    RelevanceExecutionPolicy,
    build_gemini_policy,
    build_jev_atomic_policy,
    build_jev_faithful_policy,
    build_relevance_release,
    store_relevance_release,
)
from job_finder.jobs.models import JobListing

Provider = Literal["gemini", "jev-faithful", "jev-atomic"]


class Arguments(argparse.Namespace):
    manifest_id: str = ""
    provider: Provider = "gemini"
    idempotency_key: str = ""
    implementation_ref: str = ""


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parse_arguments(argv)
    postgres_dsn = _required_environment("JOB_FINDER_POSTGRES_DSN")
    observed_at = datetime.now(UTC)
    rates = format_compensation_rates(fetch_exchange_rates(observed_at=observed_at).rates)
    model_attempts: list[ModelCallAttempt] = []
    jev_observations: list[JevCriterionObservation] = []
    jev_latencies: list[int] = []

    with psycopg.connect(postgres_dsn, autocommit=True) as connection:
        release = bootstrap_prompt_release(connection)
        relevance_release = store_relevance_release(
            connection,
            build_relevance_release(
                build_gemini_policy(release)
                if arguments.provider == "gemini"
                else (
                    build_jev_atomic_policy()
                    if arguments.provider == "jev-atomic"
                    else build_jev_faithful_policy(release)
                )
            ),
            created_at=observed_at,
            created_by="evaluate_manifest",
        )
        target = ReleaseTarget(
            prompt_release_id=release.id,
            relevance_release_id=relevance_release.id,
        )
        evaluator = _case_evaluator(
            release=release,
            target=target,
            relevance_policy=relevance_release.policy,
            rates=rates,
            openrouter_api_key=os.environ.get("OPENROUTER_API_KEY"),
            typesafe_api_key=os.environ.get("TYPESAFE_API_KEY"),
            model_attempts=model_attempts,
            jev_observations=jev_observations,
            jev_latencies=jev_latencies,
        )
        run = run_manifest(
            connection,
            manifest_id=arguments.manifest_id,
            target=target,
            evaluator=evaluator,
            implementation_ref=arguments.implementation_ref,
            completed_at=datetime.now(UTC),
            idempotency_key=arguments.idempotency_key,
        )

    telemetry: dict[str, object]
    if arguments.provider == "gemini":
        telemetry = _openrouter_telemetry(model_attempts)
    else:
        metrics = summarize_observations(jev_observations, jev_latencies)
        telemetry = {
            **metrics.model_dump(mode="json"),
            "relevance_release_id": relevance_release.id,
        }
    print(
        json.dumps(
            {
                "provider": arguments.provider,
                "run": run.model_dump(mode="json"),
                "telemetry": telemetry,
            },
            indent=2,
            default=str,
        )
    )
    return 0


def _case_evaluator(
    *,
    release: PromptRelease,
    target: ReleaseTarget,
    relevance_policy: RelevanceExecutionPolicy,
    rates: str,
    openrouter_api_key: str | None,
    typesafe_api_key: str | None,
    model_attempts: list[ModelCallAttempt],
    jev_observations: list[JevCriterionObservation],
    jev_latencies: list[int],
) -> CaseEvaluator:
    match relevance_policy:
        case GeminiExecutionPolicy():
            if not openrouter_api_key:
                raise ValueError("OPENROUTER_API_KEY is required for the Gemini benchmark")
            api_key = openrouter_api_key
        case JevAtomicExecutionPolicy() | JevFaithfulExecutionPolicy():
            if not typesafe_api_key:
                raise ValueError("TYPESAFE_API_KEY is required for the Jev benchmark")
            api_key = typesafe_api_key
        case _:
            assert_never(relevance_policy)

    def evaluate_case(
        case: EvaluationManifestCase,
        case_target: ReleaseTarget,
        trial_index: int,
    ) -> EvaluationResult:
        if case_target != target:
            raise ValueError("Benchmark release target changed")
        job = JobListing.model_validate(
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

        def evaluate_criterion(
            prompt: PromptVersion,
            values: Mapping[str, str],
        ) -> CriterionResult:
            match relevance_policy:
                case GeminiExecutionPolicy():
                    context = ModelCallContext(
                        processing_attempt_id=uuid4(),
                        pipeline_run_id=uuid4(),
                        prompt_release_id=release.id,
                        operation_key=(
                            f"manifest:{case.position}:{trial_index}:{prompt.definition.name}"
                        ),
                        input_digest=InputDigest(prompt_input_digest(values)),
                    )
                    return evaluate_openrouter_prompt(
                        prompt,
                        values,
                        context,
                        ModelCallPersistence(
                            find_completed=lambda _request_id: None,
                            next_attempt_number=lambda _request_id: 0,
                            record=model_attempts.append,
                        ),
                        api_key=api_key,
                    )
                case JevAtomicExecutionPolicy() | JevFaithfulExecutionPolicy():
                    result = evaluate_jev_prompt(
                        prompt,
                        values,
                        api_key=api_key,
                        execution_policy=relevance_policy,
                        observe_request=jev_latencies.append,
                    )
                case _:
                    assert_never(relevance_policy)
            if isinstance(result, JevCriterionObservation):
                jev_observations.append(result)
                return result.result
            return result

        return evaluate_job(job, release, evaluate_criterion, rates=rates)

    return evaluate_case


def _openrouter_telemetry(attempts: Sequence[ModelCallAttempt]) -> dict[str, object]:
    latencies = sorted(attempt.latency_ms for attempt in attempts)
    return {
        "request_count": len(attempts),
        "input_tokens": sum(attempt.input_tokens or 0 for attempt in attempts),
        "output_tokens": sum(attempt.output_tokens or 0 for attempt in attempts),
        "cost_usd": str(
            sum((attempt.cost_usd or Decimal(0) for attempt in attempts), start=Decimal(0))
        ),
        "p50_latency_ms": _percentile(latencies, 0.50),
        "p95_latency_ms": _percentile(latencies, 0.95),
    }


def _percentile(values: Sequence[int], percentile: float) -> float | None:
    if not values:
        return None
    position = (len(values) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _parse_arguments(argv: Sequence[str] | None) -> Arguments:
    parser = argparse.ArgumentParser(description="Run one provider against a frozen manifest.")
    _ = parser.add_argument("--manifest-id", required=True)
    _ = parser.add_argument(
        "--provider", required=True, choices=("gemini", "jev-faithful", "jev-atomic")
    )
    _ = parser.add_argument("--idempotency-key", required=True)
    _ = parser.add_argument("--implementation-ref", required=True)
    return parser.parse_args(argv, namespace=Arguments())


if __name__ == "__main__":
    raise SystemExit(main())
