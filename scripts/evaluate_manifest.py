from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Literal, assert_never
from uuid import uuid4

import psycopg

from job_finder.discovery.exchange_rates import fetch_exchange_rates, format_compensation_rates
from job_finder.evaluation.evaluate import evaluate_job
from job_finder.evaluation.jev import (
    JevCriterionObservation,
    evaluate_prompt as evaluate_jev_prompt,
)
from job_finder.evaluation.manifests import (
    CaseEvaluator,
    CompletedEvaluationExecution,
    EvaluateManifestCommand,
    EvaluationManifestCase,
    FailedEvaluationExecution,
    run_manifest,
)
from job_finder.evaluation.models import (
    CriterionResult,
    EvaluationResult,
    InputDigest,
    ModelCallContext,
    ProviderRequestObservation,
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
        execution = run_manifest(
            connection,
            command=EvaluateManifestCommand(
                idempotency_key=arguments.idempotency_key,
                manifest_id=arguments.manifest_id,
                target=target,
                implementation_ref=arguments.implementation_ref,
            ),
            create_exchange_rates=lambda: fetch_exchange_rates(observed_at=datetime.now(UTC)),
            create_evaluator=lambda rates, record: _case_evaluator(
                release=release,
                target=target,
                relevance_policy=relevance_release.policy,
                rates=format_compensation_rates(rates.rates),
                openrouter_api_key=os.environ.get("OPENROUTER_API_KEY"),
                typesafe_api_key=os.environ.get("TYPESAFE_API_KEY"),
                record_request=record,
            ),
        )

    payload: dict[str, object] = {
        "provider": arguments.provider,
        "execution": execution.model_dump(mode="json"),
    }
    if isinstance(execution, CompletedEvaluationExecution):
        payload["run"] = execution.run.model_dump(mode="json")
        if execution.telemetry is not None:
            payload["telemetry"] = execution.telemetry.model_dump(mode="json")
    elif isinstance(execution, FailedEvaluationExecution):
        payload["failure"] = execution.failure.model_dump(mode="json")
        if execution.telemetry is not None:
            payload["telemetry"] = execution.telemetry.model_dump(mode="json")
    print(json.dumps(payload, indent=2, default=str))
    return 1 if isinstance(execution, FailedEvaluationExecution) else 0


def _case_evaluator(
    *,
    release: PromptRelease,
    target: ReleaseTarget,
    relevance_policy: RelevanceExecutionPolicy,
    rates: str,
    openrouter_api_key: str | None,
    typesafe_api_key: str | None,
    record_request: Callable[[ProviderRequestObservation], None],
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
                            record=lambda _attempt: None,
                        ),
                        api_key=api_key,
                        observe_request=record_request,
                    )
                case JevAtomicExecutionPolicy() | JevFaithfulExecutionPolicy():
                    result = evaluate_jev_prompt(
                        prompt,
                        values,
                        api_key=api_key,
                        execution_policy=relevance_policy,
                        observe_attempt=record_request,
                    )
                case _:
                    assert_never(relevance_policy)
            if isinstance(result, JevCriterionObservation):
                return result.result
            return result

        return evaluate_job(job, release, evaluate_criterion, rates=rates)

    return evaluate_case


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
