from __future__ import annotations

from datetime import datetime
from typing import Annotated, ClassVar, Literal

import psycopg
from pydantic import BaseModel, ConfigDict, Field

from job_finder.acquisition_policy import AcquisitionPolicyRevisionId
from job_finder.acquisition_policy_service import (
    ActiveAcquisitionPolicy,
    get_active_acquisition_policy,
)

_Connection = psycopg.Connection[tuple[object, ...]]
_RevisionId = Annotated[AcquisitionPolicyRevisionId, Field(pattern=r"^[0-9a-f]{64}$")]


class AcquisitionActivationError(ValueError):
    pass


class _Model(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class ActivateAcquisitionPolicyCommand(_Model):
    idempotency_key: str = Field(min_length=1, max_length=200)
    candidate_revision_id: _RevisionId
    expected_revision_id: _RevisionId
    expected_generation: int = Field(ge=0, le=2**63 - 1)
    actor: str = Field(min_length=1, max_length=200)
    timestamp: datetime


class AcquisitionActivationReceipt(_Model):
    idempotency_key: str
    outcome: Literal["activated", "active_changed"]
    candidate_revision_id: _RevisionId
    expected_revision_id: _RevisionId
    expected_generation: int
    observed_revision_id: _RevisionId
    observed_generation: int
    resulting_generation: int
    actor: str
    requested_at: datetime
    replayed: bool = False


def activate_acquisition_policy(
    connection: _Connection, command: ActivateAcquisitionPolicyCommand
) -> AcquisitionActivationReceipt:
    if not connection.autocommit:
        raise AcquisitionActivationError("Acquisition activation requires autocommit")
    with connection.transaction():
        _ = connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"acquisition_activation:{command.idempotency_key}",),
        )
        previous = load_acquisition_activation_receipt(connection, command.idempotency_key)
        if previous is not None:
            _require_matching_request(previous, command)
            return previous.model_copy(update={"replayed": True})
        _require_published_candidate(connection, command.candidate_revision_id)
        row = connection.execute(
            "SELECT revision_id FROM active_acquisition_policy WHERE singleton_id = 1 FOR UPDATE"
        ).fetchone()
        if row is None:
            raise AcquisitionActivationError("Active acquisition policy is missing")
        observed = get_active_acquisition_policy(connection)
        matches = (
            observed.revision.id == command.expected_revision_id
            and observed.generation == command.expected_generation
        )
        outcome: Literal["activated", "active_changed"] = (
            "activated" if matches else "active_changed"
        )
        resulting_generation = observed.generation + 1 if matches else observed.generation
        if matches:
            _activate_locked_policy(connection, command, observed)
        _store_activation_receipt(connection, command, observed, outcome, resulting_generation)
        receipt = load_acquisition_activation_receipt(connection, command.idempotency_key)
        if receipt is None:
            raise AcquisitionActivationError("Acquisition activation receipt is missing")
        return receipt


def _require_published_candidate(
    connection: _Connection, candidate_revision_id: AcquisitionPolicyRevisionId
) -> None:
    row = connection.execute(
        "SELECT 1 FROM acquisition_policy_publications WHERE revision_id = %s",
        (candidate_revision_id,),
    ).fetchone()
    if row is None:
        raise AcquisitionActivationError("Acquisition candidate is not published")


def _activate_locked_policy(
    connection: _Connection,
    command: ActivateAcquisitionPolicyCommand,
    observed: ActiveAcquisitionPolicy,
) -> None:
    updated = connection.execute(
        """
        UPDATE active_acquisition_policy
        SET revision_id = %s, generation = generation + 1,
            activated_at = %s, activated_by = %s
        WHERE singleton_id = 1 AND revision_id = %s AND generation = %s
        """,
        (
            command.candidate_revision_id,
            command.timestamp,
            command.actor,
            observed.revision.id,
            observed.generation,
        ),
    ).rowcount
    if updated != 1:
        raise AcquisitionActivationError("Locked acquisition policy changed unexpectedly")


def _store_activation_receipt(
    connection: _Connection,
    command: ActivateAcquisitionPolicyCommand,
    observed: ActiveAcquisitionPolicy,
    outcome: Literal["activated", "active_changed"],
    resulting_generation: int,
) -> None:
    _ = connection.execute(
        """
        INSERT INTO acquisition_policy_activation_receipts (
          idempotency_key, outcome, candidate_revision_id,
          expected_revision_id, expected_generation,
          observed_revision_id, observed_generation, resulting_generation,
          actor, requested_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            command.idempotency_key,
            outcome,
            command.candidate_revision_id,
            command.expected_revision_id,
            command.expected_generation,
            observed.revision.id,
            observed.generation,
            resulting_generation,
            command.actor,
            command.timestamp,
        ),
    )


def load_acquisition_activation_receipt(
    connection: _Connection, idempotency_key: str
) -> AcquisitionActivationReceipt | None:
    row = connection.execute(
        """
        SELECT idempotency_key, outcome, candidate_revision_id,
               expected_revision_id, expected_generation,
               observed_revision_id, observed_generation, resulting_generation,
               actor, requested_at
        FROM acquisition_policy_activation_receipts WHERE idempotency_key = %s
        """,
        (idempotency_key,),
    ).fetchone()
    if row is None:
        return None
    return AcquisitionActivationReceipt.model_validate(
        dict(
            zip(
                (
                    "idempotency_key",
                    "outcome",
                    "candidate_revision_id",
                    "expected_revision_id",
                    "expected_generation",
                    "observed_revision_id",
                    "observed_generation",
                    "resulting_generation",
                    "actor",
                    "requested_at",
                ),
                row,
                strict=True,
            )
        )
    )


def _require_matching_request(
    receipt: AcquisitionActivationReceipt, command: ActivateAcquisitionPolicyCommand
) -> None:
    if (
        receipt.candidate_revision_id,
        receipt.expected_revision_id,
        receipt.expected_generation,
        receipt.actor,
    ) != (
        command.candidate_revision_id,
        command.expected_revision_id,
        command.expected_generation,
        command.actor,
    ):
        raise AcquisitionActivationError(
            "Idempotency key belongs to another acquisition activation"
        )
