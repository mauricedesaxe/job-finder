from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, ClassVar, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from job_finder.discovery.exchange_rates import ExchangeRateSnapshot
from job_finder.evaluation.models import (
    InputDigest,
    ModelCallContext,
    OperationalError,
    PromptReleaseId,
    RetryableOperationalError,
)
from job_finder.evaluation.prompt_releases import PromptRelease
from job_finder.jobs.decision_pipeline import job_id_for_url

Connection = psycopg.Connection[tuple[object, ...]]
RateSnapshotFactory = Callable[[], ExchangeRateSnapshot]
PromptReleaseLoader = Callable[[Connection], PromptRelease]
_RATES = TypeAdapter(dict[str, Decimal])


class PipelineStateModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class OrchestrationRun(PipelineStateModel):
    id: UUID
    idempotency_key: str
    implementation_ref: str
    prompt_release_id: Annotated[PromptReleaseId, Field(pattern=r"^[0-9a-f]{64}$")]
    exchange_rates: ExchangeRateSnapshot
    status: Literal["running", "completed", "failed"]
    started_at: datetime
    completed_at: datetime | None


class DiscoveryRegistration(PipelineStateModel):
    discovered_count: int = Field(ge=0)
    new_work_count: int = Field(ge=0)


class JobWorkClaim(PipelineStateModel):
    job_id: UUID
    raw_url: str
    keyword: str
    owner_token: UUID
    attempt_count: int = Field(gt=0)
    lease_expires_at: datetime


def prepare_orchestration_run(
    connection: Connection,
    *,
    idempotency_key: str,
    implementation_ref: str,
    started_at: datetime,
    load_prompt_release: PromptReleaseLoader,
    fetch_rates: RateSnapshotFactory,
) -> OrchestrationRun:
    _require_autocommit(connection)
    existing = load_orchestration_run(connection, idempotency_key)
    if existing is not None:
        if existing.implementation_ref != implementation_ref:
            raise ValueError("Run idempotency key belongs to another implementation")
        if existing.status == "failed":
            with connection.transaction():
                _ = connection.execute(
                    """
                    UPDATE pipeline_runs
                    SET status = 'running', completed_at = NULL, error = NULL
                    WHERE id = %s AND status = 'failed'
                    """,
                    (existing.id,),
                )
            return _load_run_by_id(connection, existing.id)
        return existing
    release = load_prompt_release(connection)
    rates = fetch_rates()
    run_id = uuid5(NAMESPACE_URL, f"orchestration-run:{idempotency_key}")
    rate_data = {currency: str(value) for currency, value in sorted(rates.rates.items())}
    rate_digest = _digest(rate_data)
    with connection.transaction():
        inserted = connection.execute(
            """
            INSERT INTO pipeline_runs (
              id, idempotency_key, kind, implementation_ref, prompt_release_id,
              parameters, status, started_at
            ) VALUES (%s, %s, 'orchestration', %s, %s, '{}'::jsonb, 'running', %s)
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING id
            """,
            (run_id, idempotency_key, implementation_ref, release.id, started_at),
        ).fetchone()
        if inserted is not None:
            _ = connection.execute(
                """
                INSERT INTO run_exchange_rate_snapshots (
                  pipeline_run_id, content_digest, rates, source, observed_at
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (run_id, rate_digest, Jsonb(rate_data), rates.source, rates.observed_at),
            )
    stored = load_orchestration_run(connection, idempotency_key)
    if stored is None:
        raise RuntimeError("Orchestration run could not be loaded after creation")
    if stored.implementation_ref != implementation_ref:
        raise ValueError("Run idempotency key belongs to another implementation")
    return stored


def load_orchestration_run(connection: Connection, idempotency_key: str) -> OrchestrationRun | None:
    row = connection.execute(
        "SELECT id FROM pipeline_runs WHERE idempotency_key = %s AND kind = 'orchestration'",
        (idempotency_key,),
    ).fetchone()
    return None if row is None else _load_run_by_id(connection, UUID(str(row[0])))


def complete_orchestration_run(
    connection: Connection, run_id: UUID, *, completed_at: datetime
) -> None:
    _require_autocommit(connection)
    with connection.transaction():
        _ = connection.execute(
            """
            UPDATE pipeline_runs
            SET status = 'completed', completed_at = %s, error = NULL
            WHERE id = %s AND status = 'running'
            """,
            (completed_at, run_id),
        )


def fail_orchestration_run(
    connection: Connection,
    run_id: UUID,
    *,
    completed_at: datetime,
    error_code: str,
    reason: str,
) -> None:
    _require_autocommit(connection)
    with connection.transaction():
        _ = connection.execute(
            """
            UPDATE pipeline_runs
            SET status = 'failed', completed_at = %s, error = %s
            WHERE id = %s AND status = 'running'
            """,
            (completed_at, Jsonb({"code": error_code, "reason": reason}), run_id),
        )


def register_discoveries(
    connection: Connection,
    *,
    run_id: UUID,
    keyword: str,
    domain: str,
    raw_urls: tuple[str, ...],
    discovered_at: datetime,
) -> DiscoveryRegistration:
    _require_autocommit(connection)
    discovered_count = 0
    new_work_count = 0
    with connection.transaction():
        for raw_url in raw_urls:
            job_id = job_id_for_url(raw_url)
            inserted_job = connection.execute(
                """
                INSERT INTO jobs (id, raw_url, first_discovered_at, last_discovered_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (raw_url) DO NOTHING
                RETURNING id
                """,
                (job_id, raw_url, discovered_at, discovered_at),
            ).fetchone()
            job_row = connection.execute(
                """
                UPDATE jobs
                SET last_discovered_at = GREATEST(last_discovered_at, %s)
                WHERE raw_url = %s
                RETURNING id
                """,
                (discovered_at, raw_url),
            ).fetchone()
            if job_row is None:
                raise RuntimeError("Registered job could not be loaded")
            job_id = UUID(str(job_row[0]))
            inserted_discovery = connection.execute(
                """
                INSERT INTO job_discoveries (
                  pipeline_run_id, job_id, keyword, domain, discovered_at
                ) VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING job_id
                """,
                (run_id, job_id, keyword, domain, discovered_at),
            ).fetchone()
            if inserted_discovery is not None:
                discovered_count += 1
            if inserted_job is not None:
                inserted_work = connection.execute(
                    """
                    INSERT INTO job_work_items (
                      job_id, discovery_run_id, keyword, state, created_at
                    ) VALUES (%s, %s, %s, 'pending', %s)
                    RETURNING job_id
                    """,
                    (job_id, run_id, keyword, discovered_at),
                ).fetchone()
                if inserted_work is not None:
                    new_work_count += 1
    return DiscoveryRegistration(
        discovered_count=discovered_count,
        new_work_count=new_work_count,
    )


def claim_next_job(
    connection: Connection,
    *,
    owner_token: UUID,
    claimed_at: datetime,
    lease_for: timedelta,
) -> JobWorkClaim | None:
    _require_autocommit(connection)
    if lease_for <= timedelta(0):
        raise ValueError("Job claim lease must be positive")
    with connection.transaction():
        row = connection.execute(
            """
            WITH candidate AS (
              SELECT job_id
              FROM job_work_items
              WHERE state = 'pending'
                 OR (state = 'failed' AND COALESCE(retry_at, created_at) <= %s)
                 OR (state = 'leased' AND lease_expires_at <= %s)
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
                      item.lease_expires_at
            """,
            (claimed_at, claimed_at, owner_token, claimed_at + lease_for),
        ).fetchone()
    if row is None:
        return None
    return JobWorkClaim.model_validate(
        {
            "job_id": row[0],
            "raw_url": row[1],
            "keyword": row[2],
            "owner_token": owner_token,
            "attempt_count": row[3],
            "lease_expires_at": row[4],
        }
    )


def complete_job_claim(
    connection: Connection,
    claim: JobWorkClaim,
    *,
    decision_id: str,
    completed_at: datetime,
) -> bool:
    _require_autocommit(connection)
    with connection.transaction():
        changed = connection.execute(
            """
            UPDATE job_work_items
            SET state = 'completed', owner_token = NULL, lease_expires_at = NULL,
                terminal_decision_id = %s, completed_at = %s, retry_at = NULL,
                last_error = NULL
            WHERE job_id = %s AND state = 'leased' AND owner_token = %s
            """,
            (decision_id, completed_at, claim.job_id, claim.owner_token),
        ).rowcount
    return changed == 1


def fail_job_claim(
    connection: Connection,
    claim: JobWorkClaim,
    *,
    failed_at: datetime,
    retry_after: timedelta,
    error_code: str,
    reason: str,
) -> bool:
    _require_autocommit(connection)
    if retry_after < timedelta(0):
        raise ValueError("Job retry delay cannot be negative")
    with connection.transaction():
        changed = connection.execute(
            """
            UPDATE job_work_items
            SET state = 'failed', owner_token = NULL, lease_expires_at = NULL,
                retry_at = %s, last_error = %s
            WHERE job_id = %s AND state = 'leased' AND owner_token = %s
            """,
            (
                failed_at + retry_after,
                Jsonb(
                    {
                        "code": error_code,
                        "reason": reason,
                        "retryability": "retryable",
                    }
                ),
                claim.job_id,
                claim.owner_token,
            ),
        ).rowcount
    return changed == 1


def terminally_fail_job_claim(
    connection: Connection,
    claim: JobWorkClaim,
    *,
    completed_at: datetime,
    error_code: str,
    reason: str,
) -> bool:
    _require_autocommit(connection)
    with connection.transaction():
        changed = connection.execute(
            """
            UPDATE job_work_items
            SET state = 'terminal_error', owner_token = NULL, lease_expires_at = NULL,
                retry_at = NULL, completed_at = %s, terminal_decision_id = NULL,
                last_error = %s
            WHERE job_id = %s AND state = 'leased' AND owner_token = %s
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
                claim.job_id,
                claim.owner_token,
            ),
        ).rowcount
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
    _require_autocommit(connection)
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
    _require_autocommit(connection)
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
    _require_autocommit(connection)
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


def _load_run_by_id(connection: Connection, run_id: UUID) -> OrchestrationRun:
    row = connection.execute(
        """
        SELECT r.idempotency_key, r.implementation_ref, r.prompt_release_id,
               r.status, r.started_at, r.completed_at,
               x.rates, x.source, x.observed_at
        FROM pipeline_runs r
        JOIN run_exchange_rate_snapshots x ON x.pipeline_run_id = r.id
        WHERE r.id = %s AND r.kind = 'orchestration'
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("Orchestration run is incomplete")
    return OrchestrationRun.model_validate(
        {
            "id": run_id,
            "idempotency_key": row[0],
            "implementation_ref": row[1],
            "prompt_release_id": row[2],
            "status": row[3],
            "started_at": row[4],
            "completed_at": row[5],
            "exchange_rates": {
                "rates": _RATES.validate_python(row[6]),
                "source": row[7],
                "observed_at": row[8],
            },
        }
    )


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _require_autocommit(connection: Connection) -> None:
    if not connection.autocommit:
        raise ValueError("Pipeline state operations require an autocommit connection")
