from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import ClassVar, Never
from uuid import UUID, uuid4

import psycopg
from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter

from job_finder.ats.models import AtsEvidence, AtsNotApplicable
from job_finder.benchmarks.provider_attempts import (
    Provider,
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
from job_finder.discovery.exchange_rates import ExchangeRateSnapshot
from job_finder.discovery.jina import JinaUnavailable, ScrapeResult, SearchSucceeded
from job_finder.evaluation.jev import JevRetryPolicy, JevSender
from job_finder.evaluation.models import ModelCallAttempt, ReleaseTarget
from job_finder.evaluation.openrouter import (
    ChatCompletionSender,
    GenerationSender,
    RetryPolicy,
)
from job_finder.evaluation.qualification_components import QualificationTargetId
from job_finder.evaluation.qualification_prompt_compilations import (
    CompiledQualificationTarget,
    load_compiled_qualification_target,
)
from job_finder.evaluation.relevance_releases import (
    GeminiExecutionPolicy,
    load_relevance_release,
)
from job_finder.pipeline.discoveries import register_discoveries
from job_finder.pipeline.orchestration import PipelineBoundaries, process_claimed_jobs
from job_finder.pipeline.runs import OrchestrationRun, prepare_orchestration_run
from job_finder.search_configuration import SearchConfigurationRevisionId

_ATTEMPT = TypeAdapter(ModelCallAttempt)
_ATS: TypeAdapter[AtsEvidence] = TypeAdapter(AtsEvidence)
_SCRAPE: TypeAdapter[ScrapeResult] = TypeAdapter(ScrapeResult)
_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
_PROVIDER: TypeAdapter[Provider] = TypeAdapter(Provider)
_FLOAT: TypeAdapter[float] = TypeAdapter(float)


class CompositionFixtureInput(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    raw_url: str
    keyword: str
    domain: str
    scrape: dict[str, JsonValue]
    ats_evidence: dict[str, JsonValue]
    configuration_revision_id: SearchConfigurationRevisionId
    exchange_rates: ExchangeRateSnapshot
    openrouter_settings: ProviderExperimentSettings
    relevance_settings: ProviderExperimentSettings
    observed_at: datetime


class CompositionExpected(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    decision_outcome: str | None
    decision_stage: str | None
    work_state: str
    review_enqueued: bool


@dataclass(frozen=True)
class _CompositionRun:
    results: tuple[dict[str, JsonValue], ...]
    observations: tuple[CompositionExpected, ...]
    calls: tuple[tuple[Provider, ModelCallAttempt], ...]


def execute_composition_fixture_set(
    connection: psycopg.Connection[tuple[object, ...]],
    target_id: QualificationTargetId,
    fixture_id: FixtureSetId,
    artifact_path: Path,
    *,
    openrouter_api_key: str,
    typesafe_api_key: str | None,
    completed_at: datetime,
    created_by: str,
    model_sender: ChatCompletionSender | None = None,
    generation_sender: GenerationSender | None = None,
    jev_sender: JevSender | None = None,
) -> QualificationEvidenceId:
    if not connection.autocommit:
        raise ValueError("Composition execution requires an autocommit connection")
    compiled = load_compiled_qualification_target(connection, target_id, artifact_path)
    fixtures, inputs = _load_composition_inputs(connection, compiled, fixture_id)
    execution = _run_composition_cases(
        connection,
        compiled,
        fixtures,
        inputs,
        openrouter_api_key=openrouter_api_key,
        typesafe_api_key=typesafe_api_key,
        model_sender=model_sender,
        generation_sender=generation_sender,
        jev_sender=jev_sender,
    )
    calls = execution.calls
    attempts = tuple(attempt for _, attempt in calls)
    coverage = _composition_coverage(fixtures, inputs, execution)
    evidence = _build_composition_evidence(
        compiled,
        target_id,
        fixture_id,
        execution,
        coverage,
        completed_at,
        synthetic=(
            model_sender is not None or generation_sender is not None or jev_sender is not None
        ),
    )
    with connection.transaction():
        evidence_id = store_qualification_evidence(
            connection, evidence, created_at=completed_at, created_by=created_by
        )
        if attempts:
            store_provider_attempts(
                connection,
                evidence,
                attempts,
                providers=tuple(provider for provider, _ in calls),
                created_at=completed_at,
                created_by=created_by,
            )
    return evidence_id


def _build_composition_evidence(
    compiled: CompiledQualificationTarget,
    target_id: QualificationTargetId,
    fixture_id: FixtureSetId,
    execution: _CompositionRun,
    coverage: dict[str, bool],
    completed_at: datetime,
    *,
    synthetic: bool,
) -> QualificationEvidence:
    results = execution.results
    attempts = tuple(attempt for _, attempt in execution.calls)
    return QualificationEvidence(
        target_id=target_id,
        phase="composition",
        fixture_set_id=fixture_id,
        executor_artifact_id=compiled.target.content.artifact_id,
        origin="synthetic" if synthetic else "canonical",
        outcome=(
            "passed"
            if all(result["passed"] for result in results) and all(coverage.values())
            else "failed"
        ),
        result={
            "case_count": len(results),
            "passed_count": sum(bool(result["passed"]) for result in results),
            "cases": _JSON.validate_python(list(results)),
            "coverage": _JSON.validate_python(coverage),
        },
        attempts=tuple(provider_attempt_evidence(attempt) for attempt in attempts),
        completed_at=completed_at,
    )


def _load_composition_inputs(
    connection: psycopg.Connection[tuple[object, ...]],
    compiled: CompiledQualificationTarget,
    fixture_id: FixtureSetId,
) -> tuple[PhaseFixtureSet, tuple[CompositionFixtureInput, ...]]:
    row = connection.execute(
        "SELECT content FROM qualification_fixture_sets WHERE id = %s AND phase = 'composition'",
        (fixture_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Composition fixture set not found")
    fixtures = PhaseFixtureSet.model_validate(row[0])
    if fixtures.phase != "composition" or fixture_set_id(fixtures) != fixture_id:
        raise ValueError("Composition fixture set has invalid identity")
    inputs = tuple(CompositionFixtureInput.model_validate(case.input) for case in fixtures.cases)
    _validate_shared_inputs(inputs)
    relevance = load_relevance_release(connection, compiled.target.relevance.relevance_release_id)
    relevance_provider = (
        "openrouter" if isinstance(relevance.policy, GeminiExecutionPolicy) else "typesafe"
    )
    for case, content in zip(fixtures.cases, inputs, strict=True):
        _validate_case_input(case.input_path, content, relevance_provider)
        _validate_target_temperatures(compiled, content, relevance_provider)
    return fixtures, inputs


def _validate_shared_inputs(inputs: tuple[CompositionFixtureInput, ...]) -> None:
    if len({content.raw_url for content in inputs}) != len(inputs):
        raise ValueError("Composition fixture URLs must be distinct")
    if (
        len({content.configuration_revision_id for content in inputs}) != 1
        or len({content.exchange_rates.model_dump_json() for content in inputs}) != 1
    ):
        raise ValueError("Composition cases must share frozen run inputs")


def _validate_case_input(
    input_path: str, content: CompositionFixtureInput, relevance_provider: str
) -> None:
    if content.openrouter_settings.provider != "openrouter" or (
        content.relevance_settings.provider != relevance_provider
    ):
        raise ValueError("Composition provider settings have invalid providers")
    if relevance_provider == "openrouter" and (
        content.relevance_settings.retry_limit != content.openrouter_settings.retry_limit
    ):
        raise ValueError("OpenRouter relevance and phase retries must agree")
    ats = _ATS.validate_python(content.ats_evidence)
    if (input_path == "ats") != (not isinstance(ats, AtsNotApplicable)):
        raise ValueError("Composition input path differs from ATS evidence")
    _ = _SCRAPE.validate_python(content.scrape)


def _validate_target_temperatures(
    compiled: CompiledQualificationTarget,
    content: CompositionFixtureInput,
    relevance_provider: str,
) -> None:
    for name in ("job-finder-enrichment", "job-finder-title-deduplication"):
        prompt = compiled.prompt_release.version(name)
        if _FLOAT.validate_python(prompt.parameters["temperature"]) != (
            content.openrouter_settings.temperature
        ):
            raise ValueError("Composition OpenRouter temperature differs from target prompt")
    if relevance_provider == "openrouter":
        for prompt in compiled.prompt_release.versions[:-2]:
            if _FLOAT.validate_python(prompt.parameters["temperature"]) != (
                content.relevance_settings.temperature
            ):
                raise ValueError("Composition relevance temperature differs from target prompt")


def _run_composition_cases(
    connection: psycopg.Connection[tuple[object, ...]],
    compiled: CompiledQualificationTarget,
    fixtures: PhaseFixtureSet,
    inputs: tuple[CompositionFixtureInput, ...],
    *,
    openrouter_api_key: str,
    typesafe_api_key: str | None,
    model_sender: ChatCompletionSender | None,
    generation_sender: GenerationSender | None,
    jev_sender: JevSender | None,
) -> _CompositionRun:
    results: list[dict[str, JsonValue]] = []
    observations: list[CompositionExpected] = []
    calls: tuple[tuple[Provider, ModelCallAttempt], ...] = ()
    with connection.transaction(force_rollback=True):
        run = prepare_orchestration_run(
            connection,
            idempotency_key=f"qualification-composition:{uuid4()}",
            implementation_ref=compiled.target.content.artifact_id,
            configuration_revision_id=inputs[0].configuration_revision_id,
            target=ReleaseTarget(
                prompt_release_id=compiled.prompt_release.id,
                relevance_release_id=compiled.target.relevance.relevance_release_id,
            ),
            started_at=inputs[0].observed_at,
            fetch_rates=lambda: inputs[0].exchange_rates,
        )
        for case, content in zip(fixtures.cases, inputs, strict=True):
            observation = _run_case(
                connection,
                run,
                case.input_path,
                content,
                openrouter_api_key=openrouter_api_key,
                typesafe_api_key=typesafe_api_key,
                model_sender=model_sender,
                generation_sender=generation_sender,
                jev_sender=jev_sender,
            )
            expected = CompositionExpected.model_validate(case.expected)
            observations.append(observation)
            results.append(
                {
                    "passed": observation == expected,
                    "observed": _JSON.validate_python(observation.model_dump(mode="json")),
                    "expected": _JSON.validate_python(expected.model_dump(mode="json")),
                }
            )
        calls = _load_run_attempts(connection, run.id)
    return _CompositionRun(tuple(results), tuple(observations), calls)


def _run_case(
    connection: psycopg.Connection[tuple[object, ...]],
    run: OrchestrationRun,
    input_path: str,
    content: CompositionFixtureInput,
    *,
    openrouter_api_key: str,
    typesafe_api_key: str | None,
    model_sender: ChatCompletionSender | None,
    generation_sender: GenerationSender | None,
    jev_sender: JevSender | None,
) -> CompositionExpected:
    registration = register_discoveries(
        connection,
        run_id=run.id,
        keyword=content.keyword,
        domain=content.domain,
        raw_urls=(content.raw_url,),
        discovered_at=content.observed_at,
    )
    if registration.new_work_count != 1:
        raise ValueError("Composition case did not register one new work item")
    scrape = _SCRAPE.validate_python(content.scrape)
    ats = _ATS.validate_python(content.ats_evidence)
    boundaries = PipelineBoundaries(
        search=lambda _keyword, _domain: SearchSucceeded(urls=()),
        scrape=lambda url: scrape if url == content.raw_url else _unexpected_url(url),
        fetch_ats=lambda url, _title: (ats if url == content.raw_url else _unexpected_url(url)),
        model_sender=model_sender,
        generation_sender=generation_sender,
        model_retry_policy=RetryPolicy(
            max_attempts=content.openrouter_settings.retry_limit + 1,
            base_delay_seconds=0,
        ),
        jev_sender=jev_sender,
        jev_retry_policy=JevRetryPolicy(
            max_attempts=content.relevance_settings.retry_limit + 1,
            base_delay_seconds=0,
        ),
    )
    summary = process_claimed_jobs(
        connection,
        run,
        boundaries,
        openrouter_api_key=openrouter_api_key,
        typesafe_api_key=typesafe_api_key,
        owner_token=uuid4(),
        observed_at=content.observed_at,
        max_items=1,
        lease_for=timedelta(minutes=5),
        retry_after=timedelta(minutes=2),
        enable_ats_enrichment=input_path == "ats",
        now=lambda: content.observed_at,
    )
    if summary.claimed_count != 1:
        raise ValueError("Composition case did not process one work item")
    return _read_case_observation(connection, content.raw_url)


def _load_run_attempts(
    connection: psycopg.Connection[tuple[object, ...]], run_id: UUID
) -> tuple[tuple[Provider, ModelCallAttempt], ...]:
    rows = connection.execute(
        """
        SELECT m.provider, projection.payload
        FROM model_call_attempts m
        JOIN langfuse_projection_items projection
          ON projection.kind = 'model_call' AND projection.source_id = m.id::text
        WHERE m.pipeline_run_id = %s
        ORDER BY m.observed_at, m.id
        """,
        (run_id,),
    ).fetchall()
    return tuple(
        (_PROVIDER.validate_python(provider), _ATTEMPT.validate_python(payload))
        for provider, payload in rows
    )


def _composition_coverage(
    fixtures: PhaseFixtureSet,
    inputs: tuple[CompositionFixtureInput, ...],
    execution: _CompositionRun,
) -> dict[str, bool]:
    operation_keys = {attempt.context.operation_key for _, attempt in execution.calls}
    return {
        "direct": any(case.input_path == "direct" for case in fixtures.cases),
        "ats": any(case.input_path == "ats" for case in fixtures.cases),
        "qualified": any(item.decision_outcome == "qualified" for item in execution.observations),
        "rejected": any(item.decision_outcome == "rejected" for item in execution.observations),
        "retry": any(
            isinstance(_SCRAPE.validate_python(content.scrape), JinaUnavailable)
            and observation.work_state == "failed"
            for content, observation in zip(inputs, execution.observations, strict=True)
        ),
        "relevance": any(key.startswith("evaluation:") for key in operation_keys),
        "enrichment": "enrichment" in operation_keys,
        "deduplication": "deduplication" in operation_keys,
    }


def _read_case_observation(
    connection: psycopg.Connection[tuple[object, ...]], raw_url: str
) -> CompositionExpected:
    row = connection.execute(
        """
        SELECT decision.outcome, decision.decision_stage, work.state,
               review.evaluation_id IS NOT NULL
        FROM jobs job
        JOIN job_work_items work ON work.job_id = job.id
        LEFT JOIN job_snapshots snapshot ON snapshot.job_id = job.id
        LEFT JOIN evaluation_decisions decision ON decision.snapshot_id = snapshot.id
        LEFT JOIN review_items review ON review.evaluation_id = decision.id
        WHERE job.raw_url = %s
        ORDER BY snapshot.observed_at DESC NULLS LAST
        LIMIT 1
        """,
        (raw_url,),
    ).fetchone()
    if row is None:
        raise ValueError("Composition case work item disappeared")
    return CompositionExpected(
        decision_outcome=None if row[0] is None else str(row[0]),
        decision_stage=None if row[1] is None else str(row[1]),
        work_state=str(row[2]),
        review_enqueued=bool(row[3]),
    )


def _unexpected_url(url: str) -> Never:
    raise ValueError(f"Unexpected composition URL: {url}")
