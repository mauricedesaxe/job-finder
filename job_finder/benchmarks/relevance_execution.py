from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import assert_never
from uuid import NAMESPACE_URL, uuid5

import psycopg
from pydantic import TypeAdapter

from job_finder.ats.models import AtsAvailable, AtsEvidence
from job_finder.benchmarks.identity import canonical_digest
from job_finder.benchmarks.manifests import (
    EvaluationManifest,
    EvaluationManifestCase,
    ManifestPolicy,
    load_manifest,
)
from job_finder.benchmarks.provider_attempts import (
    provider_attempt_evidence,
    store_provider_attempts,
)
from job_finder.benchmarks.qualification_evidence import (
    ExperimentInputId,
    QualificationEvidence,
    QualificationEvidenceId,
    RelevanceExperimentInput,
    experiment_input_id,
    store_qualification_evidence,
)
from job_finder.benchmarks.scoring import EvaluationMetrics, score_results, score_trial
from job_finder.discovery.exchange_rates import format_compensation_rates
from job_finder.evaluation.evaluate import evaluate_job
from job_finder.evaluation.jev import (
    JevRetryPolicy,
    JevSender,
    evaluate_persisted_prompt as evaluate_jev_prompt,
)
from job_finder.evaluation.models import (
    CriterionResult,
    InputDigest,
    ModelCallAttempt,
    ModelCallContext,
)
from job_finder.evaluation.openrouter import (
    ChatCompletionSender,
    ModelCallPersistence,
    RetryPolicy,
    evaluate_prompt as evaluate_openrouter_prompt,
    prompt_input_digest,
)
from job_finder.evaluation.prompt_releases import PromptVersion
from job_finder.evaluation.qualification_components import QualificationTargetId
from job_finder.evaluation.qualification_prompt_compilations import (
    CompiledQualificationTarget,
    load_compiled_qualification_target,
)
from job_finder.evaluation.relevance_releases import (
    GeminiExecutionPolicy,
    JevAtomicExecutionPolicy,
    JevFaithfulExecutionPolicy,
    load_relevance_release,
)
from job_finder.jobs.models import JobListing

_FLOAT: TypeAdapter[float] = TypeAdapter(float)
_INT: TypeAdapter[int] = TypeAdapter(int)
_ATS: TypeAdapter[AtsEvidence] = TypeAdapter(AtsEvidence)


def execute_direct_relevance_experiment(
    connection: psycopg.Connection[tuple[object, ...]],
    target_id: QualificationTargetId,
    input_id: ExperimentInputId,
    artifact_path: Path,
    *,
    api_key: str,
    completed_at: datetime,
    created_by: str,
    openrouter_sender: ChatCompletionSender | None = None,
    jev_sender: JevSender | None = None,
) -> QualificationEvidenceId:
    compiled = load_compiled_qualification_target(connection, target_id, artifact_path)
    frozen = _load_direct_input(connection, input_id)
    manifest = load_manifest(connection, frozen.manifest_id)
    _require_direct_manifest_sources(connection, manifest)
    release = load_relevance_release(connection, compiled.target.relevance.relevance_release_id)
    _require_provider_settings(compiled, frozen, release.policy)
    provider = frozen.provider_settings.provider
    _require_matching_sender(frozen, openrouter_sender, jev_sender)
    synthetic = openrouter_sender is not None or jev_sender is not None
    attempts: list[ModelCallAttempt] = []
    run_id = canonical_digest(
        {
            "kind": "qualification_relevance",
            "target_id": target_id,
            "experiment_input_id": input_id,
            "completed_at": completed_at.isoformat(),
        }
    )
    results = tuple(
        score_trial(
            run_id,
            case,
            trial_index,
            _evaluate_case(
                compiled,
                release.policy,
                case,
                trial_index,
                frozen,
                attempts,
                api_key=api_key,
                completed_at=completed_at,
                openrouter_sender=openrouter_sender,
                jev_sender=jev_sender,
            ),
        )
        for case in manifest.cases
        for trial_index in range(case.trial_count)
    )
    metrics = score_results(manifest, results)
    passed = _metrics_pass(metrics, manifest.policy)
    evidence = QualificationEvidence(
        target_id=target_id,
        phase="relevance",
        component_release_id=compiled.target.content.relevance_release_id,
        experiment_input_id=input_id,
        executor_artifact_id=compiled.target.content.artifact_id,
        origin="synthetic" if synthetic else "canonical",
        outcome="passed" if passed else "failed",
        result={
            "metrics": metrics.model_dump(mode="json"),
            "trials": [result.model_dump(mode="json") for result in results],
        },
        attempts=tuple(provider_attempt_evidence(attempt) for attempt in attempts),
        completed_at=completed_at,
    )
    with connection.transaction():
        evidence_id = store_qualification_evidence(
            connection, evidence, created_at=completed_at, created_by=created_by
        )
        store_provider_attempts(
            connection,
            evidence,
            attempts,
            provider=provider,
            created_at=completed_at,
            created_by=created_by,
        )
    return evidence_id


def _load_direct_input(
    connection: psycopg.Connection[tuple[object, ...]], input_id: ExperimentInputId
) -> RelevanceExperimentInput:
    row = connection.execute(
        "SELECT content FROM relevance_experiment_inputs WHERE id = %s", (input_id,)
    ).fetchone()
    if row is None:
        raise ValueError("Frozen relevance experiment input not found")
    frozen = RelevanceExperimentInput.model_validate(row[0])
    if experiment_input_id(frozen) != input_id:
        raise ValueError("Frozen relevance experiment has invalid identity")
    if frozen.input_path != "direct":
        raise ValueError("Direct relevance execution requires direct inputs")
    return frozen


def _require_direct_manifest_sources(
    connection: psycopg.Connection[tuple[object, ...]], manifest: EvaluationManifest
) -> None:
    rows = connection.execute(
        """
        SELECT item.position, snapshot.ats_evidence
        FROM evaluation_manifest_cases item
        JOIN review_events event ON event.id = item.review_event_id
        JOIN review_items review ON review.id = event.review_item_id
        JOIN evaluation_decisions decision ON decision.id = review.evaluation_id
        JOIN job_snapshots snapshot ON snapshot.id = decision.snapshot_id
        WHERE item.manifest_id = %s
        ORDER BY item.position
        """,
        (manifest.id,),
    ).fetchall()
    if tuple(_INT.validate_python(row[0]) for row in rows) != tuple(
        case.position for case in manifest.cases
    ):
        raise ValueError("Relevance manifest source snapshots are incomplete")
    for _, evidence in rows:
        if evidence is not None and isinstance(_ATS.validate_python(evidence), AtsAvailable):
            raise ValueError("Direct relevance manifest contains ATS-prepared input")


def _require_matching_sender(
    frozen: RelevanceExperimentInput,
    openrouter_sender: ChatCompletionSender | None,
    jev_sender: JevSender | None,
) -> None:
    if frozen.provider_settings.provider == "openrouter" and jev_sender is not None:
        raise ValueError("Jev sender cannot execute an OpenRouter target")
    if frozen.provider_settings.provider == "typesafe" and openrouter_sender is not None:
        raise ValueError("OpenRouter sender cannot execute a TypeSafe target")


def _metrics_pass(metrics: EvaluationMetrics, policy: ManifestPolicy) -> bool:
    return (
        metrics.operational_failure_count == 0
        and metrics.critical_false_positive_count == 0
        and metrics.false_positive_rate <= policy.max_false_positive_rate
        and metrics.false_negative_rate <= policy.max_false_negative_rate
    )


def _require_provider_settings(
    compiled: CompiledQualificationTarget,
    frozen: RelevanceExperimentInput,
    policy: GeminiExecutionPolicy | JevAtomicExecutionPolicy | JevFaithfulExecutionPolicy,
) -> None:
    provider = frozen.provider_settings.provider
    if provider == "openrouter" and not isinstance(policy, GeminiExecutionPolicy):
        raise ValueError("Frozen provider differs from target relevance policy")
    if provider == "typesafe" and not isinstance(
        policy, JevAtomicExecutionPolicy | JevFaithfulExecutionPolicy
    ):
        raise ValueError("Frozen provider differs from target relevance policy")
    if frozen.provider_settings.seed is not None:
        raise ValueError("Relevance provider does not support seeded requests")
    if provider == "openrouter" and any(
        _FLOAT.validate_python(version.parameters["temperature"])
        != frozen.provider_settings.temperature
        for version in compiled.prompt_release.versions
        if version.definition.phase in ("filter", "profile")
    ):
        raise ValueError("Frozen temperature differs from stored prompt settings")
    if provider == "typesafe" and frozen.provider_settings.temperature != 0:
        raise ValueError("TypeSafe relevance requests require zero frozen temperature")


def _evaluate_case(
    compiled: CompiledQualificationTarget,
    policy: GeminiExecutionPolicy | JevAtomicExecutionPolicy | JevFaithfulExecutionPolicy,
    case: EvaluationManifestCase,
    trial_index: int,
    frozen: RelevanceExperimentInput,
    attempts: list[ModelCallAttempt],
    *,
    api_key: str,
    completed_at: datetime,
    openrouter_sender: ChatCompletionSender | None,
    jev_sender: JevSender | None,
):
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
    release = compiled.prompt_release

    def evaluate_criterion(prompt: PromptVersion, values: Mapping[str, str]) -> CriterionResult:
        context = ModelCallContext(
            processing_attempt_id=uuid5(
                NAMESPACE_URL,
                f"qualification:{compiled.target.id}:{frozen.manifest_id}:{case.position}:{trial_index}",
            ),
            pipeline_run_id=uuid5(NAMESPACE_URL, f"qualification:{compiled.target.id}"),
            prompt_release_id=release.id,
            operation_key=f"relevance:{case.position}:{trial_index}:{prompt.definition.name}",
            input_digest=InputDigest(prompt_input_digest(values)),
        )
        persistence = ModelCallPersistence(
            find_completed=lambda _request_id: None,
            next_attempt_number=lambda _request_id: 0,
            record=attempts.append,
        )
        match policy:
            case GeminiExecutionPolicy():
                return evaluate_openrouter_prompt(
                    prompt,
                    values,
                    context,
                    persistence,
                    api_key=api_key,
                    sender=openrouter_sender,
                    retry_policy=RetryPolicy(max_attempts=frozen.provider_settings.retry_limit + 1),
                    now=lambda: completed_at,
                )
            case JevAtomicExecutionPolicy() | JevFaithfulExecutionPolicy():
                return evaluate_jev_prompt(
                    prompt,
                    values,
                    context,
                    persistence,
                    api_key=api_key,
                    execution_policy=policy,
                    sender=jev_sender,
                    retry_policy=JevRetryPolicy(
                        max_attempts=frozen.provider_settings.retry_limit + 1
                    ),
                    now=lambda: completed_at,
                )
            case _:
                assert_never(policy)

    return evaluate_job(
        job,
        release,
        evaluate_criterion,
        rates=format_compensation_rates(frozen.exchange_rates.rates),
    )
