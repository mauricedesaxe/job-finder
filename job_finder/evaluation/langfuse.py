from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import ClassVar, Literal, Never
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

Connection = psycopg.Connection[tuple[object, ...]]


class ProjectionModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class LangfuseProjection(ProjectionModel):
    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    kind: str
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


ProjectionDeliveryResult = (
    ProjectionDelivered | ProjectionFailed | ProjectionLeaseLost | ProjectionIdle
)
ProjectionSender = Callable[[LangfuseProjection], object]


class LangfuseUnavailable(RuntimeError):
    pass


def unavailable_projection_sender(_projection: LangfuseProjection) -> Never:
    raise LangfuseUnavailable(
        "No supported Langfuse v4 mapping exists for the durable projection payload"
    )


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
