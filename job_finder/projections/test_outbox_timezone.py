from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Literal, cast

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel, JsonValue

from job_finder.projections.outbox import enqueue_projection


class _CreatedPayload(BaseModel):
    created_at: datetime


class _CompletedPayload(BaseModel):
    completed_at: datetime


class _CaptureConnection:
    params: tuple[object, ...] | None = None

    def execute(self, _query: str, params: tuple[object, ...]) -> None:
        self.params = params


def _capture(
    kind: Literal["evaluation_manifest", "evaluation_run", "prompt_promotion"],
    payload: BaseModel,
    observed_at: datetime,
) -> dict[str, JsonValue]:
    connection = _CaptureConnection()
    enqueue_projection(
        cast(psycopg.Connection[tuple[object, ...]], cast(object, connection)),
        kind,
        "example",
        payload,
        observed_at,
    )
    assert connection.params is not None
    payloads = [param for param in connection.params if isinstance(param, Jsonb)]
    assert len(payloads) == 1
    return cast(dict[str, JsonValue], payloads[0].obj)


def test_benchmark_projection_has_one_payload_for_the_same_instant() -> None:
    utc = datetime(2026, 9, 21, 12, tzinfo=UTC)
    shifted = utc.astimezone(timezone(timedelta(hours=3)))

    def capture(
        kind: Literal["evaluation_manifest", "evaluation_run", "prompt_promotion"],
        payload: BaseModel,
        moment: datetime,
    ) -> dict[str, JsonValue]:
        captured = _capture(kind, payload, moment)
        serialized_timestamps = [
            value for field, value in captured.items() if field in ("created_at", "completed_at")
        ]
        assert serialized_timestamps
        for value in serialized_timestamps:
            assert cast(str, value).endswith(("Z", "+00:00"))
        return captured

    assert capture("evaluation_manifest", _CreatedPayload(created_at=utc), utc) == capture(
        "evaluation_manifest", _CreatedPayload(created_at=shifted), shifted
    )
    assert capture("evaluation_run", _CompletedPayload(completed_at=utc), utc) == capture(
        "evaluation_run", _CompletedPayload(completed_at=shifted), shifted
    )
    assert capture("prompt_promotion", _CreatedPayload(created_at=utc), utc) == capture(
        "prompt_promotion", _CreatedPayload(created_at=shifted), shifted
    )
