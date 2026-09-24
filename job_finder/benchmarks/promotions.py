from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import ClassVar, Literal, Self

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, model_validator

import job_finder.benchmarks.comparisons as _comparisons
import job_finder.benchmarks.executions as _executions
import job_finder.benchmarks.manifests as _benchmark_manifests
import job_finder.benchmarks.scoring as _scoring
from job_finder.evaluation.models import ReleaseTarget

_Connection = psycopg.Connection[tuple[object, ...]]
_Digest = str


class _EvaluationModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class PromptPromotionDecision(_EvaluationModel):
    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    baseline_run_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    baseline_prompt_release_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    baseline_target: ReleaseTarget | None = None
    candidate_run_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_prompt_release_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_target: ReleaseTarget | None = None
    comparison_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    eligible: bool | None = None
    eligibility_failures: tuple[str, ...] = ()
    decision: Literal["approved", "rejected"]
    reason: str
    actor: str
    created_at: datetime

    @model_validator(mode="after")
    def targets_match_prompt_provenance(self) -> Self:
        for prompt_release_id, target in (
            (self.baseline_prompt_release_id, self.baseline_target),
            (self.candidate_prompt_release_id, self.candidate_target),
        ):
            if target is not None and target.prompt_release_id != prompt_release_id:
                raise ValueError("Promotion target must match prompt provenance")
        return self


def record_prompt_promotion_decision(
    connection: _Connection,
    *,
    baseline_run_id: _Digest,
    candidate_run_id: _Digest,
    expected_comparison_id: _Digest,
    decision: Literal["approved", "rejected"],
    reason: str,
    actor: str,
    created_at: datetime,
    idempotency_key: str,
) -> PromptPromotionDecision:
    _require_autocommit(connection)
    if not reason.strip():
        raise ValueError("Promotion decision reason must not be blank")
    with connection.transaction():
        _ = connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (f"prompt_promotion:{idempotency_key}",),
        )
        existing = load_promotion_decision(connection, idempotency_key)
        if existing is not None:
            if (
                existing.baseline_run_id != baseline_run_id
                or existing.candidate_run_id != candidate_run_id
                or existing.comparison_id != expected_comparison_id
                or existing.decision != decision
                or existing.reason != reason
                or existing.actor != actor
            ):
                raise ValueError("Idempotency key belongs to a different promotion decision")
            return existing
        _ = connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"prompt_promotion_pair:{baseline_run_id}:{candidate_run_id}",),
        )
        comparison = _comparisons.preview_run_comparison(
            connection, baseline_run_id, candidate_run_id
        )
        if comparison.id != expected_comparison_id:
            raise ValueError("Promotion decision evidence is stale")
        if decision == "approved" and not comparison.eligible:
            raise ValueError("Ineligible release target cannot be approved")
        duplicate = connection.execute(
            """
            SELECT 1 FROM prompt_promotion_decisions
            WHERE baseline_run_id = %s AND candidate_run_id = %s
            """,
            (baseline_run_id, candidate_run_id),
        ).fetchone()
        if duplicate is not None:
            raise ValueError("This run comparison already has a promotion decision")
        baseline = _executions.load_run(connection, baseline_run_id)
        candidate = _executions.load_run(connection, candidate_run_id)
        promotion_id = _digest({"kind": "prompt_promotion", "idempotency_key": idempotency_key})
        promotion = PromptPromotionDecision(
            id=promotion_id,
            manifest_id=comparison.manifest_id,
            baseline_run_id=baseline.id,
            baseline_prompt_release_id=comparison.baseline_target.prompt_release_id,
            baseline_target=comparison.baseline_target,
            candidate_run_id=candidate.id,
            candidate_prompt_release_id=comparison.candidate_target.prompt_release_id,
            candidate_target=comparison.candidate_target,
            comparison_id=comparison.id,
            eligible=comparison.eligible,
            eligibility_failures=comparison.eligibility_failures,
            decision=decision,
            reason=reason,
            actor=actor,
            created_at=created_at,
        )
        _ = connection.execute(
            """
            INSERT INTO prompt_promotion_decisions (
              id, idempotency_key, manifest_id, baseline_run_id,
              baseline_prompt_release_id, candidate_run_id,
              candidate_prompt_release_id, baseline_relevance_release_id,
              candidate_relevance_release_id, comparison_id, decision, reason,
              baseline_metrics, candidate_metrics, actor, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                promotion.id,
                idempotency_key,
                promotion.manifest_id,
                promotion.baseline_run_id,
                comparison.baseline_target.prompt_release_id,
                promotion.candidate_run_id,
                comparison.candidate_target.prompt_release_id,
                comparison.baseline_target.relevance_release_id,
                comparison.candidate_target.relevance_release_id,
                promotion.comparison_id,
                promotion.decision,
                promotion.reason,
                Jsonb(baseline.metrics.model_dump(mode="json")),
                Jsonb(candidate.metrics.model_dump(mode="json")),
                actor,
                created_at,
            ),
        )
        _benchmark_manifests.enqueue_projection(
            connection, "prompt_promotion", promotion.id, promotion, created_at
        )
    return promotion


def load_promotion_decision(
    connection: _Connection, idempotency_key: str
) -> PromptPromotionDecision | None:
    row = connection.execute(
        """
        SELECT id, manifest_id, baseline_run_id, baseline_prompt_release_id,
               baseline_relevance_release_id, candidate_run_id,
               candidate_prompt_release_id, candidate_relevance_release_id,
               comparison_id, decision, reason, baseline_metrics, candidate_metrics,
               actor, created_at
        FROM prompt_promotion_decisions WHERE idempotency_key = %s
        """,
        (idempotency_key,),
    ).fetchone()
    if row is None:
        return None
    return PromptPromotionDecision.model_validate(
        {
            "id": row[0],
            "manifest_id": row[1],
            "baseline_run_id": row[2],
            "baseline_prompt_release_id": row[3],
            "baseline_target": None
            if row[4] is None
            else {"prompt_release_id": row[3], "relevance_release_id": row[4]},
            "candidate_run_id": row[5],
            "candidate_prompt_release_id": row[6],
            "candidate_target": None
            if row[7] is None
            else {"prompt_release_id": row[6], "relevance_release_id": row[7]},
            "comparison_id": row[8],
            "decision": row[9],
            "reason": row[10],
            "eligible": not _comparisons.promotion_eligibility_failures(
                _benchmark_manifests.load_manifest(connection, str(row[1])).policy,
                _scoring.EvaluationMetrics.model_validate(row[11]),
                _scoring.EvaluationMetrics.model_validate(row[12]),
            ),
            "eligibility_failures": _comparisons.promotion_eligibility_failures(
                _benchmark_manifests.load_manifest(connection, str(row[1])).policy,
                _scoring.EvaluationMetrics.model_validate(row[11]),
                _scoring.EvaluationMetrics.model_validate(row[12]),
            ),
            "actor": row[13],
            "created_at": row[14],
        }
    )


def _digest(value: object) -> _Digest:
    content = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(content.encode()).hexdigest()


def _require_autocommit(connection: _Connection) -> None:
    if not connection.autocommit:
        raise ValueError("Evaluation manifest operations require an autocommit connection")
