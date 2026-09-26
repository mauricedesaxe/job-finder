from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from typing import cast
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb
from pydantic import JsonValue

from job_finder.evaluation.models import (
    InputDigest,
    ModelCallAttempt,
    ModelCallContext,
    ModelRequestId,
    PromptReleaseId,
    PromptVersionId,
)
from job_finder.evaluation.openrouter import enqueue_model_call_projection


class _CaptureConnection:
    params: tuple[object, ...] | None = None

    def execute(self, _query: str, params: tuple[object, ...]) -> None:
        self.params = params


def test_model_call_projection_has_one_payload_for_the_same_instant() -> None:
    observed_at = datetime(2026, 9, 21, 12, tzinfo=UTC)
    attempt = ModelCallAttempt(
        id=UUID(int=1),
        context=ModelCallContext(
            processing_attempt_id=UUID(int=2),
            pipeline_run_id=UUID(int=3),
            prompt_release_id=PromptReleaseId("a" * 64),
            operation_key="example",
            input_digest=InputDigest("b" * 64),
        ),
        request_id=ModelRequestId("c" * 64),
        attempt_number=0,
        prompt_name="example",
        prompt_version_id=PromptVersionId("d" * 64),
        requested_model="example",
        response_model="example",
        provider_response_id=None,
        status="accepted",
        parsed_output={"pass": True},
        raw_response={},
        input_tokens=1,
        output_tokens=1,
        cost_usd=None,
        latency_ms=1,
        error=None,
        observed_at=observed_at,
    )

    def capture(item: ModelCallAttempt) -> tuple[str, object]:
        connection = _CaptureConnection()
        enqueue_model_call_projection(
            cast(psycopg.Connection[tuple[object, ...]], cast(object, connection)), item
        )
        assert connection.params is not None
        return cast(str, connection.params[2]), cast(
            dict[str, JsonValue], cast(Jsonb, connection.params[3]).obj
        )

    shifted = replace(attempt, observed_at=observed_at.astimezone(timezone(timedelta(hours=3))))
    assert capture(attempt) == capture(shifted)
