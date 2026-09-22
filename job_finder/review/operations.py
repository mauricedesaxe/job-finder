from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal, TypeAlias, cast
from uuid import UUID

from psycopg.types.json import Jsonb

from job_finder.review.postgres import Connection, ConnectionFactory

PipelineRunStatus = Literal["running", "completed", "failed"]
FailureSource = Literal["pipeline", "job"]
WorkItemState = Literal["pending", "leased", "failed", "completed", "terminal_error"]
ActionableWorkState = Literal["failed", "terminal_error"]
RecoveryOutcome = Literal["applied", "stale_state", "active_lease", "not_found"]


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

    def __post_init__(self) -> None:
        if self.health is not operations_health(self.queues, self.recent_runs):
            raise ValueError("health does not match the operations evidence")
        if self.actionable_work_total < len(self.actionable_work):
            raise ValueError("actionable work total cannot be smaller than the bounded items")


class RecoveryAction(StrEnum):
    RETRY_NOW = "retry_now"
    RECOVER_TERMINAL = "recover_terminal"


@dataclass(frozen=True)
class WorkRecoveryCommand:
    idempotency_key: str
    job_id: UUID
    action: RecoveryAction
    expected_state: ActionableWorkState
    expected_attempt_count: int
    actor: str
    requested_at: datetime

    def __post_init__(self) -> None:
        if not self.idempotency_key or len(self.idempotency_key) > 200:
            raise ValueError("idempotency key must contain 1 to 200 characters")
        if not self.actor or len(self.actor) > 200:
            raise ValueError("actor must contain 1 to 200 characters")
        if self.expected_attempt_count < 0:
            raise ValueError("expected attempt count cannot be negative")
        expected_action = {
            "failed": RecoveryAction.RETRY_NOW,
            "terminal_error": RecoveryAction.RECOVER_TERMINAL,
        }[self.expected_state]
        if self.action is not expected_action:
            raise ValueError("recovery action does not match expected state")


@dataclass(frozen=True)
class WorkRecoveryReceipt:
    idempotency_key: str
    job_id: UUID
    action: RecoveryAction
    expected_state: ActionableWorkState
    expected_attempt_count: int
    actor: str
    requested_at: datetime
    outcome: RecoveryOutcome
    prior_state: WorkItemState | None
    prior_attempt_count: int | None
    prior_retry_at: datetime | None
    prior_failed_at: datetime | None
    prior_error: Mapping[str, object] | None
    resulting_state: WorkItemState | None
    resulting_attempt_count: int | None
    resulting_retry_at: datetime | None


@dataclass(frozen=True)
class WorkRecoveryApplied:
    receipt: WorkRecoveryReceipt
    replayed: bool


@dataclass(frozen=True)
class WorkRecoveryStaleState:
    receipt: WorkRecoveryReceipt
    replayed: bool


@dataclass(frozen=True)
class WorkRecoveryActiveLease:
    receipt: WorkRecoveryReceipt
    replayed: bool


@dataclass(frozen=True)
class WorkRecoveryNotFound:
    receipt: WorkRecoveryReceipt
    replayed: bool


@dataclass(frozen=True)
class WorkRecoveryKeyConflict:
    idempotency_key: str


WorkRecoveryResult: TypeAlias = (
    WorkRecoveryApplied
    | WorkRecoveryStaleState
    | WorkRecoveryActiveLease
    | WorkRecoveryNotFound
    | WorkRecoveryKeyConflict
)


class OperationsUnavailable(RuntimeError):
    pass


def _unavailable_recovery(_command: WorkRecoveryCommand) -> WorkRecoveryResult:
    raise OperationsUnavailable("Work recovery is unavailable")


@dataclass(frozen=True)
class OperationsService:
    load: Callable[[], OperationsSnapshot]
    recover: Callable[[WorkRecoveryCommand], WorkRecoveryResult] = _unavailable_recovery


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
        actionable_work_total=0,
    )
    return OperationsService(load=lambda: snapshot)


def postgres_operations_service(connect: ConnectionFactory) -> OperationsService:
    def load() -> OperationsSnapshot:
        with connect() as connection:
            return load_operations_snapshot(connection)

    def recover(command: WorkRecoveryCommand) -> WorkRecoveryResult:
        with connect() as connection:
            return recover_work(connection, command)

    return OperationsService(load=load, recover=recover)


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
        SELECT job_id, state, attempt_count, retry_at,
               COALESCE(last_failed_at, completed_at, created_at) AS failed_at,
               last_error
        FROM job_work_items
        WHERE state IN ('failed', 'terminal_error')
        ORDER BY failed_at DESC, job_id
        LIMIT %s
        """,
        (actionable_work_limit,),
    ).fetchall()
    actionable_work = tuple(_parse_actionable_work(row) for row in actionable_rows)
    actionable_work_total = queues.retrying + queues.terminal_error

    return OperationsSnapshot(
        health=operations_health(queues, recent_runs),
        queues=queues,
        spend=spend,
        recent_runs=recent_runs,
        failures=failures,
        actionable_work=actionable_work,
        actionable_work_total=actionable_work_total,
    )


def recover_work(connection: Connection, command: WorkRecoveryCommand) -> WorkRecoveryResult:
    if not connection.autocommit:
        raise ValueError("Work recovery requires an autocommit connection")
    with connection.transaction():
        _ = connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"work_recovery:{command.idempotency_key}",),
        ).fetchone()
        existing = _load_recovery_receipt(connection, command.idempotency_key)
        if existing is not None:
            if not _receipt_matches_command(existing, command):
                return WorkRecoveryKeyConflict(command.idempotency_key)
            return _recovery_result(existing, replayed=True)

        row = connection.execute(
            """
            SELECT state, attempt_count, retry_at,
                   COALESCE(last_failed_at, completed_at, created_at), last_error,
                   COALESCE(lease_expires_at > %s, false)
            FROM job_work_items
            WHERE job_id = %s
            FOR UPDATE
            """,
            (command.requested_at, command.job_id),
        ).fetchone()
        prior = _parse_recovery_prior(row)
        outcome: RecoveryOutcome
        resulting_state: WorkItemState | None
        resulting_attempt_count: int | None
        resulting_retry_at: datetime | None
        if prior is None:
            outcome = "not_found"
            resulting_state = None
            resulting_attempt_count = None
            resulting_retry_at = None
        elif prior[0] == "leased" and prior[5]:
            outcome = "active_lease"
            resulting_state, resulting_attempt_count, resulting_retry_at = prior[:3]
        elif prior[0] != command.expected_state or prior[1] != command.expected_attempt_count:
            outcome = "stale_state"
            resulting_state, resulting_attempt_count, resulting_retry_at = prior[:3]
        elif command.action is RecoveryAction.RETRY_NOW:
            outcome = "applied"
            resulting_state = "failed"
            resulting_attempt_count = prior[1]
            resulting_retry_at = command.requested_at
        else:
            outcome = "applied"
            resulting_state = "pending"
            resulting_attempt_count = 0
            resulting_retry_at = None

        receipt = WorkRecoveryReceipt(
            idempotency_key=command.idempotency_key,
            job_id=command.job_id,
            action=command.action,
            expected_state=command.expected_state,
            expected_attempt_count=command.expected_attempt_count,
            actor=command.actor,
            requested_at=command.requested_at,
            outcome=outcome,
            prior_state=None if prior is None else prior[0],
            prior_attempt_count=None if prior is None else prior[1],
            prior_retry_at=None if prior is None else prior[2],
            prior_failed_at=None if prior is None else prior[3],
            prior_error=None if prior is None else prior[4],
            resulting_state=resulting_state,
            resulting_attempt_count=resulting_attempt_count,
            resulting_retry_at=resulting_retry_at,
        )
        _insert_recovery_receipt(connection, receipt)
        if outcome == "applied":
            _apply_recovery(connection, command)
        return _recovery_result(receipt, replayed=False)


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


def _parse_actionable_work(row: tuple[object, ...]) -> ActionableWork:
    job_id, state, attempt_count, retry_at, failed_at, raw_error = row
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
    return ActionableWork(
        job_id=job_id,
        state=cast(ActionableWorkState, state),
        attempt_count=attempt_count,
        retry_at=retry_at,
        failed_at=failed_at,
        failure_summary=_error_summary(raw_error),
    )


RecoveryPrior: TypeAlias = tuple[
    WorkItemState,
    int,
    datetime | None,
    datetime,
    Mapping[str, object] | None,
    bool,
]


def _parse_recovery_prior(row: tuple[object, ...] | None) -> RecoveryPrior | None:
    if row is None:
        return None
    state, attempt_count, retry_at, failed_at, raw_error, active_lease = row
    if state not in {"pending", "leased", "failed", "completed", "terminal_error"}:
        raise RuntimeError("Work state is invalid")
    if not isinstance(attempt_count, int):
        raise RuntimeError("Work attempt count is invalid")
    if retry_at is not None and not isinstance(retry_at, datetime):
        raise RuntimeError("Work retry time is invalid")
    if not isinstance(failed_at, datetime):
        raise RuntimeError("Work failure time is invalid")
    if raw_error is not None and not isinstance(raw_error, dict):
        raise RuntimeError("Work error is invalid")
    if not isinstance(active_lease, bool):
        raise RuntimeError("Work lease state is invalid")
    return (
        cast(WorkItemState, state),
        attempt_count,
        retry_at,
        failed_at,
        None if raw_error is None else cast(Mapping[str, object], raw_error),
        active_lease,
    )


def _insert_recovery_receipt(connection: Connection, receipt: WorkRecoveryReceipt) -> None:
    _ = connection.execute(
        """
        INSERT INTO work_recovery_receipts (
          idempotency_key, job_id, action, expected_state, expected_attempt_count,
          actor, requested_at,
          outcome, prior_state, prior_attempt_count, prior_retry_at,
          prior_failed_at, prior_error, resulting_state,
          resulting_attempt_count, resulting_retry_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            receipt.idempotency_key,
            receipt.job_id,
            receipt.action.value,
            receipt.expected_state,
            receipt.expected_attempt_count,
            receipt.actor,
            receipt.requested_at,
            receipt.outcome,
            receipt.prior_state,
            receipt.prior_attempt_count,
            receipt.prior_retry_at,
            receipt.prior_failed_at,
            None if receipt.prior_error is None else Jsonb(dict(receipt.prior_error)),
            receipt.resulting_state,
            receipt.resulting_attempt_count,
            receipt.resulting_retry_at,
        ),
    )


def _load_recovery_receipt(
    connection: Connection, idempotency_key: str
) -> WorkRecoveryReceipt | None:
    row = connection.execute(
        """
        SELECT idempotency_key, job_id, action, expected_state, expected_attempt_count,
               actor, requested_at,
               outcome, prior_state, prior_attempt_count, prior_retry_at,
               prior_failed_at, prior_error, resulting_state,
               resulting_attempt_count, resulting_retry_at
        FROM work_recovery_receipts
        WHERE idempotency_key = %s
        """,
        (idempotency_key,),
    ).fetchone()
    if row is None:
        return None
    if not isinstance(row[1], UUID) or not isinstance(row[6], datetime):
        raise RuntimeError("Work recovery receipt identity is invalid")
    raw_error = row[12]
    if raw_error is not None and not isinstance(raw_error, dict):
        raise RuntimeError("Work recovery receipt error is invalid")
    return WorkRecoveryReceipt(
        idempotency_key=str(row[0]),
        job_id=row[1],
        action=RecoveryAction(str(row[2])),
        expected_state=cast(ActionableWorkState, row[3]),
        expected_attempt_count=int(str(row[4])),
        actor=str(row[5]),
        requested_at=row[6],
        outcome=cast(RecoveryOutcome, row[7]),
        prior_state=cast(WorkItemState | None, row[8]),
        prior_attempt_count=None if row[9] is None else int(str(row[9])),
        prior_retry_at=cast(datetime | None, row[10]),
        prior_failed_at=cast(datetime | None, row[11]),
        prior_error=None if raw_error is None else cast(Mapping[str, object], raw_error),
        resulting_state=cast(WorkItemState | None, row[13]),
        resulting_attempt_count=None if row[14] is None else int(str(row[14])),
        resulting_retry_at=cast(datetime | None, row[15]),
    )


def _receipt_matches_command(receipt: WorkRecoveryReceipt, command: WorkRecoveryCommand) -> bool:
    return (
        receipt.job_id == command.job_id
        and receipt.action is command.action
        and receipt.expected_state == command.expected_state
        and receipt.expected_attempt_count == command.expected_attempt_count
        and receipt.actor == command.actor
    )


def _recovery_result(receipt: WorkRecoveryReceipt, *, replayed: bool) -> WorkRecoveryResult:
    if receipt.outcome == "applied":
        return WorkRecoveryApplied(receipt, replayed)
    if receipt.outcome == "stale_state":
        return WorkRecoveryStaleState(receipt, replayed)
    if receipt.outcome == "active_lease":
        return WorkRecoveryActiveLease(receipt, replayed)
    if receipt.outcome == "not_found":
        return WorkRecoveryNotFound(receipt, replayed)
    raise RuntimeError("Work recovery receipt outcome is invalid")


def _apply_recovery(connection: Connection, command: WorkRecoveryCommand) -> None:
    if command.action is RecoveryAction.RETRY_NOW:
        changed = connection.execute(
            """
            UPDATE job_work_items
            SET retry_at = %s
            WHERE job_id = %s AND state = 'failed'
              AND attempt_count = %s
              AND owner_token IS NULL AND lease_expires_at IS NULL
            """,
            (command.requested_at, command.job_id, command.expected_attempt_count),
        ).rowcount
    else:
        changed = connection.execute(
            """
            UPDATE job_work_items
            SET state = 'pending', attempt_count = 0, owner_token = NULL,
                lease_expires_at = NULL, retry_at = NULL,
                terminal_decision_id = NULL, last_error = NULL, completed_at = NULL
            WHERE job_id = %s AND state = 'terminal_error'
              AND attempt_count = %s
              AND owner_token IS NULL AND lease_expires_at IS NULL
            """,
            (command.job_id, command.expected_attempt_count),
        ).rowcount
    if changed != 1:
        raise RuntimeError("Locked work item changed during recovery")


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
