from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, TypeAlias, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from psycopg.types.json import Jsonb

from job_finder.database import Connection
from job_finder.evaluation.models import PromptReleaseId, RelevanceReleaseId

WorkItemState = Literal["pending", "leased", "failed", "completed", "terminal_error"]
ReevaluationOutcome = Literal[
    "accepted", "not_found", "source_changed", "active_work", "unsupported"
]


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
