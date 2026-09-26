from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import ClassVar
from uuid import NAMESPACE_URL, uuid5

import psycopg
from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter

from job_finder.benchmarks.provider_attempts import (
    provider_attempt_evidence,
    store_provider_attempts,
)
from job_finder.benchmarks.qualification_evidence import (
    FixtureSetId,
    PhaseFixtureSet,
    ProviderExperimentSettings,
    QualificationEvidence,
    QualificationEvidenceId,
    fixture_set_id,
    store_qualification_evidence,
)
from job_finder.evaluation.models import (
    InputDigest,
    ModelCallAttempt,
    ModelCallContext,
    PromptAccepted,
)
from job_finder.evaluation.openrouter import (
    ChatCompletionSender,
    GenerationSender,
    ModelCallPersistence,
    RetryPolicy,
    prompt_input_digest,
)
from job_finder.evaluation.qualification_components import QualificationTargetId
from job_finder.evaluation.qualification_prompt_compilations import (
    load_compiled_qualification_target,
)
from job_finder.jobs.enrichment import EnrichedJob, enrich_job, enrichment_values
from job_finder.jobs.listings import JobListing

_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
_FLOAT: TypeAdapter[float] = TypeAdapter(float)


class EnrichmentFixtureInput(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    job: JobListing
    provider_settings: ProviderExperimentSettings


def execute_enrichment_fixture_set(
    connection: psycopg.Connection[tuple[object, ...]],
    target_id: QualificationTargetId,
    fixture_id: FixtureSetId,
    artifact_path: Path,
    *,
    api_key: str,
    completed_at: datetime,
    created_by: str,
    sender: ChatCompletionSender | None = None,
    generation_sender: GenerationSender | None = None,
) -> QualificationEvidenceId:
    compiled = load_compiled_qualification_target(connection, target_id, artifact_path)
    row = connection.execute(
        "SELECT content FROM qualification_fixture_sets WHERE id = %s AND phase = 'enrichment'",
        (fixture_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Enrichment fixture set not found")
    fixtures = PhaseFixtureSet.model_validate(row[0])
    if fixtures.phase != "enrichment" or fixture_set_id(fixtures) != fixture_id:
        raise ValueError("Enrichment fixture set has invalid identity")
    prompt = compiled.prompt_release.version("job-finder-enrichment")
    attempts: list[ModelCallAttempt] = []
    results: list[dict[str, JsonValue]] = []
    passed_count = 0
    for position, case in enumerate(fixtures.cases):
        content = EnrichmentFixtureInput.model_validate(case.input)
        _require_case_settings(content, prompt.parameters, case.input_path)
        expected = EnrichedJob.model_validate(case.expected)
        values = enrichment_values(content.job)
        context = ModelCallContext(
            processing_attempt_id=uuid5(
                NAMESPACE_URL, f"enrichment:{target_id}:{fixture_id}:{position}"
            ),
            pipeline_run_id=uuid5(NAMESPACE_URL, f"qualification:{target_id}"),
            prompt_release_id=compiled.prompt_release.id,
            operation_key=f"enrichment:{position}",
            input_digest=InputDigest(prompt_input_digest(values)),
        )
        result = enrich_job(
            content.job,
            compiled.prompt_release,
            context,
            ModelCallPersistence(
                find_completed=lambda _request_id: None,
                next_attempt_number=lambda _request_id: 0,
                record=attempts.append,
            ),
            api_key=api_key,
            sender=sender,
            generation_sender=generation_sender,
            retry_policy=RetryPolicy(max_attempts=content.provider_settings.retry_limit + 1),
        )
        passed = isinstance(result, PromptAccepted) and result.output == expected
        passed_count += passed
        observed = (
            result.output.model_dump(mode="json")
            if isinstance(result, PromptAccepted)
            else result.model_dump(mode="json")
        )
        results.append(
            {
                "input_path": case.input_path,
                "passed": passed,
                "observed": _JSON.validate_python(observed),
                "expected": expected.model_dump(mode="json"),
            }
        )
    evidence = QualificationEvidence(
        target_id=target_id,
        phase="enrichment",
        component_release_id=compiled.target.content.enrichment_release_id,
        fixture_set_id=fixture_id,
        executor_artifact_id=compiled.target.content.artifact_id,
        origin="synthetic" if sender is not None or generation_sender is not None else "canonical",
        outcome="passed" if passed_count == len(results) else "failed",
        result={
            "case_count": len(results),
            "passed_count": passed_count,
            "cases": _JSON.validate_python(results),
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
            provider="openrouter",
            created_at=completed_at,
            created_by=created_by,
        )
    return evidence_id


def _require_case_settings(
    content: EnrichmentFixtureInput,
    prompt_parameters: dict[str, JsonValue],
    input_path: str,
) -> None:
    settings = content.provider_settings
    if settings.provider != "openrouter" or settings.seed is not None:
        raise ValueError("Enrichment fixtures require unseeded OpenRouter settings")
    if _FLOAT.validate_python(prompt_parameters["temperature"]) != settings.temperature:
        raise ValueError("Frozen enrichment temperature differs from stored prompt")
    prepared_ats = content.job.description.startswith("## ATS Structured Data (")
    if (input_path == "ats") != prepared_ats:
        raise ValueError("Enrichment fixture input path differs from job description")
