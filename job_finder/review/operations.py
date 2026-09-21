from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal, cast
from uuid import UUID

from job_finder.review.postgres import Connection, ConnectionFactory

PipelineRunStatus = Literal["running", "completed", "failed"]
FailureSource = Literal["pipeline", "job"]


class OperationsHealth(StrEnum):
    CAUGHT_UP = "caught_up"
    WORKING = "working"
    ACTION_REQUIRED = "action_required"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class QueueCounts:
    pending: int = 0
    leased: int = 0
    retrying: int = 0
    completed: int = 0
    terminal_error: int = 0

    def __post_init__(self) -> None:
        if (
            min(
                self.pending,
                self.leased,
                self.retrying,
                self.completed,
                self.terminal_error,
            )
            < 0
        ):
            raise ValueError("queue counts cannot be negative")

    @property
    def active(self) -> int:
        return self.pending + self.leased + self.retrying


@dataclass(frozen=True)
class SpendSummary:
    known_usd: Decimal
    unknown_attempts: int

    def __post_init__(self) -> None:
        if self.known_usd < 0 or self.unknown_attempts < 0:
            raise ValueError("spend values cannot be negative")


@dataclass(frozen=True)
class PipelineRunSummary:
    id: UUID
    kind: str
    status: PipelineRunStatus
    started_at: datetime
    completed_at: datetime | None

    def __post_init__(self) -> None:
        if (self.status == "running") != (self.completed_at is None):
            raise ValueError("run completion does not match its status")


@dataclass(frozen=True)
class FailureSample:
    source: FailureSource
    occurred_at: datetime
    summary: str


@dataclass(frozen=True)
class OperationsSnapshot:
    health: OperationsHealth
    queues: QueueCounts
    spend: SpendSummary
    recent_runs: tuple[PipelineRunSummary, ...]
    failures: tuple[FailureSample, ...]

    def __post_init__(self) -> None:
        if self.health is not operations_health(self.queues, self.recent_runs):
            raise ValueError("health does not match the operations evidence")


@dataclass(frozen=True)
class OperationsService:
    load: Callable[[], OperationsSnapshot]


def operations_health(
    queues: QueueCounts,
    recent_runs: tuple[PipelineRunSummary, ...],
) -> OperationsHealth:
    if queues.terminal_error > 0:
        return OperationsHealth.ACTION_REQUIRED
    if recent_runs and recent_runs[0].status == "failed":
        return OperationsHealth.ACTION_REQUIRED
    if queues.active > 0:
        return OperationsHealth.WORKING
    if not recent_runs:
        return OperationsHealth.UNKNOWN
    newest = recent_runs[0]
    if newest.status == "running":
        return OperationsHealth.WORKING
    return OperationsHealth.CAUGHT_UP


def unknown_operations_service() -> OperationsService:
    snapshot = OperationsSnapshot(
        health=OperationsHealth.UNKNOWN,
        queues=QueueCounts(),
        spend=SpendSummary(known_usd=Decimal(0), unknown_attempts=0),
        recent_runs=(),
        failures=(),
    )
    return OperationsService(load=lambda: snapshot)


def postgres_operations_service(connect: ConnectionFactory) -> OperationsService:
    def load() -> OperationsSnapshot:
        with connect() as connection:
            return load_operations_snapshot(connection)

    return OperationsService(load=load)


def load_operations_snapshot(
    connection: Connection,
    *,
    recent_run_limit: int = 10,
    failure_limit: int = 8,
) -> OperationsSnapshot:
    if recent_run_limit < 1:
        raise ValueError("recent run limit must be positive")
    if failure_limit < 0:
        raise ValueError("failure limit cannot be negative")

    queue_rows = connection.execute(
        """
        SELECT state, count(*)
        FROM job_work_items
        GROUP BY state
        """
    ).fetchall()
    queue_values = {str(row[0]): int(str(row[1])) for row in queue_rows}
    queues = QueueCounts(
        pending=queue_values.get("pending", 0),
        leased=queue_values.get("leased", 0),
        retrying=queue_values.get("failed", 0),
        completed=queue_values.get("completed", 0),
        terminal_error=queue_values.get("terminal_error", 0),
    )

    run_rows = connection.execute(
        """
        SELECT id, kind, status, started_at, completed_at
        FROM pipeline_runs
        ORDER BY started_at DESC, id DESC
        LIMIT %s
        """,
        (recent_run_limit,),
    ).fetchall()
    recent_runs = tuple(_parse_run(row) for row in run_rows)

    spend_row = connection.execute(
        """
        SELECT COALESCE(sum(cost_usd), 0), count(*) FILTER (WHERE cost_usd IS NULL)
        FROM model_call_attempts
        """
    ).fetchone()
    if spend_row is None:
        raise RuntimeError("Could not read model spend")
    spend = SpendSummary(
        known_usd=Decimal(str(spend_row[0])),
        unknown_attempts=int(str(spend_row[1])),
    )

    failure_rows = connection.execute(
        """
        SELECT source, occurred_at, error
        FROM (
          SELECT 'pipeline' AS source,
                 COALESCE(completed_at, started_at) AS occurred_at,
                 error
          FROM pipeline_runs
          WHERE status = 'failed'
          UNION ALL
          SELECT 'job' AS source,
                 COALESCE(completed_at, retry_at, created_at) AS occurred_at,
                 last_error AS error
          FROM job_work_items
          WHERE state IN ('failed', 'terminal_error')
        ) failures
        ORDER BY occurred_at DESC
        LIMIT %s
        """,
        (failure_limit,),
    ).fetchall()
    failures = tuple(_parse_failure(row) for row in failure_rows)

    return OperationsSnapshot(
        health=operations_health(queues, recent_runs),
        queues=queues,
        spend=spend,
        recent_runs=recent_runs,
        failures=failures,
    )


def _parse_run(row: tuple[object, ...]) -> PipelineRunSummary:
    run_id, kind, status, started_at, completed_at = row
    if not isinstance(run_id, UUID):
        raise RuntimeError("Pipeline run id is invalid")
    if not isinstance(kind, str):
        raise RuntimeError("Pipeline run kind is invalid")
    if status not in {"running", "completed", "failed"}:
        raise RuntimeError("Pipeline run status is invalid")
    if not isinstance(started_at, datetime):
        raise RuntimeError("Pipeline run start time is invalid")
    if completed_at is not None and not isinstance(completed_at, datetime):
        raise RuntimeError("Pipeline run completion time is invalid")
    return PipelineRunSummary(
        id=run_id,
        kind=kind,
        status=cast(PipelineRunStatus, status),
        started_at=started_at,
        completed_at=completed_at,
    )


def _parse_failure(row: tuple[object, ...]) -> FailureSample:
    source, occurred_at, raw_error = row
    if source not in {"pipeline", "job"}:
        raise RuntimeError("Failure source is invalid")
    if not isinstance(occurred_at, datetime):
        raise RuntimeError("Failure time is invalid")
    return FailureSample(
        source=cast(FailureSource, source),
        occurred_at=occurred_at,
        summary=_error_summary(raw_error),
    )


def _error_summary(raw_error: object) -> str:
    if not isinstance(raw_error, dict):
        return "Failure details unavailable"
    error = cast(Mapping[str, object], raw_error)
    code = error.get("code")
    reason = error.get("reason")
    if isinstance(code, str) and isinstance(reason, str):
        return f"{code}: {reason}"
    if isinstance(reason, str):
        return reason
    if isinstance(code, str):
        return code
    return "Failure details unavailable"
