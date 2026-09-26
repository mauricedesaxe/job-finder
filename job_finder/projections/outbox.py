from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, ClassVar, Literal
from uuid import UUID

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError

from job_finder.database import Connection as _Connection

ProjectionKind = Literal["evaluation_manifest", "evaluation_run", "prompt_promotion", "model_call"]
_BenchmarkProjectionKind = Literal["evaluation_manifest", "evaluation_run", "prompt_promotion"]
_METADATA = TypeAdapter(dict[str, JsonValue])
_PROJECTION_KIND: TypeAdapter[ProjectionKind] = TypeAdapter(ProjectionKind)
_TIMESTAMP: TypeAdapter[datetime] = TypeAdapter(datetime)


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


class ProjectionFailureSummary(ProjectionModel):
    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    kind: ProjectionKind
    source_id: str
    attempt_count: int = Field(ge=0)
    retry_at: datetime | None
    error_code: str


class ProjectionQueueStatus(ProjectionModel):
    pending_count: int = Field(ge=0)
    leased_count: int = Field(ge=0)
    completed_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    failures: Annotated[tuple[ProjectionFailureSummary, ...], Field(max_length=100)]


ProjectionDeliveryResult = (
    ProjectionDelivered | ProjectionFailed | ProjectionLeaseLost | ProjectionIdle
)
ProjectionSender = Callable[[LangfuseProjection], object]
TypedProjectionSender = Callable[[LangfuseProjection], LangfuseProjectionResponse]


class LangfuseUnavailable(RuntimeError):
    pass


def enqueue_projection(
    connection: _Connection,
    kind: _BenchmarkProjectionKind,
    source_id: str,
    payload: BaseModel,
    created_at: datetime,
) -> None:
    data = payload.model_dump(mode="json")
    for field in ("created_at", "completed_at"):
        moment = getattr(payload, field, None)
        if isinstance(moment, datetime):
            data[field] = _TIMESTAMP.dump_python(moment.astimezone(UTC), mode="json")
    payload_digest = _digest(data)
    projection_id = _digest({"kind": kind, "source_id": source_id})
    _ = connection.execute(
        """
        INSERT INTO langfuse_projection_items (
          id, kind, source_id, payload_digest, payload, state, created_at
        ) VALUES (%s, %s, %s, %s, %s, 'pending', %s)
        ON CONFLICT (kind, source_id) DO NOTHING
        """,
        (projection_id, kind, source_id, payload_digest, Jsonb(data), created_at),
    )


def deliver_next_projection(
    connection: _Connection,
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


def load_projection_queue_status(
    connection: _Connection,
    *,
    failure_limit: int = 20,
) -> ProjectionQueueStatus:
    _require_autocommit(connection)
    if failure_limit < 0 or failure_limit > 100:
        raise ValueError("Projection failure limit must be between 0 and 100")
    counts = {"pending": 0, "leased": 0, "completed": 0, "failed": 0}
    for row in connection.execute(
        "SELECT state, count(*) FROM langfuse_projection_items GROUP BY state"
    ).fetchall():
        counts[str(row[0])] = int(str(row[1]))
    rows = connection.execute(
        """
        SELECT id, kind, source_id, attempt_count, retry_at, last_error
        FROM langfuse_projection_items
        WHERE state = 'failed'
        ORDER BY COALESCE(retry_at, created_at), id
        LIMIT %s
        """,
        (failure_limit,),
    ).fetchall()
    return ProjectionQueueStatus(
        pending_count=counts["pending"],
        leased_count=counts["leased"],
        completed_count=counts["completed"],
        failed_count=counts["failed"],
        failures=tuple(_parse_projection_failure(row) for row in rows),
    )


def _parse_projection_failure(row: tuple[object, ...]) -> ProjectionFailureSummary:
    last_error = _METADATA.validate_python(row[5])
    return ProjectionFailureSummary(
        id=str(row[0]),
        kind=_PROJECTION_KIND.validate_python(row[1]),
        source_id=str(row[2]),
        attempt_count=int(str(row[3])),
        retry_at=(datetime.fromisoformat(str(row[4])) if row[4] is not None else None),
        error_code=str(last_error.get("code", "unknown")),
    )


def _lease_next(
    connection: _Connection,
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
    connection: _Connection,
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


def _digest(value: object) -> str:
    content = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(content.encode()).hexdigest()


def _require_autocommit(connection: _Connection) -> None:
    if not connection.autocommit:
        raise ValueError("Langfuse projection delivery requires an autocommit connection")
