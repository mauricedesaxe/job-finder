from __future__ import annotations

from datetime import datetime, timedelta
from typing import ClassVar, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from job_finder.evaluation.models import (
    InputDigest,
    ModelCallContext,
    OperationalError,
    PromptReleaseId,
    RetryableOperationalError,
)
from job_finder.pipeline.connection import Connection, require_autocommit
from job_finder.pipeline.runs import fail_reevaluation_for_request, fail_reevaluation_run

JobWorkFailureOutcome = Literal["retry", "terminal_error", "lease_lost"]
JOB_WORK_ATTEMPT_LIMIT = 3


class PipelineStateModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class JobWorkClaim(PipelineStateModel):
    job_id: UUID
    raw_url: str
    keyword: str
    owner_token: UUID
    attempt_count: int = Field(gt=0)
    attempt_limit: int = Field(default=JOB_WORK_ATTEMPT_LIMIT, gt=0)
    lease_expires_at: datetime
    reevaluation_request_key: str | None = None
    source_snapshot_id: str | None = None
    predecessor_decision_id: str | None = None
    reevaluation_pipeline_run_id: UUID | None = None


def claim_next_job(
    connection: Connection,
    *,
    owner_token: UUID,
    claimed_at: datetime,
    lease_for: timedelta,
    onboarding_request_key: str | None = None,
    attempt_limit: int = JOB_WORK_ATTEMPT_LIMIT,
) -> JobWorkClaim | None:
    require_autocommit(connection)
    if lease_for <= timedelta(0):
        raise ValueError("Job claim lease must be positive")
    if attempt_limit < 1:
        raise ValueError("Job attempt limit must be positive")
    with connection.transaction():
        exhausted_rows = connection.execute(
            """
            UPDATE job_work_items
            SET state = 'terminal_error', owner_token = NULL, lease_expires_at = NULL,
                retry_at = NULL, completed_at = %s, terminal_decision_id = NULL,
                last_failed_at = %s,
                last_error = CASE
                  WHEN state = 'failed' THEN last_error
                  ELSE %s
                END
            WHERE attempt_count >= %s
              AND onboarding_request_key IS NOT DISTINCT FROM %s
              AND (
                (state = 'failed' AND COALESCE(retry_at, created_at) <= %s)
                OR (state = 'leased' AND lease_expires_at <= %s)
              )
            RETURNING active_reevaluation_key
            """,
            (
                claimed_at,
                claimed_at,
                Jsonb(
                    {
                        "code": "lease_expired",
                        "reason": "Job work lease expired after the final attempt",
                        "retryability": "retryable",
                    }
                ),
                attempt_limit,
                onboarding_request_key,
                claimed_at,
                claimed_at,
            ),
        ).fetchall()
        for row in exhausted_rows:
            if row[0] is not None:
                fail_reevaluation_for_request(
                    connection,
                    str(row[0]),
                    claimed_at,
                    "lease_expired",
                    "Job work lease expired after the final attempt",
                )
        row = connection.execute(
            """
            WITH candidate AS (
              SELECT job_id
              FROM job_work_items
              WHERE attempt_count < %s
                AND onboarding_request_key IS NOT DISTINCT FROM %s
                AND (
                  state = 'pending'
                  OR (state = 'failed' AND COALESCE(retry_at, created_at) <= %s)
                  OR (state = 'leased' AND lease_expires_at <= %s)
                )
              ORDER BY created_at, job_id
              FOR UPDATE SKIP LOCKED
              LIMIT 1
            )
            UPDATE job_work_items item
            SET state = 'leased', owner_token = %s, lease_expires_at = %s,
                attempt_count = item.attempt_count + 1, retry_at = NULL,
                last_error = NULL
            FROM candidate, jobs
            WHERE item.job_id = candidate.job_id AND jobs.id = item.job_id
            RETURNING item.job_id, jobs.raw_url, item.keyword, item.attempt_count,
                      item.lease_expires_at, item.active_reevaluation_key
            """,
            (
                attempt_limit,
                onboarding_request_key,
                claimed_at,
                claimed_at,
                owner_token,
                claimed_at + lease_for,
            ),
        ).fetchone()
    if row is None:
        return None
    reevaluation = None
    if row[5] is not None:
        reevaluation = connection.execute(
            """
            SELECT source_snapshot_id, source_decision_id, reevaluation_pipeline_run_id
            FROM job_reevaluation_requests
            WHERE idempotency_key = %s AND outcome = 'accepted'
            """,
            (row[5],),
        ).fetchone()
        if reevaluation is None:
            raise RuntimeError("Claimed reevaluation work has no accepted request")
    return JobWorkClaim.model_validate(
        {
            "job_id": row[0],
            "raw_url": row[1],
            "keyword": row[2],
            "owner_token": owner_token,
            "attempt_count": row[3],
            "attempt_limit": attempt_limit,
            "lease_expires_at": row[4],
            "reevaluation_request_key": row[5],
            "source_snapshot_id": None if reevaluation is None else reevaluation[0],
            "predecessor_decision_id": None if reevaluation is None else reevaluation[1],
            "reevaluation_pipeline_run_id": None if reevaluation is None else reevaluation[2],
        }
    )


def complete_job_claim(
    connection: Connection,
    claim: JobWorkClaim,
    *,
    decision_id: str,
    completed_at: datetime,
) -> bool:
    require_autocommit(connection)
    with connection.transaction():
        changed = connection.execute(
            """
            UPDATE job_work_items
            SET state = 'completed', owner_token = NULL, lease_expires_at = NULL,
                terminal_decision_id = %s, completed_at = %s, retry_at = NULL,
                last_error = NULL
            WHERE job_id = %s AND state = 'leased' AND owner_token = %s
              AND lease_expires_at > %s
            """,
            (decision_id, completed_at, claim.job_id, claim.owner_token, completed_at),
        ).rowcount
        if changed == 1 and claim.reevaluation_pipeline_run_id is not None:
            _ = connection.execute(
                """
                UPDATE pipeline_runs
                SET status = 'completed', completed_at = %s, error = NULL
                WHERE id = %s AND kind = 'reevaluation' AND status = 'running'
                """,
                (completed_at, claim.reevaluation_pipeline_run_id),
            )
    return changed == 1


def fail_job_claim(
    connection: Connection,
    claim: JobWorkClaim,
    *,
    failed_at: datetime,
    retry_after: timedelta,
    error_code: str,
    reason: str,
) -> JobWorkFailureOutcome:
    require_autocommit(connection)
    if retry_after < timedelta(0):
        raise ValueError("Job retry delay cannot be negative")
    exhausted = claim.attempt_count >= claim.attempt_limit
    with connection.transaction():
        changed = connection.execute(
            """
            UPDATE job_work_items
            SET state = %s, owner_token = NULL, lease_expires_at = NULL,
                retry_at = %s, completed_at = %s, terminal_decision_id = NULL,
                last_error = %s, last_failed_at = %s
            WHERE job_id = %s AND state = 'leased' AND owner_token = %s
              AND lease_expires_at > %s
            """,
            (
                "terminal_error" if exhausted else "failed",
                None if exhausted else failed_at + retry_after,
                failed_at if exhausted else None,
                Jsonb(
                    {
                        "code": error_code,
                        "reason": reason,
                        "retryability": "retryable",
                    }
                ),
                failed_at,
                claim.job_id,
                claim.owner_token,
                failed_at,
            ),
        ).rowcount
        if changed == 1 and exhausted and claim.reevaluation_pipeline_run_id is not None:
            fail_reevaluation_run(
                connection,
                claim.reevaluation_pipeline_run_id,
                failed_at,
                error_code,
                reason,
            )
    if changed != 1:
        return "lease_lost"
    return "terminal_error" if exhausted else "retry"


def terminally_fail_job_claim(
    connection: Connection,
    claim: JobWorkClaim,
    *,
    completed_at: datetime,
    error_code: str,
    reason: str,
) -> bool:
    require_autocommit(connection)
    with connection.transaction():
        changed = connection.execute(
            """
            UPDATE job_work_items
            SET state = 'terminal_error', owner_token = NULL, lease_expires_at = NULL,
                retry_at = NULL, completed_at = %s, terminal_decision_id = NULL,
                last_error = %s, last_failed_at = %s
            WHERE job_id = %s AND state = 'leased' AND owner_token = %s
              AND lease_expires_at > %s
            """,
            (
                completed_at,
                Jsonb(
                    {
                        "code": error_code,
                        "reason": reason,
                        "retryability": "terminal",
                    }
                ),
                completed_at,
                claim.job_id,
                claim.owner_token,
                completed_at,
            ),
        ).rowcount
        if changed == 1 and claim.reevaluation_pipeline_run_id is not None:
            fail_reevaluation_run(
                connection,
                claim.reevaluation_pipeline_run_id,
                completed_at,
                error_code,
                reason,
            )
    return changed == 1


def find_terminal_decision_id(connection: Connection, job_id: UUID) -> str | None:
    row = connection.execute(
        """
        SELECT d.id
        FROM evaluation_decisions d
        JOIN job_snapshots s ON s.id = d.snapshot_id
        WHERE s.job_id = %s
        ORDER BY d.created_at DESC, d.id DESC
        LIMIT 1
        """,
        (job_id,),
    ).fetchone()
    return None if row is None else str(row[0])


def ensure_model_call_context(
    connection: Connection,
    *,
    run_id: UUID,
    job_id: UUID,
    operation_key: str,
    input_digest: InputDigest,
    started_at: datetime,
    prompt_release_id: PromptReleaseId,
) -> ModelCallContext:
    require_autocommit(connection)
    row = connection.execute(
        """
        SELECT id FROM processing_attempts
        WHERE pipeline_run_id = %s AND job_id = %s
          AND operation_key = %s AND input_digest = %s
        """,
        (run_id, job_id, operation_key, input_digest),
    ).fetchone()
    if row is None:
        attempt_id = uuid5(
            NAMESPACE_URL,
            f"model-operation:{run_id}:{job_id}:{operation_key}:{input_digest}",
        )
        with connection.transaction():
            _ = connection.execute(
                """
                INSERT INTO processing_attempts (
                  id, pipeline_run_id, job_id, operation_key, attempt_number,
                  input_digest, status, started_at
                ) VALUES (%s, %s, %s, %s, 0, %s, 'running', %s)
                ON CONFLICT (pipeline_run_id, job_id, operation_key, input_digest)
                DO NOTHING
                """,
                (attempt_id, run_id, job_id, operation_key, input_digest, started_at),
            )
        row = connection.execute(
            """
            SELECT id FROM processing_attempts
            WHERE pipeline_run_id = %s AND job_id = %s
              AND operation_key = %s AND input_digest = %s
            """,
            (run_id, job_id, operation_key, input_digest),
        ).fetchone()
    if row is None:
        raise RuntimeError("Could not create model-call processing attempt")
    with connection.transaction():
        _ = connection.execute(
            """
            UPDATE processing_attempts attempt
            SET status = 'running', completed_at = NULL, error = NULL
            WHERE attempt.id = %s
              AND attempt.status IN ('failed', 'completed')
              AND NOT EXISTS (
                SELECT 1 FROM model_call_attempts model_call
                WHERE model_call.processing_attempt_id = attempt.id
                  AND model_call.status = 'accepted'
              )
            """,
            (row[0],),
        )
    return ModelCallContext(
        processing_attempt_id=UUID(str(row[0])),
        pipeline_run_id=run_id,
        prompt_release_id=prompt_release_id,
        operation_key=operation_key,
        input_digest=input_digest,
    )


def complete_model_call_context(
    connection: Connection, context: ModelCallContext, *, completed_at: datetime
) -> None:
    require_autocommit(connection)
    with connection.transaction():
        _ = connection.execute(
            """
            UPDATE processing_attempts
            SET status = 'completed', completed_at = %s, error = NULL
            WHERE id = %s AND status = 'running'
            """,
            (completed_at, context.processing_attempt_id),
        )


def fail_model_call_context(
    connection: Connection,
    context: ModelCallContext,
    failure: OperationalError,
    *,
    completed_at: datetime,
) -> None:
    require_autocommit(connection)
    with connection.transaction():
        _ = connection.execute(
            """
            UPDATE processing_attempts
            SET status = 'failed', completed_at = %s, error = %s
            WHERE id = %s AND status = 'running'
            """,
            (
                completed_at,
                Jsonb(
                    {
                        "code": failure.error_code,
                        "reason": failure.reason,
                        "retryability": (
                            "retryable"
                            if isinstance(failure, RetryableOperationalError)
                            else "terminal"
                        ),
                    }
                ),
                context.processing_attempt_id,
            ),
        )
