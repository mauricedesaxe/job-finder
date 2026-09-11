import threading
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import TypeAdapter

from job_finder.config import LangfuseSettings
from job_finder.evaluation.models import (
    InputDigest,
    ModelCallAttempt,
    ModelCallContext,
    ModelRequestId,
    PromptReleaseId,
    PromptVersionId,
)
from job_finder.evaluation.langfuse import (
    LangfuseGateway,
    LangfuseProjection,
    LangfuseUnavailable,
    ObservationProjection,
    create_langfuse_projection_sender,
)
from job_finder.evaluation.manifests import (
    EvaluationCaseInput,
    EvaluationManifest,
    EvaluationManifestCase,
    EvaluationMetrics,
    EvaluationRun,
    EvaluationTrialResult,
    ManifestPolicy,
    PromptPromotionDecision,
)

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


def test_projects_manifest_run_and_promotion_with_stable_remote_identities() -> None:
    datasets: list[tuple[str, str]] = []
    items: list[tuple[str, str]] = []
    observations: list[ObservationProjection] = []
    gateway = LangfuseGateway(
        create_dataset=lambda name, _description, metadata: _record_dataset(
            datasets, name, str(metadata["manifest_id"])
        ),
        create_dataset_item=lambda dataset, item_id, _input, _expected, _metadata: (
            _record_item(items, dataset, item_id)
        ),
        send_observation=lambda observation: _record_observation(observations, observation),
    )
    sender = create_langfuse_projection_sender(_settings(), gateway=gateway)

    manifest_response = sender(_projection("evaluation_manifest", _manifest()))
    run_response = sender(_projection("evaluation_run", _run()))
    promotion_response = sender(_projection("prompt_promotion", _promotion()))
    model_call_response = sender(_model_call_projection())
    repeated_run_response = sender(_projection("evaluation_run", _run()))

    assert manifest_response.remote_id == "dataset-manifest"
    assert len(datasets) == 1
    assert len(items) == 1
    assert run_response.remote_id == observations[0].trace_id
    assert promotion_response.remote_id == observations[1].trace_id
    assert repeated_run_response.remote_id == run_response.remote_id
    assert observations[0].observation_type == "EVALUATOR"
    assert observations[1].observation_type == "EVENT"
    assert observations[2].observation_type == "GENERATION"
    assert observations[2].usage is not None
    assert observations[2].usage.input_tokens == 12
    assert observations[2].input["messages"] == [{"role": "user", "content": "job body"}]
    assert model_call_response.remote_id == observations[2].trace_id
    assert observations[0].trace_id == observations[3].trace_id


def test_a_hanging_gateway_send_becomes_langfuse_unavailable_after_the_timeout() -> None:
    release = threading.Event()

    def hang(observation: ObservationProjection) -> str:
        release.wait(10)
        return observation.trace_id

    gateway = LangfuseGateway(
        create_dataset=lambda name, _description, _metadata: "dataset-manifest",
        create_dataset_item=lambda dataset, item_id, _input, _expected, _metadata: item_id,
        send_observation=hang,
    )
    sender = create_langfuse_projection_sender(_settings(), gateway=gateway, send_timeout=0.2)

    try:
        with pytest.raises(LangfuseUnavailable, match="did not finish within 0 seconds"):
            _ = sender(_projection("evaluation_run", _run()))
    finally:
        release.set()


def _record_dataset(records: list[tuple[str, str]], name: str, manifest_id: str) -> str:
    records.append((name, manifest_id))
    return "dataset-manifest"


def _record_item(records: list[tuple[str, str]], dataset: str, item_id: str) -> str:
    records.append((dataset, item_id))
    return item_id


def _record_observation(
    records: list[ObservationProjection], observation: ObservationProjection
) -> str:
    records.append(observation)
    return observation.trace_id


def _settings() -> LangfuseSettings:
    return LangfuseSettings.model_validate(
        {
            "public_key": "pk-lf-public",
            "secret_key": "sk-lf-secret",
            "base_url": "https://cloud.langfuse.com",
            "environment": "test",
        }
    )


def _projection(
    kind: str,
    payload: EvaluationManifest | EvaluationRun | PromptPromotionDecision,
) -> LangfuseProjection:
    return LangfuseProjection.model_validate(
        {
            "id": "a" * 64,
            "idempotency_key": "a" * 64,
            "kind": kind,
            "source_id": payload.id,
            "payload_digest": "b" * 64,
            "payload": payload.model_dump(mode="json"),
            "attempt_count": 1,
        }
    )


def _model_call_projection() -> LangfuseProjection:
    attempt = ModelCallAttempt(
        id=UUID(int=10),
        context=ModelCallContext(
            processing_attempt_id=UUID(int=11),
            pipeline_run_id=UUID(int=12),
            prompt_release_id=PromptReleaseId("5" * 64),
            operation_key="evaluation:test",
            input_digest=InputDigest("b" * 64),
        ),
        request_id=ModelRequestId("c" * 64),
        attempt_number=0,
        prompt_name="job-finder-filter-location-eligibility",
        prompt_version_id=PromptVersionId("d" * 64),
        request_messages=({"role": "user", "content": "job body"},),
        requested_model="google/gemini-2.5-flash",
        response_model="google/gemini-2.5-flash-001",
        provider_response_id="generation-1",
        status="accepted",
        parsed_output={"pass": True, "reason": "Matched."},
        raw_response={"id": "generation-1"},
        input_tokens=12,
        output_tokens=4,
        cost_usd=Decimal("0.00012"),
        latency_ms=250,
        error=None,
        observed_at=NOW,
    )
    return LangfuseProjection.model_validate(
        {
            "id": "e" * 64,
            "idempotency_key": "e" * 64,
            "kind": "model_call",
            "source_id": str(attempt.id),
            "payload_digest": "f" * 64,
            "payload": TypeAdapter(ModelCallAttempt).dump_python(attempt, mode="json"),
            "attempt_count": 1,
        }
    )


def _manifest() -> EvaluationManifest:
    return EvaluationManifest(
        id="1" * 64,
        policy=ManifestPolicy(),
        cases=(
            EvaluationManifestCase(
                position=0,
                curation_id=UUID(int=1),
                review_event_id=UUID(int=2),
                expected_outcome="qualified",
                critical=False,
                trial_count=1,
                input=EvaluationCaseInput(
                    title="Engineer",
                    company="Acme",
                    url="https://example.com/job",
                    source="other",
                    description="Build useful tools.",
                    location="Remote",
                    keywords=("python",),
                    date_posted=NOW.date(),
                    observed_at=NOW,
                    original_outcome="qualified",
                    review_decision="pursue",
                    target_profile="applied-ai-product-engineer",
                ),
            ),
        ),
        created_at=NOW,
        created_by="owner",
    )


def _run() -> EvaluationRun:
    result = EvaluationTrialResult(
        id="3" * 64,
        case_position=0,
        trial_index=0,
        expected_outcome="qualified",
        actual_outcome="qualified",
        failure_kind=None,
        reason="Matched.",
    )
    return EvaluationRun(
        id="4" * 64,
        idempotency_key="run-1",
        manifest_id="1" * 64,
        prompt_release_id="5" * 64,
        implementation_ref="commit-1",
        metrics=EvaluationMetrics(
            result_count=1,
            false_positive_count=0,
            false_negative_count=0,
            operational_failure_count=0,
            critical_false_positive_count=0,
            false_positive_rate=Decimal(0),
            false_negative_rate=Decimal(0),
        ),
        results=(result,),
        completed_at=NOW,
    )


def _promotion() -> PromptPromotionDecision:
    return PromptPromotionDecision(
        id="6" * 64,
        manifest_id="1" * 64,
        baseline_run_id="7" * 64,
        baseline_prompt_release_id="8" * 64,
        candidate_run_id="9" * 64,
        candidate_prompt_release_id="a" * 64,
        decision="approved",
        reason="Candidate clears every promotion check.",
        actor="owner",
        created_at=NOW,
    )
