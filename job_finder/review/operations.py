from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal, TypeAlias, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from psycopg.types.json import Jsonb

from job_finder.evaluation.models import PromptReleaseId, RelevanceReleaseId
from job_finder.review.postgres import Connection, ConnectionFactory

PipelineRunStatus = Literal["running", "completed", "failed"]
FailureSource = Literal["pipeline", "job"]
WorkItemState = Literal["pending", "leased", "failed", "completed", "terminal_error"]
ActionableWorkState = Literal["failed", "terminal_error"]
RecoveryOutcome = Literal["applied", "stale_state", "active_lease", "not_found"]
ReevaluationOutcome = Literal[
    "accepted", "not_found", "source_changed", "active_work", "unsupported"
]


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


class RecoveryAction(StrEnum):
    RETRY_NOW = "retry_now"
    RECOVER_TERMINAL = "recover_terminal"


class DismissalAction(StrEnum):
    DISMISS = "dismiss"
    UNDO_DISMISS = "undo_dismiss"


@dataclass(frozen=True)
class WorkDismissalCommand:
    idempotency_key: str
    job_id: UUID
    action: DismissalAction
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


@dataclass(frozen=True)
class WorkDismissalReceipt:
    idempotency_key: str
    job_id: UUID
    action: DismissalAction
    expected_attempt_count: int
    actor: str
    requested_at: datetime
    outcome: RecoveryOutcome
    prior_state: WorkItemState | None
    prior_attempt_count: int | None
    resulting_attempt_count: int | None


@dataclass(frozen=True)
class WorkDismissalApplied:
    receipt: WorkDismissalReceipt
    replayed: bool


@dataclass(frozen=True)
class WorkDismissalStaleState:
    receipt: WorkDismissalReceipt
    replayed: bool


@dataclass(frozen=True)
class WorkDismissalActiveLease:
    receipt: WorkDismissalReceipt
    replayed: bool


@dataclass(frozen=True)
class WorkDismissalNotFound:
    receipt: WorkDismissalReceipt
    replayed: bool


@dataclass(frozen=True)
class WorkDismissalKeyConflict:
    idempotency_key: str


WorkDismissalResult: TypeAlias = (
    WorkDismissalApplied
    | WorkDismissalStaleState
    | WorkDismissalActiveLease
    | WorkDismissalNotFound
    | WorkDismissalKeyConflict
)


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


@dataclass(frozen=True)
class JobReevaluationCommand:
    idempotency_key: str
    expected_decision_id: str
    expected_snapshot_id: str
    actor: str
    requested_at: datetime

    def __post_init__(self) -> None:
        if not self.idempotency_key or len(self.idempotency_key) > 200:
            raise ValueError("idempotency key must contain 1 to 200 characters")
        if not self.actor or len(self.actor) > 200:
            raise ValueError("actor must contain 1 to 200 characters")
        for name, value in (
            ("decision", self.expected_decision_id),
            ("snapshot", self.expected_snapshot_id),
        ):
            if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                raise ValueError(f"expected {name} id must be a lowercase SHA-256 digest")


@dataclass(frozen=True)
class JobReevaluationReceipt:
    idempotency_key: str
    expected_decision_id: str
    expected_snapshot_id: str
    actor: str
    requested_at: datetime
    outcome: ReevaluationOutcome
    job_id: UUID | None = None
    source_decision_id: str | None = None
    source_snapshot_id: str | None = None
    source_pipeline_run_id: UUID | None = None
    reevaluation_pipeline_run_id: UUID | None = None
    prompt_release_id: PromptReleaseId | None = None
    relevance_release_id: RelevanceReleaseId | None = None
    release_generation: int | None = None
    observed_work_state: WorkItemState | None = None
    conflict_code: str | None = None
    conflict_reason: str | None = None


@dataclass(frozen=True)
class JobReevaluationAccepted:
    receipt: JobReevaluationReceipt
    replayed: bool


@dataclass(frozen=True)
class JobReevaluationNotFound:
    receipt: JobReevaluationReceipt
    replayed: bool


@dataclass(frozen=True)
class JobReevaluationSourceChanged:
    receipt: JobReevaluationReceipt
    replayed: bool


@dataclass(frozen=True)
class JobReevaluationActiveWork:
    receipt: JobReevaluationReceipt
    replayed: bool


@dataclass(frozen=True)
class JobReevaluationUnsupported:
    receipt: JobReevaluationReceipt
    replayed: bool


@dataclass(frozen=True)
class JobReevaluationKeyConflict:
    idempotency_key: str


JobReevaluationResult: TypeAlias = (
    JobReevaluationAccepted
    | JobReevaluationNotFound
    | JobReevaluationSourceChanged
    | JobReevaluationActiveWork
    | JobReevaluationUnsupported
    | JobReevaluationKeyConflict
)


class OperationsUnavailable(RuntimeError):
    pass


def _unavailable_recovery(_command: WorkRecoveryCommand) -> WorkRecoveryResult:
    raise OperationsUnavailable("Work recovery is unavailable")


def _unavailable_reevaluation(_command: JobReevaluationCommand) -> JobReevaluationResult:
    raise OperationsUnavailable("Job reevaluation is unavailable")


def _unavailable_dismissal(_command: WorkDismissalCommand) -> WorkDismissalResult:
    raise OperationsUnavailable("Work dismissal is unavailable")


@dataclass(frozen=True)
class OperationsService:
    load: Callable[[], OperationsSnapshot]
    recover: Callable[[WorkRecoveryCommand], WorkRecoveryResult] = _unavailable_recovery
    reevaluate: Callable[[JobReevaluationCommand], JobReevaluationResult] = (
        _unavailable_reevaluation
    )
    dismiss: Callable[[WorkDismissalCommand], WorkDismissalResult] = _unavailable_dismissal


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

    def reevaluate(command: JobReevaluationCommand) -> JobReevaluationResult:
        with connect() as connection:
            return request_job_reevaluation(connection, command)

    def dismiss(command: WorkDismissalCommand) -> WorkDismissalResult:
        with connect() as connection:
            return dismiss_work(connection, command)

    return OperationsService(load=load, recover=recover, reevaluate=reevaluate, dismiss=dismiss)


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


def dismiss_work(connection: Connection, command: WorkDismissalCommand) -> WorkDismissalResult:
    if not connection.autocommit:
        raise ValueError("Work dismissal requires an autocommit connection")
    with connection.transaction():
        _ = connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"work_dismissal:{command.idempotency_key}",),
        ).fetchone()
        existing = _load_dismissal_receipt(connection, command.idempotency_key)
        if existing is not None:
            if not _dismissal_receipt_matches(existing, command):
                return WorkDismissalKeyConflict(command.idempotency_key)
            return _dismissal_result(existing, replayed=True)

        row = connection.execute(
            """
            SELECT state, attempt_count,
                   COALESCE(lease_expires_at > %s, false)
            FROM job_work_items
            WHERE job_id = %s
            FOR UPDATE
            """,
            (command.requested_at, command.job_id),
        ).fetchone()
        prior = _parse_dismissal_prior(row)
        outcome: RecoveryOutcome
        resulting_attempt_count: int | None
        if prior is None:
            outcome = "not_found"
            resulting_attempt_count = None
        elif command.action is DismissalAction.DISMISS and prior[0] == "leased" and prior[2]:
            outcome = "active_lease"
            resulting_attempt_count = prior[1]
        elif prior[0] != "terminal_error" or prior[1] != command.expected_attempt_count:
            outcome = "stale_state"
            resulting_attempt_count = prior[1]
        elif command.action is DismissalAction.DISMISS:
            outcome = "applied"
            resulting_attempt_count = prior[1]
            _ = connection.execute(
                """
                INSERT INTO work_dismissals (job_id, attempt_count, actor, dismissed_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (job_id) DO UPDATE
                SET attempt_count = EXCLUDED.attempt_count,
                    actor = EXCLUDED.actor,
                    dismissed_at = EXCLUDED.dismissed_at
                """,
                (command.job_id, prior[1], command.actor, command.requested_at),
            )
        else:
            dismissed_row = connection.execute(
                "SELECT attempt_count FROM work_dismissals WHERE job_id = %s",
                (command.job_id,),
            ).fetchone()
            if dismissed_row is None or int(str(dismissed_row[0])) != prior[1]:
                outcome = "not_found" if dismissed_row is None else "stale_state"
                resulting_attempt_count = prior[1]
            else:
                outcome = "applied"
                resulting_attempt_count = prior[1]
                _ = connection.execute(
                    "DELETE FROM work_dismissals WHERE job_id = %s", (command.job_id,)
                )

        receipt = WorkDismissalReceipt(
            idempotency_key=command.idempotency_key,
            job_id=command.job_id,
            action=command.action,
            expected_attempt_count=command.expected_attempt_count,
            actor=command.actor,
            requested_at=command.requested_at,
            outcome=outcome,
            prior_state=None if prior is None else prior[0],
            prior_attempt_count=None if prior is None else prior[1],
            resulting_attempt_count=resulting_attempt_count,
        )
        _insert_dismissal_receipt(connection, receipt)
        return _dismissal_result(receipt, replayed=False)


DismissalPrior: TypeAlias = tuple[WorkItemState, int, bool]


def _parse_dismissal_prior(row: tuple[object, ...] | None) -> DismissalPrior | None:
    if row is None:
        return None
    state, attempt_count, active_lease = row
    if state not in {"pending", "leased", "failed", "completed", "terminal_error"}:
        raise RuntimeError("Work state is invalid")
    if not isinstance(attempt_count, int):
        raise RuntimeError("Work attempt count is invalid")
    if not isinstance(active_lease, bool):
        raise RuntimeError("Work lease state is invalid")
    return (cast(WorkItemState, state), attempt_count, active_lease)


def _insert_dismissal_receipt(connection: Connection, receipt: WorkDismissalReceipt) -> None:
    _ = connection.execute(
        """
        INSERT INTO work_dismissal_receipts (
          idempotency_key, job_id, action, expected_attempt_count,
          actor, requested_at, outcome,
          prior_state, prior_attempt_count, resulting_attempt_count
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            receipt.idempotency_key,
            receipt.job_id,
            receipt.action.value,
            receipt.expected_attempt_count,
            receipt.actor,
            receipt.requested_at,
            receipt.outcome,
            receipt.prior_state,
            receipt.prior_attempt_count,
            receipt.resulting_attempt_count,
        ),
    )


def _load_dismissal_receipt(
    connection: Connection, idempotency_key: str
) -> WorkDismissalReceipt | None:
    row = connection.execute(
        """
        SELECT idempotency_key, job_id, action, expected_attempt_count,
               actor, requested_at, outcome,
               prior_state, prior_attempt_count, resulting_attempt_count
        FROM work_dismissal_receipts
        WHERE idempotency_key = %s
        """,
        (idempotency_key,),
    ).fetchone()
    if row is None:
        return None
    if not isinstance(row[1], UUID) or not isinstance(row[5], datetime):
        raise RuntimeError("Work dismissal receipt identity is invalid")
    return WorkDismissalReceipt(
        idempotency_key=str(row[0]),
        job_id=row[1],
        action=DismissalAction(str(row[2])),
        expected_attempt_count=int(str(row[3])),
        actor=str(row[4]),
        requested_at=row[5],
        outcome=cast(RecoveryOutcome, row[6]),
        prior_state=cast(WorkItemState | None, row[7]),
        prior_attempt_count=None if row[8] is None else int(str(row[8])),
        resulting_attempt_count=None if row[9] is None else int(str(row[9])),
    )


def _dismissal_receipt_matches(
    receipt: WorkDismissalReceipt, command: WorkDismissalCommand
) -> bool:
    return (
        receipt.job_id == command.job_id
        and receipt.action is command.action
        and receipt.expected_attempt_count == command.expected_attempt_count
        and receipt.actor == command.actor
    )


def _dismissal_result(receipt: WorkDismissalReceipt, *, replayed: bool) -> WorkDismissalResult:
    if receipt.outcome == "applied":
        return WorkDismissalApplied(receipt, replayed)
    if receipt.outcome == "stale_state":
        return WorkDismissalStaleState(receipt, replayed)
    if receipt.outcome == "active_lease":
        return WorkDismissalActiveLease(receipt, replayed)
    if receipt.outcome == "not_found":
        return WorkDismissalNotFound(receipt, replayed)
    raise RuntimeError("Work dismissal receipt outcome is invalid")


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


def request_job_reevaluation(
    connection: Connection, command: JobReevaluationCommand
) -> JobReevaluationResult:
    if not connection.autocommit:
        raise ValueError("Job reevaluation requires an autocommit connection")
    with connection.transaction():
        _ = connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"job_reevaluation_key:{command.idempotency_key}",),
        ).fetchone()
        existing = _load_reevaluation_receipt(connection, command.idempotency_key)
        if existing is not None:
            if not _reevaluation_receipt_matches(existing, command):
                return JobReevaluationKeyConflict(command.idempotency_key)
            return _reevaluation_result(existing, replayed=True)

        _ = connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"job_reevaluation_source:{command.expected_decision_id}",),
        ).fetchone()
        _ = connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"job_reevaluation_snapshot:{command.expected_snapshot_id}",),
        ).fetchone()
        source = connection.execute(
            """
            SELECT s.job_id, d.snapshot_id, d.pipeline_run_id,
                   source_run.kind, source_run.configuration_revision_id,
                   rates.content_digest, rates.rates, rates.source, rates.observed_at,
                   EXISTS (
                     SELECT 1 FROM snapshot_corrections correction
                     WHERE correction.snapshot_id = s.id
                   )
            FROM evaluation_decisions d
            JOIN job_snapshots s ON s.id = d.snapshot_id
            JOIN pipeline_runs source_run ON source_run.id = d.pipeline_run_id
            LEFT JOIN run_exchange_rate_snapshots rates
              ON rates.pipeline_run_id = source_run.id
            WHERE d.id = %s
            """,
            (command.expected_decision_id,),
        ).fetchone()
        if source is None:
            return _record_reevaluation_conflict(
                connection,
                command,
                outcome="not_found",
                code="decision_not_found",
                reason="The source decision does not exist.",
            )

        job_id = UUID(str(source[0]))
        source_snapshot_id = str(source[1])
        source_run_id = UUID(str(source[2]))
        if source_snapshot_id != command.expected_snapshot_id:
            return _record_reevaluation_conflict(
                connection,
                command,
                outcome="source_changed",
                code="snapshot_changed",
                reason="The source decision no longer matches the requested snapshot.",
                job_id=job_id,
            )

        work = connection.execute(
            """
            SELECT state, terminal_decision_id
            FROM job_work_items
            WHERE job_id = %s
            FOR UPDATE
            """,
            (job_id,),
        ).fetchone()
        if work is None:
            return _record_reevaluation_conflict(
                connection,
                command,
                outcome="unsupported",
                code="work_item_missing",
                reason="This decision has no processable work item.",
                job_id=job_id,
            )
        observed_state = cast(WorkItemState, str(work[0]))
        if observed_state != "completed":
            return _record_reevaluation_conflict(
                connection,
                command,
                outcome="active_work",
                code="work_not_completed",
                reason="This job already has active or unresolved work.",
                job_id=job_id,
                observed_work_state=observed_state,
            )
        if str(work[1]) != command.expected_decision_id:
            return _record_reevaluation_conflict(
                connection,
                command,
                outcome="source_changed",
                code="decision_changed",
                reason="A newer terminal decision replaced the requested source.",
                job_id=job_id,
                observed_work_state=observed_state,
            )
        if bool(source[9]):
            return _record_reevaluation_conflict(
                connection,
                command,
                outcome="unsupported",
                code="corrected_snapshot",
                reason="Corrected snapshots cannot yet be reevaluated immutably.",
                job_id=job_id,
                observed_work_state=observed_state,
            )
        if source[3] not in {"orchestration", "reevaluation"} or any(
            value is None for value in source[4:9]
        ):
            return _record_reevaluation_conflict(
                connection,
                command,
                outcome="unsupported",
                code="incomplete_provenance",
                reason="The source decision lacks complete pinned run provenance.",
                job_id=job_id,
                observed_work_state=observed_state,
            )

        active = connection.execute(
            """
            SELECT prompt_release_id, relevance_release_id, generation
            FROM active_release_target
            WHERE singleton_id = 1
            FOR SHARE
            """
        ).fetchone()
        if active is None:
            raise RuntimeError("Active release target is missing")
        prompt_release_id = PromptReleaseId(str(active[0]))
        relevance_release_id = RelevanceReleaseId(str(active[1]))
        release_generation = int(str(active[2]))
        reevaluation_run_id = uuid5(
            NAMESPACE_URL, f"job-reevaluation-run:{command.idempotency_key}"
        )
        _ = connection.execute(
            """
            INSERT INTO pipeline_runs (
              id, idempotency_key, kind, implementation_ref,
              configuration_revision_id, prompt_release_id, relevance_release_id,
              parameters, status, started_at
            ) VALUES (%s, %s, 'reevaluation', 'owner-reevaluation-v1', %s, %s, %s,
              %s, 'running', %s)
            """,
            (
                reevaluation_run_id,
                f"job-reevaluation:{command.idempotency_key}",
                source[4],
                prompt_release_id,
                relevance_release_id,
                Jsonb(
                    {
                        "source_decision_id": command.expected_decision_id,
                        "source_snapshot_id": command.expected_snapshot_id,
                        "requested_by": command.actor,
                    }
                ),
                command.requested_at,
            ),
        )
        _ = connection.execute(
            """
            INSERT INTO run_exchange_rate_snapshots (
              pipeline_run_id, content_digest, rates, source, observed_at
            ) VALUES (%s, %s, %s, %s, %s)
            """,
            (reevaluation_run_id, source[5], Jsonb(source[6]), source[7], source[8]),
        )
        receipt = JobReevaluationReceipt(
            idempotency_key=command.idempotency_key,
            expected_decision_id=command.expected_decision_id,
            expected_snapshot_id=command.expected_snapshot_id,
            actor=command.actor,
            requested_at=command.requested_at,
            outcome="accepted",
            job_id=job_id,
            source_decision_id=command.expected_decision_id,
            source_snapshot_id=command.expected_snapshot_id,
            source_pipeline_run_id=source_run_id,
            reevaluation_pipeline_run_id=reevaluation_run_id,
            prompt_release_id=prompt_release_id,
            relevance_release_id=relevance_release_id,
            release_generation=release_generation,
            observed_work_state="completed",
        )
        _insert_reevaluation_receipt(connection, receipt)
        changed = connection.execute(
            """
            UPDATE job_work_items
            SET state = 'pending', attempt_count = 0, owner_token = NULL,
                lease_expires_at = NULL, retry_at = NULL,
                terminal_decision_id = NULL, last_error = NULL,
                last_failed_at = NULL, completed_at = NULL,
                active_reevaluation_key = %s
            WHERE job_id = %s AND state = 'completed'
              AND terminal_decision_id = %s
            """,
            (command.idempotency_key, job_id, command.expected_decision_id),
        ).rowcount
        if changed != 1:
            raise RuntimeError("Locked work item changed during reevaluation request")
        return JobReevaluationAccepted(receipt, replayed=False)


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
        failure_summary=_error_summary(raw_error),
        dismissed=dismissed,
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
    if command.action is RecoveryAction.RECOVER_TERMINAL:
        _ = connection.execute("DELETE FROM work_dismissals WHERE job_id = %s", (command.job_id,))
        _ = connection.execute(
            """
            UPDATE pipeline_runs run
            SET status = 'running', completed_at = NULL, error = NULL
            FROM job_work_items item
            JOIN job_reevaluation_requests request
              ON request.idempotency_key = item.active_reevaluation_key
            WHERE item.job_id = %s
              AND run.id = request.reevaluation_pipeline_run_id
              AND run.kind = 'reevaluation'
              AND run.status = 'failed'
            """,
            (command.job_id,),
        )


def _record_reevaluation_conflict(
    connection: Connection,
    command: JobReevaluationCommand,
    *,
    outcome: Literal["not_found", "source_changed", "active_work", "unsupported"],
    code: str,
    reason: str,
    job_id: UUID | None = None,
    observed_work_state: WorkItemState | None = None,
) -> JobReevaluationResult:
    receipt = JobReevaluationReceipt(
        idempotency_key=command.idempotency_key,
        expected_decision_id=command.expected_decision_id,
        expected_snapshot_id=command.expected_snapshot_id,
        actor=command.actor,
        requested_at=command.requested_at,
        outcome=outcome,
        job_id=job_id,
        observed_work_state=observed_work_state,
        conflict_code=code,
        conflict_reason=reason,
    )
    _insert_reevaluation_receipt(connection, receipt)
    return _reevaluation_result(receipt, replayed=False)


def _insert_reevaluation_receipt(connection: Connection, receipt: JobReevaluationReceipt) -> None:
    _ = connection.execute(
        """
        INSERT INTO job_reevaluation_requests (
          idempotency_key, expected_decision_id, expected_snapshot_id,
          actor, requested_at, outcome, job_id,
          source_decision_id, source_snapshot_id, source_pipeline_run_id,
          reevaluation_pipeline_run_id, prompt_release_id, relevance_release_id,
          release_generation, observed_work_state, conflict_code, conflict_reason
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            receipt.idempotency_key,
            receipt.expected_decision_id,
            receipt.expected_snapshot_id,
            receipt.actor,
            receipt.requested_at,
            receipt.outcome,
            receipt.job_id,
            receipt.source_decision_id,
            receipt.source_snapshot_id,
            receipt.source_pipeline_run_id,
            receipt.reevaluation_pipeline_run_id,
            receipt.prompt_release_id,
            receipt.relevance_release_id,
            receipt.release_generation,
            receipt.observed_work_state,
            receipt.conflict_code,
            receipt.conflict_reason,
        ),
    )


def _load_reevaluation_receipt(
    connection: Connection, idempotency_key: str
) -> JobReevaluationReceipt | None:
    row = connection.execute(
        """
        SELECT idempotency_key, expected_decision_id, expected_snapshot_id,
               actor, requested_at, outcome, job_id,
               source_decision_id, source_snapshot_id, source_pipeline_run_id,
               reevaluation_pipeline_run_id, prompt_release_id, relevance_release_id,
               release_generation, observed_work_state, conflict_code, conflict_reason
        FROM job_reevaluation_requests
        WHERE idempotency_key = %s
        """,
        (idempotency_key,),
    ).fetchone()
    if row is None:
        return None
    if not isinstance(row[4], datetime):
        raise RuntimeError("Job reevaluation receipt time is invalid")
    return JobReevaluationReceipt(
        idempotency_key=str(row[0]),
        expected_decision_id=str(row[1]),
        expected_snapshot_id=str(row[2]),
        actor=str(row[3]),
        requested_at=row[4],
        outcome=cast(ReevaluationOutcome, row[5]),
        job_id=None if row[6] is None else UUID(str(row[6])),
        source_decision_id=None if row[7] is None else str(row[7]),
        source_snapshot_id=None if row[8] is None else str(row[8]),
        source_pipeline_run_id=None if row[9] is None else UUID(str(row[9])),
        reevaluation_pipeline_run_id=None if row[10] is None else UUID(str(row[10])),
        prompt_release_id=None if row[11] is None else PromptReleaseId(str(row[11])),
        relevance_release_id=(None if row[12] is None else RelevanceReleaseId(str(row[12]))),
        release_generation=None if row[13] is None else int(str(row[13])),
        observed_work_state=cast(WorkItemState | None, row[14]),
        conflict_code=None if row[15] is None else str(row[15]),
        conflict_reason=None if row[16] is None else str(row[16]),
    )


def _reevaluation_receipt_matches(
    receipt: JobReevaluationReceipt, command: JobReevaluationCommand
) -> bool:
    return (
        receipt.expected_decision_id == command.expected_decision_id
        and receipt.expected_snapshot_id == command.expected_snapshot_id
        and receipt.actor == command.actor
    )


def _reevaluation_result(
    receipt: JobReevaluationReceipt, *, replayed: bool
) -> JobReevaluationResult:
    if receipt.outcome == "accepted":
        return JobReevaluationAccepted(receipt, replayed)
    if receipt.outcome == "not_found":
        return JobReevaluationNotFound(receipt, replayed)
    if receipt.outcome == "source_changed":
        return JobReevaluationSourceChanged(receipt, replayed)
    if receipt.outcome == "active_work":
        return JobReevaluationActiveWork(receipt, replayed)
    if receipt.outcome == "unsupported":
        return JobReevaluationUnsupported(receipt, replayed)
    raise RuntimeError("Job reevaluation receipt outcome is invalid")


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
