from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, ClassVar, Literal, TypeAlias
from uuid import NAMESPACE_URL, UUID, uuid5

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from job_finder.acquisition_policy import AcquisitionPolicyRevisionId
from job_finder.discovery.exchange_rates import ExchangeRateSnapshot
from job_finder.evaluation.models import PromptReleaseId, ReleaseTarget, RelevanceReleaseId
from job_finder.evaluation.qualification_components import QualificationTargetId
from job_finder.evaluation.qualification_prompt_compilations import (
    load_compiled_qualification_target,
)
from job_finder.pipeline.connection import Connection, require_autocommit
from job_finder.search_configuration import SearchConfigurationRevisionId

RateSnapshotFactory = Callable[[], ExchangeRateSnapshot]
_RATES = TypeAdapter(dict[str, Decimal])


class PipelineRunModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class _RunFields(PipelineRunModel):
    id: UUID
    idempotency_key: str
    implementation_ref: str
    target: ReleaseTarget
    exchange_rates: ExchangeRateSnapshot
    status: Literal["running", "completed", "failed"]
    started_at: datetime
    completed_at: datetime | None

    @property
    def prompt_release_id(self) -> PromptReleaseId:
        return self.target.prompt_release_id


class LegacyOrchestrationRun(_RunFields):
    authority_kind: Literal["legacy"] = "legacy"
    configuration_revision_id: Annotated[
        SearchConfigurationRevisionId, Field(pattern=r"^[0-9a-f]{64}$")
    ]


class SplitOrchestrationRun(_RunFields):
    authority_kind: Literal["split"] = "split"
    acquisition_policy_revision_id: Annotated[
        AcquisitionPolicyRevisionId, Field(pattern=r"^[0-9a-f]{64}$")
    ]
    qualification_target_id: Annotated[QualificationTargetId, Field(pattern=r"^[0-9a-f]{64}$")]


OrchestrationRun: TypeAlias = LegacyOrchestrationRun | SplitOrchestrationRun


def prepare_orchestration_run(
    connection: Connection,
    *,
    idempotency_key: str,
    implementation_ref: str,
    configuration_revision_id: SearchConfigurationRevisionId,
    target: ReleaseTarget,
    started_at: datetime,
    fetch_rates: RateSnapshotFactory,
) -> LegacyOrchestrationRun:
    require_autocommit(connection)
    existing = load_orchestration_run(connection, idempotency_key)
    if existing is not None:
        if isinstance(existing, SplitOrchestrationRun):
            raise ValueError("Run idempotency key belongs to another execution authority")
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
            resumed = load_run_by_id(connection, existing.id)
            if isinstance(resumed, SplitOrchestrationRun):
                raise ValueError("Run idempotency key belongs to another execution authority")
            return resumed
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
    if isinstance(stored, SplitOrchestrationRun):
        raise ValueError("Run idempotency key belongs to another execution authority")
    if stored.configuration_revision_id != configuration_revision_id or stored.target != target:
        raise ValueError("Run idempotency key belongs to another execution authority")
    return stored


def prepare_split_orchestration_run(
    connection: Connection,
    *,
    idempotency_key: str,
    implementation_ref: str,
    acquisition_policy_revision_id: AcquisitionPolicyRevisionId,
    qualification_target_id: QualificationTargetId,
    artifact_path: Path,
    started_at: datetime,
    fetch_rates: RateSnapshotFactory,
) -> SplitOrchestrationRun:
    require_autocommit(connection)
    existing = load_orchestration_run(connection, idempotency_key)
    if existing is not None:
        if not isinstance(existing, SplitOrchestrationRun) or (
            existing.implementation_ref != implementation_ref
            or existing.acquisition_policy_revision_id != acquisition_policy_revision_id
            or existing.qualification_target_id != qualification_target_id
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
            resumed = load_run_by_id(connection, existing.id)
            if not isinstance(resumed, SplitOrchestrationRun):
                raise RuntimeError("Split run changed authority while resuming")
            return resumed
        return existing
    compiled = load_compiled_qualification_target(
        connection, qualification_target_id, artifact_path
    )
    target = ReleaseTarget(
        prompt_release_id=compiled.prompt_release.id,
        relevance_release_id=compiled.target.relevance.relevance_release_id,
    )
    rates = fetch_rates()
    run_id = uuid5(NAMESPACE_URL, f"orchestration-run:{idempotency_key}")
    rate_data = {currency: str(value) for currency, value in sorted(rates.rates.items())}
    with connection.transaction():
        inserted = connection.execute(
            """
            INSERT INTO pipeline_runs (
              id, idempotency_key, kind, implementation_ref,
              execution_authority_kind, acquisition_policy_revision_id,
              qualification_target_id, prompt_release_id, relevance_release_id,
              parameters, status, started_at
            ) VALUES (%s, %s, 'orchestration', %s, 'split', %s, %s, %s, %s,
                      '{}'::jsonb, 'running', %s)
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            (
                run_id,
                idempotency_key,
                implementation_ref,
                acquisition_policy_revision_id,
                qualification_target_id,
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
                (run_id, _digest(rate_data), Jsonb(rate_data), rates.source, rates.observed_at),
            )
    stored = load_orchestration_run(connection, idempotency_key)
    if not isinstance(stored, SplitOrchestrationRun) or (
        stored.implementation_ref != implementation_ref
        or stored.acquisition_policy_revision_id != acquisition_policy_revision_id
        or stored.qualification_target_id != qualification_target_id
    ):
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
) -> LegacyOrchestrationRun:
    require_autocommit(connection)
    existing = connection.execute(
        "SELECT id, kind, implementation_ref FROM pipeline_runs WHERE id = %s", (run_id,)
    ).fetchone()
    if existing is not None:
        if str(existing[1]) != "onboarding" or str(existing[2]) != implementation_ref:
            raise ValueError("Onboarding run identity belongs to another execution")
        stored = load_run_by_id(connection, run_id)
        if isinstance(stored, SplitOrchestrationRun):
            raise ValueError("Onboarding run provenance differs from its pinned request")
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
            resumed = load_run_by_id(connection, run_id)
            if isinstance(resumed, SplitOrchestrationRun):
                raise ValueError("Onboarding run provenance differs from its pinned request")
            return resumed
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
    stored = load_run_by_id(connection, run_id)
    if isinstance(stored, SplitOrchestrationRun):
        raise ValueError("Onboarding run provenance differs from its pinned request")
    return stored


def prepare_split_onboarding_run(
    connection: Connection,
    *,
    run_id: UUID,
    request_key: str,
    implementation_ref: str,
    acquisition_policy_revision_id: AcquisitionPolicyRevisionId,
    qualification_target_id: QualificationTargetId,
    artifact_path: Path,
    started_at: datetime,
    fetch_rates: RateSnapshotFactory,
) -> SplitOrchestrationRun:
    require_autocommit(connection)
    existing = connection.execute(
        "SELECT kind, implementation_ref FROM pipeline_runs WHERE id = %s", (run_id,)
    ).fetchone()
    if existing is not None:
        if str(existing[0]) != "onboarding" or str(existing[1]) != implementation_ref:
            raise ValueError("Onboarding run identity belongs to another execution")
        stored = load_run_by_id(connection, run_id)
        if not isinstance(stored, SplitOrchestrationRun) or (
            stored.idempotency_key != f"onboarding:{request_key}"
            or stored.implementation_ref != implementation_ref
            or stored.acquisition_policy_revision_id != acquisition_policy_revision_id
            or stored.qualification_target_id != qualification_target_id
        ):
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
            resumed = load_run_by_id(connection, run_id)
            if not isinstance(resumed, SplitOrchestrationRun):
                raise RuntimeError("Split onboarding run changed authority while resuming")
            return resumed
        return stored
    compiled = load_compiled_qualification_target(
        connection, qualification_target_id, artifact_path
    )
    target = ReleaseTarget(
        prompt_release_id=compiled.prompt_release.id,
        relevance_release_id=compiled.target.relevance.relevance_release_id,
    )
    rates = fetch_rates()
    rate_data = {currency: str(value) for currency, value in sorted(rates.rates.items())}
    with connection.transaction():
        _ = connection.execute(
            """
            INSERT INTO pipeline_runs (
              id, idempotency_key, kind, implementation_ref,
              execution_authority_kind, acquisition_policy_revision_id,
              qualification_target_id, prompt_release_id, relevance_release_id,
              parameters, status, started_at
            ) VALUES (%s, %s, 'onboarding', %s, 'split', %s, %s, %s, %s,
                      %s, 'running', %s)
            """,
            (
                run_id,
                f"onboarding:{request_key}",
                implementation_ref,
                acquisition_policy_revision_id,
                qualification_target_id,
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
    stored = load_run_by_id(connection, run_id)
    if not isinstance(stored, SplitOrchestrationRun) or (
        stored.idempotency_key != f"onboarding:{request_key}"
        or stored.implementation_ref != implementation_ref
        or stored.acquisition_policy_revision_id != acquisition_policy_revision_id
        or stored.qualification_target_id != qualification_target_id
    ):
        raise ValueError("Onboarding run provenance differs from its pinned request")
    return stored


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
               x.rates, x.source, x.observed_at,
               r.execution_authority_kind, r.acquisition_policy_revision_id,
               r.qualification_target_id
        FROM pipeline_runs r
        JOIN run_exchange_rate_snapshots x ON x.pipeline_run_id = r.id
            WHERE r.id = %s AND r.kind IN ('orchestration', 'reevaluation', 'onboarding')
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("Orchestration run is incomplete")
    if row[3] is None or row[4] is None:
        raise RuntimeError("Orchestration run has incomplete execution release provenance")
    common = {
        "id": run_id,
        "idempotency_key": row[0],
        "implementation_ref": row[1],
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
    if row[11] == "split":
        return SplitOrchestrationRun.model_validate(
            {
                **common,
                "acquisition_policy_revision_id": row[12],
                "qualification_target_id": row[13],
            }
        )
    if row[11] != "legacy":
        raise RuntimeError("Orchestration run has unknown execution authority")
    return LegacyOrchestrationRun.model_validate({**common, "configuration_revision_id": row[2]})


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
