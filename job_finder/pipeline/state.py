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

from job_finder.configuration_service import PublishedActiveSearchConfiguration
from job_finder.discovery.exchange_rates import ExchangeRateSnapshot
from job_finder.evaluation.models import (
    InputDigest,
    ModelCallContext,
    OperationalError,
    PromptReleaseId,
    ReleaseTarget,
    RelevanceReleaseId,
    RetryableOperationalError,
)
from job_finder.evaluation.release_targets import get_active_release_target
from job_finder.jobs.decision_pipeline import job_id_for_url
from job_finder.search_configuration import SearchConfigurationRevisionId

Connection = psycopg.Connection[tuple[object, ...]]
RateSnapshotFactory = Callable[[], ExchangeRateSnapshot]
ActiveConfigurationLoader = Callable[[Connection], PublishedActiveSearchConfiguration]
JobWorkFailureOutcome = Literal["retry", "terminal_error", "lease_lost"]
JOB_WORK_ATTEMPT_LIMIT = 3
_RATES = TypeAdapter(dict[str, Decimal])


class PipelineStateModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class OrchestrationRun(PipelineStateModel):
    id: UUID
    idempotency_key: str
    implementation_ref: str
    configuration_revision_id: Annotated[
        SearchConfigurationRevisionId, Field(pattern=r"^[0-9a-f]{64}$")
    ]
    target: ReleaseTarget
    exchange_rates: ExchangeRateSnapshot
    status: Literal["running", "completed", "failed"]
    started_at: datetime
    completed_at: datetime | None

    @property
    def prompt_release_id(self) -> PromptReleaseId:
        return self.target.prompt_release_id


class DiscoveryRegistration(PipelineStateModel):
    discovered_count: int = Field(ge=0)
    new_work_count: int = Field(ge=0)
    processed_url_count: int = Field(default=0, ge=0)


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


def prepare_orchestration_run(
    connection: Connection,
    *,
    idempotency_key: str,
    implementation_ref: str,
    started_at: datetime,
    load_active_configuration: ActiveConfigurationLoader,
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
    active_configuration = load_active_configuration(connection)
    active_target = get_active_release_target(connection)
    rates = fetch_rates()
    run_id = uuid5(NAMESPACE_URL, f"orchestration-run:{idempotency_key}")
    rate_data = {currency: str(value) for currency, value in sorted(rates.rates.items())}
    rate_digest = _digest(rate_data)
    with connection.transaction():
        inserted = connection.execute(
            """
            INSERT INTO pipeline_runs (
              id, idempotency_key, kind, implementation_ref,
              configuration_revision_id, prompt_release_id, relevance_release_id,
              parameters, status, started_at
            ) VALUES (%s, %s, 'orchestration', %s, %s, %s, %s, '{}'::jsonb, 'running', %s)
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING id
            """,
            (
                run_id,
                idempotency_key,
                implementation_ref,
                active_configuration.publication.revision_id,
                active_target.target.prompt_release_id,
                active_target.target.relevance_release_id,
                started_at,
            ),
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


def prepare_onboarding_run(
    connection: Connection,
    *,
    run_id: UUID,
    request_key: str,
    implementation_ref: str,
    configuration_revision_id: SearchConfigurationRevisionId,
    target: ReleaseTarget,
    started_at: datetime,
    fetch_rates: RateSnapshotFactory,
) -> OrchestrationRun:
    _require_autocommit(connection)
    existing = connection.execute(
        "SELECT id, kind, implementation_ref FROM pipeline_runs WHERE id = %s", (run_id,)
    ).fetchone()
    if existing is not None:
        if str(existing[1]) != "onboarding" or str(existing[2]) != implementation_ref:
            raise ValueError("Onboarding run identity belongs to another execution")
        stored = _load_run_by_id(connection, run_id)
        if stored.configuration_revision_id != configuration_revision_id or stored.target != target:
            raise ValueError("Onboarding run provenance differs from its pinned request")
        if stored.status == "failed":
            with connection.transaction():
                _ = connection.execute(
                    """
                    UPDATE pipeline_runs SET status = 'running', completed_at = NULL, error = NULL
                    WHERE id = %s AND status = 'failed'
                    """,
                    (run_id,),
                )
            return _load_run_by_id(connection, run_id)
        return stored
    rates = fetch_rates()
    rate_data = {currency: str(value) for currency, value in sorted(rates.rates.items())}
    with connection.transaction():
        _ = connection.execute(
            """
            INSERT INTO pipeline_runs (
              id, idempotency_key, kind, implementation_ref,
              configuration_revision_id, prompt_release_id, relevance_release_id,
              parameters, status, started_at
            ) VALUES (%s, %s, 'onboarding', %s, %s, %s, %s, %s, 'running', %s)
            """,
            (
                run_id,
                f"onboarding:{request_key}",
                implementation_ref,
                configuration_revision_id,
                target.prompt_release_id,
                target.relevance_release_id,
                Jsonb({"request_key": request_key}),
                started_at,
            ),
        )
        _ = connection.execute(
            """
            INSERT INTO run_exchange_rate_snapshots (
              pipeline_run_id, content_digest, rates, source, observed_at
            ) VALUES (%s, %s, %s, %s, %s)
            """,
            (run_id, _digest(rate_data), Jsonb(rate_data), rates.source, rates.observed_at),
        )
    return _load_run_by_id(connection, run_id)


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
    onboarding_request_key: str | None = None,
    max_new_work: int | None = None,
) -> DiscoveryRegistration:
    _require_autocommit(connection)
    if max_new_work is not None and max_new_work < 0:
        raise ValueError("Maximum new work must be nonnegative")
    discovered_count = 0
    new_work_count = 0
    processed_url_count = 0
    with connection.transaction():
        for raw_url in raw_urls:
            if max_new_work is not None and new_work_count >= max_new_work:
                break
            processed_url_count += 1
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
                      job_id, discovery_run_id, keyword, state, created_at,
                      onboarding_request_key
                    ) VALUES (%s, %s, %s, 'pending', %s, %s)
                    RETURNING job_id
                    """,
                    (job_id, run_id, keyword, discovered_at, onboarding_request_key),
                ).fetchone()
                if inserted_work is not None:
                    new_work_count += 1
    return DiscoveryRegistration(
        discovered_count=discovered_count,
        new_work_count=new_work_count,
        processed_url_count=processed_url_count,
    )


def claim_next_job(
    connection: Connection,
    *,
    owner_token: UUID,
    claimed_at: datetime,
    lease_for: timedelta,
    onboarding_request_key: str | None = None,
    attempt_limit: int = JOB_WORK_ATTEMPT_LIMIT,
) -> JobWorkClaim | None:
    _require_autocommit(connection)
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
                _fail_reevaluation_for_request(
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
    _require_autocommit(connection)
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
    _require_autocommit(connection)
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
            _fail_reevaluation_run(
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
    _require_autocommit(connection)
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
            _fail_reevaluation_run(
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


def load_processing_run(connection: Connection, run_id: UUID) -> OrchestrationRun:
    return _load_run_by_id(connection, run_id)


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
        SELECT r.idempotency_key, r.implementation_ref, r.configuration_revision_id,
               r.prompt_release_id, r.relevance_release_id,
               r.status, r.started_at, r.completed_at,
               x.rates, x.source, x.observed_at
        FROM pipeline_runs r
        JOIN run_exchange_rate_snapshots x ON x.pipeline_run_id = r.id
            WHERE r.id = %s AND r.kind IN ('orchestration', 'reevaluation', 'onboarding')
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("Orchestration run is incomplete")
    if row[4] is None:
        raise RuntimeError("Legacy orchestration run has unknown relevance provenance")
    return OrchestrationRun.model_validate(
        {
            "id": run_id,
            "idempotency_key": row[0],
            "implementation_ref": row[1],
            "configuration_revision_id": row[2],
            "target": {
                "prompt_release_id": PromptReleaseId(str(row[3])),
                "relevance_release_id": RelevanceReleaseId(str(row[4])),
            },
            "status": row[5],
            "started_at": row[6],
            "completed_at": row[7],
            "exchange_rates": {
                "rates": _RATES.validate_python(row[8]),
                "source": row[9],
                "observed_at": row[10],
            },
        }
    )


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _require_autocommit(connection: Connection) -> None:
    if not connection.autocommit:
        raise ValueError("Pipeline state operations require an autocommit connection")


def _fail_reevaluation_run(
    connection: Connection,
    run_id: UUID,
    completed_at: datetime,
    error_code: str,
    reason: str,
) -> None:
    _ = connection.execute(
        """
        UPDATE pipeline_runs
        SET status = 'failed', completed_at = %s, error = %s
        WHERE id = %s AND kind = 'reevaluation' AND status = 'running'
        """,
        (completed_at, Jsonb({"code": error_code, "reason": reason}), run_id),
    )


def _fail_reevaluation_for_request(
    connection: Connection,
    request_key: str,
    completed_at: datetime,
    error_code: str,
    reason: str,
) -> None:
    _ = connection.execute(
        """
        UPDATE pipeline_runs run
        SET status = 'failed', completed_at = %s, error = %s
        FROM job_reevaluation_requests request
        WHERE request.idempotency_key = %s
          AND run.id = request.reevaluation_pipeline_run_id
          AND run.kind = 'reevaluation'
          AND run.status = 'running'
        """,
        (
            completed_at,
            Jsonb({"code": error_code, "reason": reason}),
            request_key,
        ),
    )
