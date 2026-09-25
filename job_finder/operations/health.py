from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import cast
from uuid import UUID

from job_finder.database import Connection
from job_finder.operations._common import (
    ActionableWorkState,
    FailureSource,
    PipelineRunStatus,
    error_summary,
)


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
class ActionableWork:
    job_id: UUID
    state: ActionableWorkState
    attempt_count: int
    retry_at: datetime | None
    failed_at: datetime
    failure_summary: str
    dismissed: bool = False

    def __post_init__(self) -> None:
        if self.attempt_count < 0:
            raise ValueError("work attempt count cannot be negative")
        if (self.state == "failed") != (self.retry_at is not None):
            raise ValueError("actionable work retry time does not match its state")


@dataclass(frozen=True)
class OperationsSnapshot:
    health: OperationsHealth
    queues: QueueCounts
    spend: SpendSummary
    recent_runs: tuple[PipelineRunSummary, ...]
    failures: tuple[FailureSample, ...]
    actionable_work: tuple[ActionableWork, ...] = ()
    actionable_work_total: int = 0
    dismissed_terminal: int = 0

    def __post_init__(self) -> None:
        if self.health is not operations_health(
            self.queues, self.recent_runs, self.dismissed_terminal
        ):
            raise ValueError("health does not match the operations evidence")
        if self.actionable_work_total < len(self.actionable_work):
            raise ValueError("actionable work total cannot be smaller than the bounded items")
        if self.dismissed_terminal > self.queues.terminal_error:
            raise ValueError("dismissed terminal work cannot exceed the terminal queue")


def operations_health(
    queues: QueueCounts,
    recent_runs: tuple[PipelineRunSummary, ...],
    dismissed_terminal: int = 0,
) -> OperationsHealth:
    if queues.terminal_error > dismissed_terminal:
        return OperationsHealth.ACTION_REQUIRED
    if recent_runs and recent_runs[0].status == "failed":
        return OperationsHealth.ACTION_REQUIRED
    if queues.active > 0:
        return OperationsHealth.WORKING
    if not recent_runs:
        return OperationsHealth.CAUGHT_UP if dismissed_terminal else OperationsHealth.UNKNOWN
    newest = recent_runs[0]
    if newest.status == "running":
        return OperationsHealth.WORKING
    return OperationsHealth.CAUGHT_UP


def load_operations_snapshot(
    connection: Connection,
    *,
    recent_run_limit: int = 10,
    failure_limit: int = 8,
    actionable_work_limit: int = 20,
) -> OperationsSnapshot:
    if recent_run_limit < 1:
        raise ValueError("recent run limit must be positive")
    if failure_limit < 0:
        raise ValueError("failure limit cannot be negative")
    if actionable_work_limit < 0:
        raise ValueError("actionable work limit cannot be negative")

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

    actionable_rows = connection.execute(
        """
        SELECT item.job_id, item.state, item.attempt_count, item.retry_at,
               COALESCE(item.last_failed_at, item.completed_at, item.created_at) AS failed_at,
               item.last_error,
               dismissal.job_id IS NOT NULL AS dismissed
        FROM job_work_items item
        LEFT JOIN work_dismissals dismissal
          ON dismissal.job_id = item.job_id
         AND dismissal.attempt_count = item.attempt_count
        WHERE item.state IN ('failed', 'terminal_error')
        ORDER BY failed_at DESC, item.job_id
        LIMIT %s
        """,
        (actionable_work_limit,),
    ).fetchall()
    actionable_work = tuple(_parse_actionable_work(row) for row in actionable_rows)
    actionable_work_total = queues.retrying + queues.terminal_error

    dismissed_row = connection.execute(
        """
        SELECT count(*)
        FROM job_work_items item
        JOIN work_dismissals dismissal
          ON dismissal.job_id = item.job_id
         AND dismissal.attempt_count = item.attempt_count
        WHERE item.state = 'terminal_error'
        """
    ).fetchone()
    dismissed_terminal = int(str((dismissed_row or (0,))[0]))

    return OperationsSnapshot(
        health=operations_health(queues, recent_runs, dismissed_terminal),
        queues=queues,
        spend=spend,
        recent_runs=recent_runs,
        failures=failures,
        actionable_work=actionable_work,
        actionable_work_total=actionable_work_total,
        dismissed_terminal=dismissed_terminal,
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
        summary=error_summary(raw_error),
    )


def _parse_actionable_work(row: tuple[object, ...]) -> ActionableWork:
    job_id, state, attempt_count, retry_at, failed_at, raw_error, dismissed = row
    if not isinstance(job_id, UUID):
        raise RuntimeError("Actionable work job id is invalid")
    if state not in {"failed", "terminal_error"}:
        raise RuntimeError("Actionable work state is invalid")
    if not isinstance(attempt_count, int):
        raise RuntimeError("Actionable work attempt count is invalid")
    if retry_at is not None and not isinstance(retry_at, datetime):
        raise RuntimeError("Actionable work retry time is invalid")
    if not isinstance(failed_at, datetime):
        raise RuntimeError("Actionable work failure time is invalid")
    if not isinstance(dismissed, bool):
        raise RuntimeError("Actionable work dismissal flag is invalid")
    return ActionableWork(
        job_id=job_id,
        state=cast(ActionableWorkState, state),
        attempt_count=attempt_count,
        retry_at=retry_at,
        failed_at=failed_at,
        failure_summary=error_summary(raw_error),
        dismissed=dismissed,
    )
