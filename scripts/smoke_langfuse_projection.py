from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from langfuse import Langfuse
from pydantic import JsonValue, TypeAdapter, ValidationError

from job_finder.config import LangfuseSettings
from job_finder.evaluation.langfuse import (
    LangfuseProjection,
    create_langfuse_projection_sender,
)
from job_finder.evaluation.models import (
    InputDigest,
    ModelCallAttempt,
    ModelCallContext,
    ModelRequestId,
    PromptReleaseId,
    PromptVersionId,
)


def main() -> int:
    settings = LangfuseSettings.from_environment()
    projection = _projection()
    sender = create_langfuse_projection_sender(settings)
    response = sender(projection)
    repeated_response = sender(projection)
    if repeated_response.remote_id != response.remote_id:
        raise RuntimeError("Projection retry changed the remote trace identity")
    client = Langfuse(
        public_key=settings.public_key,
        secret_key=settings.secret_key,
        base_url=str(settings.base_url),
        environment=settings.environment,
    )
    observations = client.api.observations.get_many(
        trace_id=response.remote_id,
        fields="core,basic,metadata,model,usage",
        limit=100,
    ).data
    matching = tuple(
        item for item in observations if _has_projection_id(item.metadata, projection.id)
    )
    client.shutdown()
    if len(matching) != 1:
        raise RuntimeError(f"Expected one projected observation, found {len(matching)}")
    observation = matching[0]
    if observation.type != "GENERATION":
        raise RuntimeError(f"Expected GENERATION, found {observation.type}")
    if observation.model != "smoke-model":
        raise RuntimeError(f"Expected smoke-model, found {observation.model}")
    if observation.usage_details != {"input": 12, "output": 4, "total": 16}:
        raise RuntimeError(f"Unexpected usage: {observation.usage_details}")
    print(f"trace_id={response.remote_id}")
    print(f"observation_id={observation.id}")
    print(f"type={observation.type}")
    print(f"model={observation.model}")
    print(f"usage={observation.usage_details}")
    print(f"cost={observation.cost_details}")
    print(f"retry_matching_projection_count={len(matching)}")
    return 0


def _projection() -> LangfuseProjection:
    now = datetime.now(UTC)
    pipeline_run_id = uuid4()
    attempt = ModelCallAttempt(
        id=uuid4(),
        context=ModelCallContext(
            processing_attempt_id=uuid4(),
            pipeline_run_id=pipeline_run_id,
            prompt_release_id=PromptReleaseId("1" * 64),
            operation_key="langfuse-live-smoke",
            input_digest=InputDigest("2" * 64),
        ),
        request_id=ModelRequestId("3" * 64),
        attempt_number=0,
        prompt_name="job-finder-langfuse-live-smoke",
        prompt_version_id=PromptVersionId("4" * 64),
        request_messages=({"role": "user", "content": "Live projection smoke test."},),
        requested_model="smoke-model",
        response_model="smoke-model",
        provider_response_id=f"smoke-{pipeline_run_id}",
        status="accepted",
        parsed_output={"pass": True, "reason": "Live projection smoke test."},
        raw_response={"smoke": True},
        input_tokens=12,
        output_tokens=4,
        cost_usd=Decimal("0.00012"),
        latency_ms=25,
        error=None,
        observed_at=now,
    )
    payload = TypeAdapter(dict[str, JsonValue]).validate_json(
        TypeAdapter(ModelCallAttempt).dump_json(attempt)
    )
    payload_digest = _digest(payload)
    projection_id = _digest({"kind": "model_call", "source_id": str(attempt.id)})
    return LangfuseProjection(
        id=projection_id,
        idempotency_key=projection_id,
        kind="model_call",
        source_id=str(attempt.id),
        payload_digest=payload_digest,
        payload=payload,
        attempt_count=1,
    )


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _has_projection_id(metadata: object, projection_id: str) -> bool:
    try:
        parsed = TypeAdapter(dict[str, JsonValue]).validate_python(metadata)
    except ValidationError:
        return False
    return parsed.get("projection_id") == projection_id


if __name__ == "__main__":
    raise SystemExit(main())
