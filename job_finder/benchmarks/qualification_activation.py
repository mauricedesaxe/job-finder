from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated, ClassVar, Literal

import psycopg
from pydantic import BaseModel, ConfigDict, Field

from job_finder.benchmarks.qualification_promotions import (
    QualificationPromotionDecision,
    load_qualification_promotion_decision,
    preview_qualification_promotion,
)
from job_finder.evaluation.qualification_components import QualificationTargetId

_Connection = psycopg.Connection[tuple[object, ...]]
_Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class QualificationActivationError(ValueError):
    pass


class _Model(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class ActiveQualificationTarget(_Model):
    target_id: QualificationTargetId | None
    generation: int = Field(ge=0)
    activated_at: datetime | None
    activated_by: str | None


class ActivateQualificationTargetCommand(_Model):
    idempotency_key: str = Field(min_length=1, max_length=200)
    promotion_decision_id: _Digest
    expected_target_id: QualificationTargetId | None
    expected_generation: int = Field(ge=0, le=2**63 - 1)
    actor: str = Field(min_length=1, max_length=200)
    timestamp: datetime


class QualificationActivationReceipt(_Model):
    idempotency_key: str
    outcome: Literal["activated", "active_changed"]
    promotion_decision_id: _Digest
    baseline_target_id: QualificationTargetId
    candidate_target_id: QualificationTargetId
    expected_target_id: QualificationTargetId | None
    expected_generation: int
    observed: ActiveQualificationTarget
    resulting_generation: int
    actor: str
    requested_at: datetime
    replayed: bool = False


def get_active_qualification_target(connection: _Connection) -> ActiveQualificationTarget:
    row = connection.execute(
        "SELECT target_id, generation, activated_at, activated_by FROM active_qualification_target WHERE singleton_id = 1"
    ).fetchone()
    if row is None:
        raise QualificationActivationError("Active qualification target row is missing")
    return ActiveQualificationTarget.model_validate(
        dict(zip(("target_id", "generation", "activated_at", "activated_by"), row, strict=True))
    )


def activate_qualification_target(
    connection: _Connection,
    command: ActivateQualificationTargetCommand,
    artifact_path: Path,
) -> QualificationActivationReceipt:
    if not connection.autocommit:
        raise QualificationActivationError("Qualification activation requires autocommit")
    with connection.transaction():
        _ = connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"qualification_activation:{command.idempotency_key}",),
        )
        previous = load_qualification_activation_receipt(connection, command.idempotency_key)
        if previous is not None:
            _require_matching_receipt(previous, command)
            return previous.model_copy(update={"replayed": True})
        promotion = _load_approved_promotion(connection, command.promotion_decision_id)
        preview = preview_qualification_promotion(
            connection,
            promotion.baseline_target_id,
            promotion.candidate_target_id,
            promotion.evidence,
            artifact_path,
        )
        if not preview.eligible:
            raise QualificationActivationError(
                "Promotion evidence is incomplete: " + "; ".join(preview.failures)
            )
        row = connection.execute(
            "SELECT target_id, generation, activated_at, activated_by FROM active_qualification_target WHERE singleton_id = 1 FOR UPDATE"
        ).fetchone()
        if row is None:
            raise QualificationActivationError("Active qualification target row is missing")
        observed = ActiveQualificationTarget.model_validate(
            dict(zip(("target_id", "generation", "activated_at", "activated_by"), row, strict=True))
        )
        matches = _matches_active(observed, command, promotion)
        outcome: Literal["activated", "active_changed"] = (
            "activated" if matches else "active_changed"
        )
        resulting_generation = observed.generation + 1 if matches else observed.generation
        if matches:
            updated = connection.execute(
                """
                UPDATE active_qualification_target
                SET target_id = %s, generation = %s, activated_at = %s, activated_by = %s
                WHERE singleton_id = 1 AND target_id IS NOT DISTINCT FROM %s AND generation = %s
                """,
                (
                    promotion.candidate_target_id,
                    resulting_generation,
                    command.timestamp,
                    command.actor,
                    observed.target_id,
                    observed.generation,
                ),
            ).rowcount
            if updated != 1:
                raise QualificationActivationError(
                    "Locked qualification authority changed unexpectedly"
                )
        _store_receipt(connection, command, promotion, observed, outcome, resulting_generation)
        receipt = load_qualification_activation_receipt(connection, command.idempotency_key)
        if receipt is None:
            raise QualificationActivationError("Qualification activation receipt is missing")
        return receipt


def _matches_active(
    observed: ActiveQualificationTarget,
    command: ActivateQualificationTargetCommand,
    promotion: QualificationPromotionDecision,
) -> bool:
    return (
        observed.target_id == command.expected_target_id
        and observed.generation == command.expected_generation
        and (observed.target_id is None or observed.target_id == promotion.baseline_target_id)
    )


def _load_approved_promotion(
    connection: _Connection, decision_id: str
) -> QualificationPromotionDecision:
    row = connection.execute(
        "SELECT idempotency_key FROM qualification_promotion_decisions WHERE id = %s AND decision = 'approved'",
        (decision_id,),
    ).fetchone()
    if row is None:
        raise QualificationActivationError("Approved qualification promotion does not exist")
    promotion = load_qualification_promotion_decision(connection, str(row[0]))
    if promotion is None:
        raise QualificationActivationError("Approved qualification promotion is missing")
    return promotion


def _store_receipt(
    connection: _Connection,
    command: ActivateQualificationTargetCommand,
    promotion: QualificationPromotionDecision,
    observed: ActiveQualificationTarget,
    outcome: Literal["activated", "active_changed"],
    resulting_generation: int,
) -> None:
    _ = connection.execute(
        """
        INSERT INTO qualification_target_activation_receipts (
          idempotency_key, outcome, promotion_decision_id,
          baseline_target_id, candidate_target_id, expected_target_id,
          expected_generation, observed_target_id, observed_generation,
          observed_activated_at, observed_activated_by, resulting_generation,
          actor, requested_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            command.idempotency_key,
            outcome,
            promotion.id,
            promotion.baseline_target_id,
            promotion.candidate_target_id,
            command.expected_target_id,
            command.expected_generation,
            observed.target_id,
            observed.generation,
            observed.activated_at,
            observed.activated_by,
            resulting_generation,
            command.actor,
            command.timestamp,
        ),
    )


def load_qualification_activation_receipt(
    connection: _Connection, idempotency_key: str
) -> QualificationActivationReceipt | None:
    row = connection.execute(
        """
        SELECT idempotency_key, outcome, promotion_decision_id,
               baseline_target_id, candidate_target_id, expected_target_id,
               expected_generation, observed_target_id, observed_generation,
               observed_activated_at, observed_activated_by, resulting_generation,
               actor, requested_at
        FROM qualification_target_activation_receipts WHERE idempotency_key = %s
        """,
        (idempotency_key,),
    ).fetchone()
    if row is None:
        return None
    return QualificationActivationReceipt.model_validate(
        {
            "idempotency_key": row[0],
            "outcome": row[1],
            "promotion_decision_id": row[2],
            "baseline_target_id": row[3],
            "candidate_target_id": row[4],
            "expected_target_id": row[5],
            "expected_generation": row[6],
            "observed": {
                "target_id": row[7],
                "generation": row[8],
                "activated_at": row[9],
                "activated_by": row[10],
            },
            "resulting_generation": row[11],
            "actor": row[12],
            "requested_at": row[13],
        }
    )


def _require_matching_receipt(
    receipt: QualificationActivationReceipt, command: ActivateQualificationTargetCommand
) -> None:
    if (
        receipt.promotion_decision_id,
        receipt.expected_target_id,
        receipt.expected_generation,
        receipt.actor,
    ) != (
        command.promotion_decision_id,
        command.expected_target_id,
        command.expected_generation,
        command.actor,
    ):
        raise QualificationActivationError("Idempotency key belongs to another activation request")
