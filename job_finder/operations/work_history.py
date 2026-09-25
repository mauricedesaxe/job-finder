from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal, cast
from uuid import UUID

from job_finder.database import Connection
from job_finder.operations._common import OperationsUnavailable, WorkItemState, error_summary


class WorkItemNotFound(LookupError):
    pass


@dataclass(frozen=True)
class ModelCallDetail:
    prompt_name: str
    prompt_version_id: str
    requested_model: str
    status: str
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: Decimal | None
    latency_ms: int
    parsed_output: object | None
    request_messages: object
    raw_response: object | None
    error_summary: str | None


@dataclass(frozen=True)
class WorkAttemptSummary:
    operation_key: str
    attempt_number: int
    status: str
    started_at: datetime | None
    completed_at: datetime | None
    run_id: UUID | None
    model_calls: int
    known_cost_usd: Decimal
    error_summary: str | None
    calls: tuple[ModelCallDetail, ...] = ()


JobVerdictOutcome = Literal[
    "qualified", "rejected", "duplicate", "company_blocked", "company_applied"
]


@dataclass(frozen=True)
class JobVerdict:
    outcome: JobVerdictOutcome
    reason: str
    matched_profile: str | None
    decided_at: datetime


@dataclass(frozen=True)
class WorkItemDetail:
    job_id: UUID
    state: WorkItemState
    attempt_count: int
    created_at: datetime
    completed_at: datetime | None
    last_failed_at: datetime | None
    retry_at: datetime | None
    failure_summary: str | None
    discovery_run_id: UUID | None
    dismissed: bool
    dismissed_at: datetime | None
    dismissed_by: str | None
    attempts: tuple[WorkAttemptSummary, ...]
    verdict: JobVerdict | None = None


def unavailable_work_detail(_job_id: UUID) -> WorkItemDetail:
    raise OperationsUnavailable("Work item detail is unavailable")


def load_work_item_detail(connection: Connection, job_id: UUID) -> WorkItemDetail:
    row = connection.execute(
        """
        SELECT item.state, item.attempt_count, item.created_at, item.completed_at,
               item.last_failed_at, item.retry_at, item.last_error, item.discovery_run_id,
               dismissal.attempt_count, dismissal.actor, dismissal.dismissed_at
        FROM job_work_items item
        LEFT JOIN work_dismissals dismissal ON dismissal.job_id = item.job_id
        WHERE item.job_id = %s
        """,
        (job_id,),
    ).fetchone()
    if row is None:
        raise WorkItemNotFound("Work item does not exist")

    decision = connection.execute(
        """
        SELECT decision.outcome, decision.reason, decision.matched_profile,
               decision.created_at
        FROM evaluation_decisions decision
        JOIN job_snapshots snapshot ON snapshot.id = decision.snapshot_id
        WHERE snapshot.job_id = %s
        ORDER BY decision.created_at DESC, decision.id DESC
        LIMIT 1
        """,
        (job_id,),
    ).fetchone()
    verdict = None
    if decision is not None:
        outcome = str(decision[0])
        if outcome not in {
            "qualified",
            "rejected",
            "duplicate",
            "company_blocked",
            "company_applied",
        }:
            raise RuntimeError("Job verdict outcome is invalid")
        if not isinstance(decision[1], str) or not isinstance(decision[3], datetime):
            raise RuntimeError("Job verdict is incomplete")
        verdict = JobVerdict(
            outcome=cast(JobVerdictOutcome, outcome),
            reason=decision[1],
            matched_profile=cast(str | None, decision[2]),
            decided_at=decision[3],
        )

    state = str(row[0])
    if state not in {"pending", "leased", "failed", "completed", "terminal_error"}:
        raise RuntimeError("Work item state is invalid")
    attempt_count = int(str(row[1]))
    dismissal_active = row[8] is not None and int(str(row[8])) == attempt_count
    attempts = _load_work_attempts(connection, job_id)
    return WorkItemDetail(
        job_id=job_id,
        state=cast(WorkItemState, state),
        attempt_count=attempt_count,
        created_at=cast(datetime, row[2]),
        completed_at=cast(datetime | None, row[3]),
        last_failed_at=cast(datetime | None, row[4]),
        retry_at=cast(datetime | None, row[5]),
        failure_summary=None if row[6] is None else error_summary(row[6]),
        discovery_run_id=cast(UUID | None, row[7]),
        dismissed=dismissal_active,
        dismissed_at=cast(datetime | None, row[10]) if dismissal_active else None,
        dismissed_by=str(row[9]) if dismissal_active and row[9] is not None else None,
        attempts=attempts,
        verdict=verdict,
    )


def _load_work_attempts(connection: Connection, job_id: UUID) -> tuple[WorkAttemptSummary, ...]:
    attempt_rows = connection.execute(
        """
        SELECT pa.id, pa.operation_key, pa.attempt_number, pa.status,
               pa.started_at, pa.completed_at, pa.pipeline_run_id, pa.error
        FROM processing_attempts pa
        WHERE pa.job_id = %s
        ORDER BY pa.started_at DESC NULLS LAST, pa.operation_key, pa.attempt_number
        LIMIT 100
        """,
        (job_id,),
    ).fetchall()
    calls_by_attempt: dict[UUID, list[ModelCallDetail]] = {
        cast(UUID, attempt[0]): [] for attempt in attempt_rows
    }
    if calls_by_attempt:
        for call in connection.execute(
            """
            SELECT processing_attempt_id, prompt_name, prompt_version_id,
                   requested_model, status, input_tokens, output_tokens,
                   cost_usd, latency_ms, parsed_output, request_messages,
                   raw_response, error
            FROM model_call_attempts
            WHERE processing_attempt_id = ANY(%s)
            ORDER BY observed_at, attempt_number, id
            """,
            (list(calls_by_attempt),),
        ).fetchall():
            calls_by_attempt[cast(UUID, call[0])].append(_parse_model_call_detail(call))
    return tuple(
        WorkAttemptSummary(
            operation_key=str(item[1]),
            attempt_number=int(str(item[2])),
            status=str(item[3]),
            started_at=cast(datetime | None, item[4]),
            completed_at=cast(datetime | None, item[5]),
            run_id=cast(UUID | None, item[6]),
            model_calls=len(calls_by_attempt[cast(UUID, item[0])]),
            known_cost_usd=sum(
                (call.cost_usd or Decimal(0) for call in calls_by_attempt[cast(UUID, item[0])]),
                Decimal(0),
            ),
            error_summary=None if item[7] is None else error_summary(item[7]),
            calls=tuple(calls_by_attempt[cast(UUID, item[0])]),
        )
        for item in attempt_rows
    )


def _parse_model_call_detail(row: tuple[object, ...]) -> ModelCallDetail:
    return ModelCallDetail(
        prompt_name=str(row[1]),
        prompt_version_id=str(row[2]),
        requested_model=str(row[3]),
        status=str(row[4]),
        input_tokens=None if row[5] is None else int(str(row[5])),
        output_tokens=None if row[6] is None else int(str(row[6])),
        cost_usd=None if row[7] is None else Decimal(str(row[7])),
        latency_ms=int(str(row[8])),
        parsed_output=row[9],
        request_messages=row[10],
        raw_response=row[11],
        error_summary=None if row[12] is None else error_summary(row[12]),
    )
