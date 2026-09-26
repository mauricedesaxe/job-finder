from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import assert_never
from uuid import uuid4

import psycopg

from job_finder.benchmarks.manifests import EvaluationManifestCase
from job_finder.benchmarks.executions import (
    CaseEvaluator,
    EvaluateManifestCommand,
    EvaluationExecutionState,
    run_manifest,
)
from job_finder.discovery.exchange_rates import (
    ExchangeRateSnapshot,
    fetch_exchange_rates,
    format_compensation_rates,
)
from job_finder.evaluation.evaluate import evaluate_job
from job_finder.evaluation.jev import (
    JevCriterionObservation,
    evaluate_prompt as evaluate_jev_prompt,
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
from job_finder.evaluation.prompt_releases import PromptRelease, PromptVersion
from job_finder.evaluation.release_targets import load_release_target
from job_finder.evaluation.relevance_releases import (
    GeminiExecutionPolicy,
    JevAtomicExecutionPolicy,
    JevFaithfulExecutionPolicy,
    RelevanceExecutionPolicy,
)
from job_finder.jobs.listings import JobListing


def run_stored_manifest(
    connection: psycopg.Connection[tuple[object, ...]],
    command: EvaluateManifestCommand,
) -> EvaluationExecutionState:
    def create_evaluator(
        rates: ExchangeRateSnapshot,
        record: Callable[[ProviderRequestObservation], None],
    ) -> CaseEvaluator:
        release, relevance_release = load_release_target(connection, command.target)
        return _case_evaluator(
            release=release,
            target=command.target,
            relevance_policy=relevance_release.policy,
            rates=format_compensation_rates(rates.rates),
            openrouter_api_key=os.environ.get("OPENROUTER_API_KEY"),
            typesafe_api_key=os.environ.get("TYPESAFE_API_KEY"),
            record_request=record,
        )

    return run_manifest(
        connection,
        command=command,
        create_exchange_rates=lambda: fetch_exchange_rates(observed_at=datetime.now(UTC)),
        create_evaluator=create_evaluator,
    )


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
