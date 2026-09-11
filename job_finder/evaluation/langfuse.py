from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import Thread
from typing import ClassVar, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

import psycopg
from langfuse import Langfuse
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError

from job_finder.config import LangfuseSettings
from job_finder.evaluation.models import ModelCallAttempt
from job_finder.evaluation.manifests import (
    EvaluationManifest,
    EvaluationRun,
    PromptPromotionDecision,
)

Connection = psycopg.Connection[tuple[object, ...]]
ProjectionKind = Literal["evaluation_manifest", "evaluation_run", "prompt_promotion", "model_call"]
_MODEL_CALL = TypeAdapter(ModelCallAttempt)
_METADATA = TypeAdapter(dict[str, JsonValue])
_JSON_VALUES = TypeAdapter(list[JsonValue])
_READ_BACK_TIMEOUT_SECONDS = 30.0
_READ_BACK_POLL_SECONDS = 1.0
_SEND_TIMEOUT_SECONDS = 120.0
_SDK_HTTP_TIMEOUT_SECONDS = 15


class ProjectionModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class LangfuseProjection(ProjectionModel):
    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    kind: ProjectionKind
    source_id: str
    payload_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    payload: dict[str, JsonValue]
    attempt_count: int = Field(gt=0)


class LangfuseProjectionResponse(ProjectionModel):
    remote_id: str = Field(min_length=1)


class ProjectionDelivered(ProjectionModel):
    kind: Literal["delivered"] = "delivered"
    projection_id: str
    remote_id: str


class ProjectionFailed(ProjectionModel):
    kind: Literal["failed"] = "failed"
    projection_id: str
    error_code: Literal["langfuse_unavailable", "invalid_response"]
    reason: str


class ProjectionLeaseLost(ProjectionModel):
    kind: Literal["lease_lost"] = "lease_lost"
    projection_id: str


class ProjectionIdle(ProjectionModel):
    kind: Literal["idle"] = "idle"


class ObservationUsage(ProjectionModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_usd: float | None = Field(default=None, ge=0)


class ObservationProjection(ProjectionModel):
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


ProjectionDeliveryResult = (
    ProjectionDelivered | ProjectionFailed | ProjectionLeaseLost | ProjectionIdle
)
ProjectionSender = Callable[[LangfuseProjection], object]
TypedProjectionSender = Callable[[LangfuseProjection], LangfuseProjectionResponse]
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


class LangfuseUnavailable(RuntimeError):
    pass


def create_langfuse_projection_sender(
    settings: LangfuseSettings,
    *,
    gateway: LangfuseGateway | None = None,
    send_timeout: float = _SEND_TIMEOUT_SECONDS,
) -> TypedProjectionSender:
    target = gateway or _sdk_gateway(settings)

    def send(projection: LangfuseProjection) -> LangfuseProjectionResponse:
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
            raise LangfuseUnavailable(
                f"projection send did not finish within {send_timeout:.0f} seconds"
            )
        if failures:
            error = failures[0]
            if isinstance(error, ValidationError):
                raise error
            raise LangfuseUnavailable(str(error)) from error
        return LangfuseProjectionResponse(remote_id=remote_ids[0])

    return send


def deliver_next_projection(
    connection: Connection,
    *,
    sender: ProjectionSender,
    owner_token: UUID,
    now: datetime,
    lease_for: timedelta,
    retry_after: timedelta,
) -> ProjectionDeliveryResult:
    _require_autocommit(connection)
    if retry_after < timedelta(0):
        raise ValueError("Projection retry delay cannot be negative")
    projection = _lease_next(connection, owner_token, now, lease_for)
    if projection is None:
        return ProjectionIdle()
    try:
        response = LangfuseProjectionResponse.model_validate(sender(projection))
    except LangfuseUnavailable as error:
        return _record_failure(
            connection,
            projection,
            owner_token,
            now + retry_after,
            "langfuse_unavailable",
            str(error),
        )
    except ValidationError as error:
        return _record_failure(
            connection,
            projection,
            owner_token,
            now + retry_after,
            "invalid_response",
            str(error),
        )
    with connection.transaction():
        changed = connection.execute(
            """
            UPDATE langfuse_projection_items
            SET state = 'completed', owner_token = NULL, lease_expires_at = NULL,
                remote_id = %s, completed_at = %s, last_error = NULL, retry_at = NULL
            WHERE id = %s AND state = 'leased' AND owner_token = %s
            """,
            (response.remote_id, now, projection.id, owner_token),
        ).rowcount
    if changed != 1:
        return ProjectionLeaseLost(projection_id=projection.id)
    return ProjectionDelivered(
        projection_id=projection.id,
        remote_id=response.remote_id,
    )


def _project(gateway: LangfuseGateway, projection: LangfuseProjection) -> str:
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


def _run_observation(projection: LangfuseProjection, run: EvaluationRun) -> ObservationProjection:
    return _observation(
        projection,
        "EVALUATOR",
        "job-finder-evaluation-run",
        {
            "manifest_id": run.manifest_id,
            "prompt_release_id": run.prompt_release_id,
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
    projection: LangfuseProjection, promotion: PromptPromotionDecision
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
        },
        promotion.created_at,
    )


def _model_call_observation(
    projection: LangfuseProjection, attempt: ModelCallAttempt
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
    projection: LangfuseProjection,
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


def _lease_next(
    connection: Connection,
    owner_token: UUID,
    now: datetime,
    lease_for: timedelta,
) -> LangfuseProjection | None:
    if lease_for <= timedelta(0):
        raise ValueError("Projection lease duration must be positive")
    with connection.transaction():
        row = connection.execute(
            """
            WITH candidate AS (
              SELECT id
              FROM langfuse_projection_items
              WHERE state = 'pending'
                 OR (state = 'failed' AND COALESCE(retry_at, created_at) <= %s)
                 OR (state = 'leased' AND lease_expires_at <= %s)
              ORDER BY created_at, id
              FOR UPDATE SKIP LOCKED
              LIMIT 1
            )
            UPDATE langfuse_projection_items item
            SET state = 'leased', owner_token = %s, lease_expires_at = %s,
                attempt_count = item.attempt_count + 1, retry_at = NULL
            FROM candidate
            WHERE item.id = candidate.id
            RETURNING item.id, item.kind, item.source_id, item.payload_digest,
                      item.payload, item.attempt_count
            """,
            (now, now, owner_token, now + lease_for),
        ).fetchone()
    if row is None:
        return None
    return LangfuseProjection.model_validate(
        {
            "id": row[0],
            "idempotency_key": row[0],
            "kind": row[1],
            "source_id": row[2],
            "payload_digest": row[3],
            "payload": row[4],
            "attempt_count": row[5],
        }
    )


def _record_failure(
    connection: Connection,
    projection: LangfuseProjection,
    owner_token: UUID,
    retry_at: datetime,
    error_code: Literal["langfuse_unavailable", "invalid_response"],
    reason: str,
) -> ProjectionFailed | ProjectionLeaseLost:
    error = {"code": error_code, "reason": reason}
    with connection.transaction():
        changed = connection.execute(
            """
            UPDATE langfuse_projection_items
            SET state = 'failed', owner_token = NULL, lease_expires_at = NULL,
                retry_at = %s, last_error = %s
            WHERE id = %s AND state = 'leased' AND owner_token = %s
            """,
            (retry_at, Jsonb(error), projection.id, owner_token),
        ).rowcount
    if changed != 1:
        return ProjectionLeaseLost(projection_id=projection.id)
    return ProjectionFailed(
        projection_id=projection.id,
        error_code=error_code,
        reason=reason,
    )


def _require_autocommit(connection: Connection) -> None:
    if not connection.autocommit:
        raise ValueError("Langfuse projection delivery requires an autocommit connection")
