from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal, TypeAlias, cast
from uuid import UUID

from job_finder.database import Connection

WorkItemState = Literal["pending", "leased", "failed", "completed", "terminal_error"]
RecoveryOutcome = Literal["applied", "stale_state", "active_lease", "not_found"]


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
                outcome = "not_found"
                resulting_attempt_count = None
                prior = None
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


def clear_work_dismissal(connection: Connection, job_id: UUID) -> None:
    _ = connection.execute("DELETE FROM work_dismissals WHERE job_id = %s", (job_id,))
