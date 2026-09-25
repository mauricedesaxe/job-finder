from __future__ import annotations

from datetime import datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from psycopg.types.json import Jsonb

from job_finder.evaluation.models import (
    InputDigest,
    ModelCallContext,
    OperationalError,
    PromptReleaseId,
    RetryableOperationalError,
)
from job_finder.pipeline.connection import Connection, require_autocommit


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
