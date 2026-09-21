from __future__ import annotations

from datetime import datetime
from typing import Annotated, ClassVar, Literal, Self

import psycopg
from pydantic import BaseModel, ConfigDict, Field, model_validator

from job_finder.evaluation.models import PromptReleaseId, ReleaseTarget, RelevanceReleaseId
from job_finder.evaluation.prompt_releases import (
    PromptRelease,
    PromptReleaseError,
    load_prompt_release,
)
from job_finder.evaluation.relevance_releases import (
    build_jev_atomic_policy,
    build_relevance_release,
    RelevanceRelease,
    RelevanceReleaseError,
    RelevanceExecutionPolicy,
    load_relevance_release,
    store_relevance_release,
    validate_release_target,
)

Connection = psycopg.Connection[tuple[object, ...]]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ReleaseTargetLifecycleError(ValueError):
    pass


class ReleaseTargetModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class ActiveReleaseTarget(ReleaseTargetModel):
    target: ReleaseTarget
    generation: int = Field(ge=0)
    activated_at: datetime
    activated_by: str = Field(min_length=1)


class CreateReleaseTargetCandidateCommand(ReleaseTargetModel):
    prompt_release_id: Annotated[PromptReleaseId, Field(pattern=r"^[0-9a-f]{64}$")]
    relevance_release_id: Annotated[RelevanceReleaseId | None, Field(pattern=r"^[0-9a-f]{64}$")] = (
        None
    )
    relevance_policy: RelevanceExecutionPolicy | None = None
    actor: str = Field(min_length=1, max_length=200)
    timestamp: datetime

    @model_validator(mode="after")
    def has_one_relevance_source(self) -> Self:
        if self.relevance_release_id is not None and self.relevance_policy is not None:
            raise ValueError("Candidate cannot specify both a relevance release and policy")
        return self


class ActivateReleaseTargetCommand(ReleaseTargetModel):
    idempotency_key: str = Field(min_length=1, max_length=200)
    promotion_decision_id: Digest
    expected_active_target: ReleaseTarget
    expected_generation: int = Field(ge=0, le=2**63 - 1)
    actor: str = Field(min_length=1, max_length=200)
    timestamp: datetime


class ReleaseTargetActivated(ReleaseTargetModel):
    kind: Literal["activated"] = "activated"
    replayed: bool
    active: ActiveReleaseTarget


class ActiveReleaseTargetChanged(ReleaseTargetModel):
    kind: Literal["active_changed"] = "active_changed"
    replayed: bool
    active: ActiveReleaseTarget


ActivateReleaseTargetResult = Annotated[
    ReleaseTargetActivated | ActiveReleaseTargetChanged,
    Field(discriminator="kind"),
]


class _ApprovedPromotion(ReleaseTargetModel):
    id: Digest
    baseline_target: ReleaseTarget
    candidate_target: ReleaseTarget


class _ActivationReceipt(ReleaseTargetModel):
    outcome: Literal["activated", "active_changed"]
    promotion_decision_id: Digest
    expected_active_target: ReleaseTarget
    expected_generation: int = Field(ge=0)
    observed_active: ActiveReleaseTarget
    resulting_generation: int = Field(ge=0)
    actor: str
    requested_at: datetime
    candidate_target: ReleaseTarget


def create_release_target_candidate(
    connection: Connection,
    command: CreateReleaseTargetCandidateCommand,
) -> ReleaseTarget:
    _require_autocommit(connection)
    prompt_release = load_prompt_release(connection, command.prompt_release_id)
    if command.relevance_release_id is not None:
        relevance_release = load_relevance_release(connection, command.relevance_release_id)
    else:
        relevance_release = store_relevance_release(
            connection,
            build_relevance_release(command.relevance_policy or build_jev_atomic_policy()),
            created_at=command.timestamp,
            created_by=command.actor,
        )
    target = ReleaseTarget(
        prompt_release_id=prompt_release.id,
        relevance_release_id=relevance_release.id,
    )
    validate_release_target(target, prompt_release, relevance_release)
    return target


def get_active_release_target(connection: Connection) -> ActiveReleaseTarget:
    row = connection.execute(
        """
        SELECT prompt_release_id, relevance_release_id, generation,
               activated_at, activated_by
        FROM active_release_target WHERE singleton_id = 1
        """
    ).fetchone()
    if row is None:
        raise ReleaseTargetLifecycleError("Active release target is missing")
    return _active_from_row(row)


def activate_release_target(
    connection: Connection,
    command: ActivateReleaseTargetCommand,
) -> ActivateReleaseTargetResult:
    _require_autocommit(connection)
    with connection.transaction():
        _ = connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext('release_target_activation'), hashtext(%s))",
            (command.idempotency_key,),
        ).fetchone()
        receipt = _load_receipt(connection, command.idempotency_key)
        if receipt is not None:
            _require_matching_receipt(receipt, command)
            return _result_from_receipt(receipt, replayed=True)

        promotion = _load_approved_promotion(connection, command.promotion_decision_id)
        row = connection.execute(
            """
            SELECT prompt_release_id, relevance_release_id, generation,
                   activated_at, activated_by
            FROM active_release_target WHERE singleton_id = 1 FOR UPDATE
            """
        ).fetchone()
        if row is None:
            raise ReleaseTargetLifecycleError("Active release target is missing")
        observed = _active_from_row(row)
        matches = (
            observed.target == promotion.baseline_target
            and observed.target == command.expected_active_target
            and observed.generation == command.expected_generation
        )
        outcome: Literal["activated", "active_changed"]
        resulting_generation: int
        if matches:
            try:
                _ = load_release_target(connection, promotion.candidate_target)
            except (PromptReleaseError, RelevanceReleaseError) as error:
                raise ReleaseTargetLifecycleError(str(error)) from error
            outcome = "activated"
            resulting_generation = observed.generation + 1
            changed = connection.execute(
                """
                UPDATE active_release_target
                SET prompt_release_id = %s, relevance_release_id = %s,
                    generation = %s, activated_at = %s, activated_by = %s
                WHERE singleton_id = 1
                  AND prompt_release_id = %s AND relevance_release_id = %s
                  AND generation = %s
                """,
                (
                    promotion.candidate_target.prompt_release_id,
                    promotion.candidate_target.relevance_release_id,
                    resulting_generation,
                    command.timestamp,
                    command.actor,
                    observed.target.prompt_release_id,
                    observed.target.relevance_release_id,
                    observed.generation,
                ),
            ).rowcount
            if changed != 1:
                raise ReleaseTargetLifecycleError(
                    "Locked active release target changed unexpectedly"
                )
        else:
            outcome = "active_changed"
            resulting_generation = observed.generation

        _ = connection.execute(
            """
            INSERT INTO release_target_activation_receipts (
              idempotency_key, outcome, promotion_decision_id,
              baseline_prompt_release_id, baseline_relevance_release_id,
              candidate_prompt_release_id, candidate_relevance_release_id,
              expected_prompt_release_id, expected_relevance_release_id,
              expected_generation, observed_prompt_release_id,
              observed_relevance_release_id, observed_generation,
              observed_activated_at, observed_activated_by,
              resulting_generation, actor, requested_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                command.idempotency_key,
                outcome,
                promotion.id,
                promotion.baseline_target.prompt_release_id,
                promotion.baseline_target.relevance_release_id,
                promotion.candidate_target.prompt_release_id,
                promotion.candidate_target.relevance_release_id,
                command.expected_active_target.prompt_release_id,
                command.expected_active_target.relevance_release_id,
                command.expected_generation,
                observed.target.prompt_release_id,
                observed.target.relevance_release_id,
                observed.generation,
                observed.activated_at,
                observed.activated_by,
                resulting_generation,
                command.actor,
                command.timestamp,
            ),
        )
        stored = _load_receipt(connection, command.idempotency_key)
        if stored is None:
            raise ReleaseTargetLifecycleError("Activation receipt is missing")
        return _result_from_receipt(stored, replayed=False)


def load_release_target(
    connection: Connection,
    target: ReleaseTarget,
) -> tuple[PromptRelease, RelevanceRelease]:
    prompt_release = load_prompt_release(connection, target.prompt_release_id)
    relevance_release = load_relevance_release(connection, target.relevance_release_id)
    validate_release_target(target, prompt_release, relevance_release)
    return prompt_release, relevance_release


def _load_approved_promotion(connection: Connection, decision_id: str) -> _ApprovedPromotion:
    row = connection.execute(
        """
        SELECT baseline_prompt_release_id, baseline_relevance_release_id,
               candidate_prompt_release_id, candidate_relevance_release_id
        FROM prompt_promotion_decisions
        WHERE id = %s AND decision = 'approved'
        """,
        (decision_id,),
    ).fetchone()
    if row is None:
        raise ReleaseTargetLifecycleError("Approved promotion decision does not exist")
    return _ApprovedPromotion(
        id=decision_id,
        baseline_target=ReleaseTarget(
            prompt_release_id=PromptReleaseId(str(row[0])),
            relevance_release_id=RelevanceReleaseId(str(row[1])),
        ),
        candidate_target=ReleaseTarget(
            prompt_release_id=PromptReleaseId(str(row[2])),
            relevance_release_id=RelevanceReleaseId(str(row[3])),
        ),
    )


def _load_receipt(connection: Connection, idempotency_key: str) -> _ActivationReceipt | None:
    row = connection.execute(
        """
        SELECT outcome, promotion_decision_id,
               expected_prompt_release_id, expected_relevance_release_id,
               expected_generation, observed_prompt_release_id,
               observed_relevance_release_id, observed_generation,
               observed_activated_at, observed_activated_by,
               resulting_generation, actor, requested_at,
               candidate_prompt_release_id, candidate_relevance_release_id
        FROM release_target_activation_receipts WHERE idempotency_key = %s
        """,
        (idempotency_key,),
    ).fetchone()
    if row is None:
        return None
    return _ActivationReceipt.model_validate(
        {
            "outcome": row[0],
            "promotion_decision_id": row[1],
            "expected_active_target": {
                "prompt_release_id": row[2],
                "relevance_release_id": row[3],
            },
            "expected_generation": row[4],
            "observed_active": {
                "target": {
                    "prompt_release_id": row[5],
                    "relevance_release_id": row[6],
                },
                "generation": row[7],
                "activated_at": row[8],
                "activated_by": row[9],
            },
            "resulting_generation": row[10],
            "actor": row[11],
            "requested_at": row[12],
            "candidate_target": {
                "prompt_release_id": row[13],
                "relevance_release_id": row[14],
            },
        }
    )


def _require_matching_receipt(
    receipt: _ActivationReceipt,
    command: ActivateReleaseTargetCommand,
) -> None:
    if (
        receipt.promotion_decision_id != command.promotion_decision_id
        or receipt.expected_active_target != command.expected_active_target
        or receipt.expected_generation != command.expected_generation
        or receipt.actor != command.actor
    ):
        raise ReleaseTargetLifecycleError(
            "Idempotency key belongs to a different release-target activation"
        )


def _result_from_receipt(
    receipt: _ActivationReceipt,
    *,
    replayed: bool,
) -> ActivateReleaseTargetResult:
    if receipt.outcome == "activated":
        return ReleaseTargetActivated(
            replayed=replayed,
            active=ActiveReleaseTarget(
                target=receipt.candidate_target,
                generation=receipt.resulting_generation,
                activated_at=receipt.requested_at,
                activated_by=receipt.actor,
            ),
        )
    return ActiveReleaseTargetChanged(replayed=replayed, active=receipt.observed_active)


def _active_from_row(row: tuple[object, ...]) -> ActiveReleaseTarget:
    return ActiveReleaseTarget(
        target=ReleaseTarget(
            prompt_release_id=PromptReleaseId(str(row[0])),
            relevance_release_id=RelevanceReleaseId(str(row[1])),
        ),
        generation=int(str(row[2])),
        activated_at=datetime.fromisoformat(str(row[3])),
        activated_by=str(row[4]),
    )


def _require_autocommit(connection: Connection) -> None:
    if not connection.autocommit:
        raise ValueError("Release-target lifecycle operations require an autocommit connection")
