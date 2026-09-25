from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Annotated, ClassVar, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from job_finder.discovery.exchange_rates import ExchangeRateSnapshot
from job_finder.evaluation.models import PromptReleaseId, ReleaseTarget, RelevanceReleaseId
from job_finder.pipeline.connection import Connection, require_autocommit
from job_finder.search_configuration import SearchConfigurationRevisionId

RateSnapshotFactory = Callable[[], ExchangeRateSnapshot]
_RATES = TypeAdapter(dict[str, Decimal])


class PipelineRunModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class OrchestrationRun(PipelineRunModel):
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


def prepare_orchestration_run(
    connection: Connection,
    *,
    idempotency_key: str,
    implementation_ref: str,
    configuration_revision_id: SearchConfigurationRevisionId,
    target: ReleaseTarget,
    started_at: datetime,
    fetch_rates: RateSnapshotFactory,
) -> OrchestrationRun:
    require_autocommit(connection)
    existing = load_orchestration_run(connection, idempotency_key)
    if existing is not None:
        if existing.implementation_ref != implementation_ref:
            raise ValueError("Run idempotency key belongs to another implementation")
        if (
            existing.configuration_revision_id != configuration_revision_id
            or existing.target != target
        ):
            raise ValueError("Run idempotency key belongs to another execution authority")
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
            return load_run_by_id(connection, existing.id)
        return existing
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
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            (
                run_id,
                idempotency_key,
                implementation_ref,
                configuration_revision_id,
                target.prompt_release_id,
                target.relevance_release_id,
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
    if stored.configuration_revision_id != configuration_revision_id or stored.target != target:
        raise ValueError("Run idempotency key belongs to another execution authority")
    return stored


def load_orchestration_run(connection: Connection, idempotency_key: str) -> OrchestrationRun | None:
    row = connection.execute(
        "SELECT id FROM pipeline_runs WHERE idempotency_key = %s AND kind = 'orchestration'",
        (idempotency_key,),
    ).fetchone()
    return None if row is None else load_run_by_id(connection, UUID(str(row[0])))


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
    require_autocommit(connection)
    existing = connection.execute(
        "SELECT id, kind, implementation_ref FROM pipeline_runs WHERE id = %s", (run_id,)
    ).fetchone()
    if existing is not None:
        if str(existing[1]) != "onboarding" or str(existing[2]) != implementation_ref:
            raise ValueError("Onboarding run identity belongs to another execution")
        stored = load_run_by_id(connection, run_id)
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
            return load_run_by_id(connection, run_id)
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
    return load_run_by_id(connection, run_id)


def complete_orchestration_run(
    connection: Connection, run_id: UUID, *, completed_at: datetime
) -> None:
    require_autocommit(connection)
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
    require_autocommit(connection)
    with connection.transaction():
        _ = connection.execute(
            """
            UPDATE pipeline_runs
            SET status = 'failed', completed_at = %s, error = %s
            WHERE id = %s AND status = 'running'
            """,
            (completed_at, Jsonb({"code": error_code, "reason": reason}), run_id),
        )


def load_run_by_id(connection: Connection, run_id: UUID) -> OrchestrationRun:
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


def fail_reevaluation_run(
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


def fail_reevaluation_for_request(
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
