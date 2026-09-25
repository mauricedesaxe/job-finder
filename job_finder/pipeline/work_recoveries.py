from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal, TypeAlias, cast
from uuid import UUID

from psycopg.types.json import Jsonb

from job_finder.database import Connection
from job_finder.pipeline.work_dismissals import clear_work_dismissal

WorkItemState = Literal["pending", "leased", "failed", "completed", "terminal_error"]
ActionableWorkState = Literal["failed", "terminal_error"]
RecoveryOutcome = Literal["applied", "stale_state", "active_lease", "not_found"]


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
        clear_work_dismissal(connection, command.job_id)
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
