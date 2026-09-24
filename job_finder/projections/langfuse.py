from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import Thread
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from langfuse import Langfuse
from pydantic import Field, JsonValue, TypeAdapter, ValidationError

from job_finder.benchmarks.executions import EvaluationRun
from job_finder.benchmarks.manifests import EvaluationManifest
from job_finder.benchmarks.promotions import PromptPromotionDecision
from job_finder.config import LangfuseSettings
from job_finder.evaluation.models import ModelCallAttempt
from job_finder.projections.outbox import (
    LangfuseProjection as _LangfuseProjection,
    LangfuseProjectionResponse as _LangfuseProjectionResponse,
    LangfuseUnavailable as _LangfuseUnavailable,
    ProjectionModel as _ProjectionModel,
    TypedProjectionSender as _TypedProjectionSender,
)

_MODEL_CALL = TypeAdapter(ModelCallAttempt)
_METADATA = TypeAdapter(dict[str, JsonValue])
_JSON_VALUES = TypeAdapter(list[JsonValue])
_READ_BACK_TIMEOUT_SECONDS = 30.0
_READ_BACK_POLL_SECONDS = 1.0
_SEND_TIMEOUT_SECONDS = 120.0
_SDK_HTTP_TIMEOUT_SECONDS = 15


class ObservationUsage(_ProjectionModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_usd: float | None = Field(default=None, ge=0)


class ObservationProjection(_ProjectionModel):
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    observation_type: Literal["EVALUATOR", "EVENT", "GENERATION"]
    name: str
    input: dict[str, JsonValue]
    output: dict[str, JsonValue]
    metadata: dict[str, JsonValue]
    version: str | None = None
    model: str | None = None
    usage: ObservationUsage | None = None
    started_at: datetime
    observed_at: datetime


CreateDataset = Callable[[str, str, dict[str, JsonValue]], str]
CreateDatasetItem = Callable[
    [str, str, dict[str, JsonValue], dict[str, JsonValue], dict[str, JsonValue]], str
]
SendObservation = Callable[[ObservationProjection], str]


@dataclass(frozen=True)
class LangfuseGateway:
    create_dataset: CreateDataset
    create_dataset_item: CreateDatasetItem
    send_observation: SendObservation


def create_langfuse_projection_sender(
    settings: LangfuseSettings,
    *,
    gateway: LangfuseGateway | None = None,
    send_timeout: float = _SEND_TIMEOUT_SECONDS,
) -> _TypedProjectionSender:
    target = gateway or _sdk_gateway(settings)

    def send(projection: _LangfuseProjection) -> _LangfuseProjectionResponse:
        remote_ids: list[str] = []
        failures: list[BaseException] = []

        def call() -> None:
            try:
                remote_ids.append(_project(target, projection))
            except BaseException as error:
                failures.append(error)

        worker = Thread(target=call, daemon=True)
        worker.start()
        worker.join(send_timeout)
        if worker.is_alive():
            # A hung SDK call once held the projection pool slot forever.
            raise _LangfuseUnavailable(
                f"projection send did not finish within {send_timeout:.0f} seconds"
            )
        if failures:
            error = failures[0]
            if isinstance(error, ValidationError):
                raise error
            raise _LangfuseUnavailable(str(error)) from error
        return _LangfuseProjectionResponse(remote_id=remote_ids[0])

    return send


def _project(gateway: LangfuseGateway, projection: _LangfuseProjection) -> str:
    match projection.kind:
        case "evaluation_manifest":
            return _project_manifest(gateway, EvaluationManifest.model_validate(projection.payload))
        case "evaluation_run":
            run = EvaluationRun.model_validate(projection.payload)
            return gateway.send_observation(_run_observation(projection, run))
        case "prompt_promotion":
            promotion = PromptPromotionDecision.model_validate(projection.payload)
            return gateway.send_observation(_promotion_observation(projection, promotion))
        case "model_call":
            attempt = _MODEL_CALL.validate_python(projection.payload)
            return gateway.send_observation(_model_call_observation(projection, attempt))


def _project_manifest(gateway: LangfuseGateway, manifest: EvaluationManifest) -> str:
    dataset_name = _dataset_name(manifest.id)
    dataset_id = gateway.create_dataset(
        dataset_name,
        "Immutable job-finder evaluation manifest",
        {
            "manifest_id": manifest.id,
            "policy": manifest.policy.model_dump(mode="json"),
            "created_at": manifest.created_at.isoformat(),
            "created_by": manifest.created_by,
        },
    )
    for case in manifest.cases:
        _ = gateway.create_dataset_item(
            dataset_name,
            str(uuid5(NAMESPACE_URL, f"job-finder:{manifest.id}:{case.position}")),
            case.input.model_dump(mode="json"),
            {"outcome": case.expected_outcome},
            {
                "position": case.position,
                "critical": case.critical,
                "trial_count": case.trial_count,
                "curation_id": str(case.curation_id),
                "review_event_id": str(case.review_event_id),
            },
        )
    return dataset_id


def _run_observation(projection: _LangfuseProjection, run: EvaluationRun) -> ObservationProjection:
    return _observation(
        projection,
        "EVALUATOR",
        "job-finder-evaluation-run",
        {
            "run_id": run.id,
            "manifest_id": run.manifest_id,
            "prompt_release_id": run.prompt_release_id,
            "target": None if run.target is None else run.target.model_dump(mode="json"),
        },
        {
            "metrics": run.metrics.model_dump(mode="json"),
            "results": [result.model_dump(mode="json") for result in run.results],
        },
        {"idempotency_key": run.idempotency_key},
        run.completed_at,
        version=run.implementation_ref,
    )


def _promotion_observation(
    projection: _LangfuseProjection, promotion: PromptPromotionDecision
) -> ObservationProjection:
    return _observation(
        projection,
        "EVENT",
        "job-finder-prompt-promotion",
        {
            "manifest_id": promotion.manifest_id,
            "baseline_run_id": promotion.baseline_run_id,
            "candidate_run_id": promotion.candidate_run_id,
        },
        {"decision": promotion.decision, "reason": promotion.reason},
        {
            "actor": promotion.actor,
            "baseline_prompt_release_id": promotion.baseline_prompt_release_id,
            "candidate_prompt_release_id": promotion.candidate_prompt_release_id,
            "baseline_target": (
                None
                if promotion.baseline_target is None
                else promotion.baseline_target.model_dump(mode="json")
            ),
            "candidate_target": (
                None
                if promotion.candidate_target is None
                else promotion.candidate_target.model_dump(mode="json")
            ),
            "comparison_id": promotion.comparison_id,
            "eligible": promotion.eligible,
            "eligibility_failures": list(promotion.eligibility_failures),
        },
        promotion.created_at,
    )


def _model_call_observation(
    projection: _LangfuseProjection, attempt: ModelCallAttempt
) -> ObservationProjection:
    usage = (
        ObservationUsage(
            input_tokens=attempt.input_tokens,
            output_tokens=attempt.output_tokens,
            cost_usd=float(attempt.cost_usd) if attempt.cost_usd is not None else None,
        )
        if attempt.input_tokens is not None and attempt.output_tokens is not None
        else None
    )
    return _observation(
        projection,
        "GENERATION",
        attempt.prompt_name,
        {
            "operation_key": attempt.context.operation_key,
            "input_digest": attempt.context.input_digest,
            "prompt_release_id": attempt.context.prompt_release_id,
            "pipeline_run_id": str(attempt.context.pipeline_run_id),
            "messages": _JSON_VALUES.validate_python(attempt.request_messages),
        },
        {
            "status": attempt.status,
            "parsed_output": attempt.parsed_output,
            "raw_response": attempt.raw_response,
            "error": attempt.error,
        },
        {
            "attempt_number": attempt.attempt_number,
            "request_id": attempt.request_id,
            "provider_response_id": attempt.provider_response_id,
            "response_model": attempt.response_model,
            "latency_ms": attempt.latency_ms,
        },
        attempt.observed_at,
        version=attempt.prompt_version_id,
        model=attempt.requested_model,
        usage=usage,
        started_at=attempt.observed_at - timedelta(milliseconds=attempt.latency_ms),
    )


def _observation(
    projection: _LangfuseProjection,
    observation_type: Literal["EVALUATOR", "EVENT", "GENERATION"],
    name: str,
    input: dict[str, JsonValue],
    output: dict[str, JsonValue],
    metadata: dict[str, JsonValue],
    observed_at: datetime,
    *,
    version: str | None = None,
    model: str | None = None,
    usage: ObservationUsage | None = None,
    started_at: datetime | None = None,
) -> ObservationProjection:
    return ObservationProjection(
        trace_id=Langfuse.create_trace_id(seed=projection.idempotency_key),
        observation_type=observation_type,
        name=name,
        input=input,
        output=output,
        metadata={
            **metadata,
            "projection_id": projection.id,
            "payload_digest": projection.payload_digest,
        },
        version=version,
        model=model,
        usage=usage,
        started_at=started_at or observed_at,
        observed_at=observed_at,
    )


def _sdk_gateway(settings: LangfuseSettings) -> LangfuseGateway:
    client = Langfuse(
        public_key=settings.public_key,
        secret_key=settings.secret_key,
        base_url=str(settings.base_url),
        environment=settings.environment,
        timeout=_SDK_HTTP_TIMEOUT_SECONDS,
    )

    def create_dataset(name: str, description: str, metadata: dict[str, JsonValue]) -> str:
        return client.create_dataset(name=name, description=description, metadata=metadata).id

    def create_dataset_item(
        dataset_name: str,
        item_id: str,
        input: dict[str, JsonValue],
        expected_output: dict[str, JsonValue],
        metadata: dict[str, JsonValue],
    ) -> str:
        return client.create_dataset_item(
            dataset_name=dataset_name,
            id=item_id,
            input=input,
            expected_output=expected_output,
            metadata=metadata,
        ).id

    def send_observation(observation: ObservationProjection) -> str:
        if _observation_is_visible(client, observation):
            return observation.trace_id
        usage_details = (
            {
                "input": observation.usage.input_tokens,
                "output": observation.usage.output_tokens,
                "total": (observation.usage.input_tokens + observation.usage.output_tokens),
            }
            if observation.usage is not None
            else None
        )
        cost_details = (
            {"total_cost": observation.usage.cost_usd}
            if observation.usage is not None and observation.usage.cost_usd is not None
            else None
        )
        metadata = {
            **observation.metadata,
            "projected_started_at": observation.started_at.isoformat(),
            "projected_observed_at": observation.observed_at.isoformat(),
        }
        match observation.observation_type:
            case "EVALUATOR":
                span = client.start_observation(
                    trace_context={"trace_id": observation.trace_id},
                    name=observation.name,
                    as_type="evaluator",
                    input=observation.input,
                    output=observation.output,
                    metadata=metadata,
                    version=observation.version,
                )
                _ = span.end()
            case "EVENT":
                span = client.start_observation(
                    trace_context={"trace_id": observation.trace_id},
                    name=observation.name,
                    as_type="span",
                    input=observation.input,
                    output=observation.output,
                    metadata=metadata,
                    version=observation.version,
                )
                _ = span.end()
            case "GENERATION":
                generation = client.start_observation(
                    trace_context={"trace_id": observation.trace_id},
                    name=observation.name,
                    as_type="generation",
                    input=observation.input,
                    output=observation.output,
                    metadata=metadata,
                    version=observation.version,
                    model=observation.model,
                    usage_details=usage_details,
                    cost_details=cost_details,
                )
                _ = generation.end()
        client.flush()
        _wait_for_observation(client, observation)
        return observation.trace_id

    return LangfuseGateway(
        create_dataset=create_dataset,
        create_dataset_item=create_dataset_item,
        send_observation=send_observation,
    )


def _wait_for_observation(client: Langfuse, observation: ObservationProjection) -> None:
    deadline = time.monotonic() + _READ_BACK_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if _observation_is_visible(client, observation):
            return
        time.sleep(_READ_BACK_POLL_SECONDS)
    message = "Langfuse did not expose projection {} on trace {} within {:.0f} seconds".format(
        observation.metadata["projection_id"],
        observation.trace_id,
        _READ_BACK_TIMEOUT_SECONDS,
    )
    raise RuntimeError(message)


def _observation_is_visible(client: Langfuse, observation: ObservationProjection) -> bool:
    response = client.api.observations.get_many(
        trace_id=observation.trace_id,
        fields="metadata",
        limit=100,
    )
    return any(
        _has_projection_id(
            item.metadata,
            observation.metadata["projection_id"],
        )
        for item in response.data
    )


def _has_projection_id(metadata: object, projection_id: JsonValue) -> bool:
    try:
        parsed = _METADATA.validate_python(metadata)
    except ValidationError:
        return False
    return parsed.get("projection_id") == projection_id


def _dataset_name(manifest_id: str) -> str:
    return f"job-finder-evaluation-{manifest_id}"
