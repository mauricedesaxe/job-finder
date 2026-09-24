from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, ClassVar, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator

from job_finder.database import Connection, ConnectionFactory
from job_finder.review.queue import ReviewDecision, ReviewJob, ReviewOutcome

FeedbackCurationFilter = Literal["all", "uncurated", "included", "excluded"]
TargetProfile = Literal[
    "early-stage-product-engineer",
    "applied-ai-product-engineer",
    "neither",
]
PrimaryReason = Literal[
    "crypto-company",
    "location",
    "compensation",
    "role-scope",
    "technology-fit",
    "company-quality",
    "work-environment",
    "insufficient-information",
    "other",
]
COMPANY_APPLICATION_COOLDOWN = timedelta(days=180)


class _ReviewFeedbackModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class FeedbackCuration(_ReviewFeedbackModel):
    id: UUID
    action: Literal["include", "exclude"]
    expected_outcome: ReviewOutcome | None
    critical: bool
    reason: str
    actor: str
    created_at: datetime


class FeedbackCurationSummary(_ReviewFeedbackModel):
    id: UUID
    action: Literal["include", "exclude"]
    expected_outcome: ReviewOutcome | None
    critical: bool


class ReviewFeedbackSummary(_ReviewFeedbackModel):
    review_event_id: UUID
    decision: ReviewDecision
    target_profile: str | None
    primary_reason: str | None
    created_at: datetime
    original_outcome: ReviewOutcome
    title: str
    company: str
    curation: FeedbackCurationSummary | None = None
    frozen_manifest_count: int = Field(ge=0)


class ReviewFeedback(_ReviewFeedbackModel):
    review_event_id: UUID
    review_item_id: UUID
    evaluation_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: ReviewDecision
    target_profile: str | None
    primary_reason: str | None
    note: str | None
    block_company: bool
    actor: str
    created_at: datetime
    original_outcome: ReviewOutcome
    matched_profile: str | None
    evaluation_reason: str
    job: ReviewJob
    curation: FeedbackCuration | None = None
    frozen_manifest_count: int = Field(ge=0)


class ReviewFeedbackPage(_ReviewFeedbackModel):
    items: Annotated[tuple[ReviewFeedbackSummary, ...], Field(max_length=100)]
    next_offset: int | None = Field(default=None, ge=0)


class ReviewSubmission(_ReviewFeedbackModel):
    review_item_id: UUID
    evaluation_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: ReviewDecision
    target_profile: TargetProfile | None = None
    primary_reason: PrimaryReason | None = None
    note: str | None = None
    block_company: bool = False
    actor: str = Field(min_length=1)
    created_at: datetime

    @field_validator("note", mode="before")
    @classmethod
    def empty_note_is_absent(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value


class ReviewSaved(_ReviewFeedbackModel):
    kind: Literal["saved"] = "saved"
    review_event_id: UUID


class ReviewConflict(_ReviewFeedbackModel):
    kind: Literal["conflict"] = "conflict"
    reason: str


ReviewSubmitResult = ReviewSaved | ReviewConflict


class ReviewFeedbackNotFound(ValueError):
    pass


@dataclass(frozen=True)
class ReviewFeedbackService:
    submit: Callable[[ReviewSubmission], ReviewSubmitResult]


def postgres_review_feedback_service(connect: ConnectionFactory) -> ReviewFeedbackService:
    def submit(review: ReviewSubmission) -> ReviewSubmitResult:
        with connect() as connection:
            return record_review(connection, review)

    return ReviewFeedbackService(submit=submit)


def list_review_feedback(
    connection: Connection,
    *,
    curation: FeedbackCurationFilter = "all",
    limit: int = 50,
    offset: int = 0,
) -> ReviewFeedbackPage:
    _require_autocommit(connection)
    if limit < 1 or limit > 100:
        raise ValueError("Feedback page size must be between 1 and 100")
    if offset < 0:
        raise ValueError("Feedback offset cannot be negative")
    action = {"included": "include", "excluded": "exclude"}.get(curation)
    rows = connection.execute(
        _FEEDBACK_QUERY
        + """
        WHERE NOT EXISTS (
          SELECT 1 FROM review_events newer
          WHERE newer.review_item_id = e.review_item_id
            AND (newer.created_at, newer.id) > (e.created_at, e.id)
        )
        AND (%s = 'all'
           OR (%s = 'uncurated' AND c.id IS NULL)
           OR c.action = %s)
        ORDER BY e.created_at DESC, e.id DESC
        LIMIT %s OFFSET %s
        """,
        (curation, curation, action, limit + 1, offset),
    ).fetchall()
    items = tuple(_summarize_review_feedback(_parse_review_feedback(row)) for row in rows[:limit])
    return ReviewFeedbackPage(
        items=items,
        next_offset=offset + limit if len(rows) > limit else None,
    )


def load_review_feedback(connection: Connection, review_event_id: UUID) -> ReviewFeedback:
    _require_autocommit(connection)
    row = connection.execute(
        _FEEDBACK_QUERY + "WHERE e.id = %s",
        (review_event_id,),
    ).fetchone()
    if row is None:
        raise ReviewFeedbackNotFound("Review feedback does not exist")
    return _parse_review_feedback(row)


def record_review(connection: Connection, review: ReviewSubmission) -> ReviewSubmitResult:
    _require_autocommit(connection)
    with connection.transaction():
        row = connection.execute(
            """
            SELECT i.evaluation_id, d.snapshot_id, s.company, s.normalized_company,
                   d.matched_profile, s.job_id
            FROM review_items i
            JOIN evaluation_decisions d ON d.id = i.evaluation_id
            JOIN job_snapshots s ON s.id = d.snapshot_id
            WHERE i.id = %s
            FOR UPDATE OF i
            """,
            (review.review_item_id,),
        ).fetchone()
        if row is None:
            return ReviewConflict(reason="This review item no longer exists.")
        if str(row[0]) != review.evaluation_id or str(row[1]) != review.snapshot_id:
            return ReviewConflict(reason="This review form no longer matches the stored job.")

        derived_profile = review.target_profile or str(row[4]) or "neither"
        latest = connection.execute(
            """
            SELECT id, decision, target_profile, primary_reason, note,
                   block_company, actor
            FROM review_events
            WHERE review_item_id = %s
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (review.review_item_id,),
        ).fetchone()
        if latest is not None and (
            str(latest[1]),
            str(latest[2]),
            None if latest[3] is None else str(latest[3]),
            None if latest[4] is None else str(latest[4]),
            bool(latest[5]),
            str(latest[6]),
        ) == (
            review.decision,
            derived_profile,
            review.primary_reason,
            review.note,
            review.block_company,
            review.actor,
        ):
            return ReviewSaved(review_event_id=UUID(str(latest[0])))
        content_digest = hashlib.sha256(
            json.dumps(
                [
                    review.decision,
                    derived_profile,
                    review.primary_reason,
                    review.note,
                    review.block_company,
                    review.actor,
                ]
            ).encode()
        ).hexdigest()
        predecessor = str(latest[0]) if latest is not None else "initial"
        event_id = uuid5(
            NAMESPACE_URL,
            f"review-event-revision:{review.review_item_id}:{predecessor}:{content_digest}",
        )
        _ = connection.execute(
            """
            INSERT INTO review_events (
              id, review_item_id, decision, target_profile, primary_reason,
              note, block_company, actor, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (
                event_id,
                review.review_item_id,
                review.decision,
                derived_profile,
                review.primary_reason,
                review.note,
                review.block_company,
                review.actor,
                review.created_at,
            ),
        )
        if review.decision == "pursue":
            _record_company_application(
                connection,
                event_id,
                job_id=UUID(str(row[5])),
                company=str(row[2]),
                normalized_company=str(row[3]),
                actor=review.actor,
                effective_at=review.created_at,
            )
        if review.block_company:
            _block_company(
                connection,
                event_id,
                company=str(row[2]),
                normalized_company=str(row[3]),
                effective_at=review.created_at,
            )
        return ReviewSaved(review_event_id=event_id)


_FEEDBACK_QUERY = """
    WITH current_curations AS (
      SELECT DISTINCT ON (review_event_id) *
      FROM evaluation_case_curations
      ORDER BY review_event_id, created_at DESC, id DESC
    )
    SELECT e.id, e.review_item_id, i.evaluation_id, d.snapshot_id,
           e.decision, e.target_profile, e.primary_reason, e.note,
           e.block_company, e.actor, e.created_at,
           d.outcome, d.matched_profile, d.reason,
           s.title, s.company, s.raw_url, s.source,
           COALESCE(sc.description, s.description), s.location, s.keywords,
           s.date_posted,
           COALESCE(sc.compensation_min, s.compensation_min),
           COALESCE(sc.compensation_max, s.compensation_max),
           COALESCE(sc.compensation_currency, s.compensation_currency),
           COALESCE(sc.compensation_period, s.compensation_period),
           COALESCE(sc.compensation_source, s.compensation_source),
           c.id, c.action, c.expected_outcome, c.critical, c.reason,
           c.actor, c.created_at,
           (SELECT count(*) FROM evaluation_manifest_cases mc
            WHERE mc.review_event_id = e.id)
    FROM review_events e
    JOIN review_items i ON i.id = e.review_item_id
    JOIN evaluation_decisions d ON d.id = i.evaluation_id
    JOIN job_snapshots s ON s.id = d.snapshot_id
    LEFT JOIN snapshot_corrections sc ON sc.snapshot_id = s.id
    LEFT JOIN current_curations c ON c.review_event_id = e.id
    """


def _parse_review_feedback(row: tuple[object, ...]) -> ReviewFeedback:
    job: dict[str, object] = {
        "title": row[14],
        "company": row[15],
        "url": row[16],
        "source": row[17],
        "description": row[18],
        "location": row[19],
        "keywords": row[20],
        "date_posted": row[21],
    }
    compensation = row[22:27]
    if any(value is not None for value in compensation):
        job["compensation"] = {
            "minimum": compensation[0],
            "maximum": compensation[1],
            "currency": compensation[2],
            "period": compensation[3],
            "source": compensation[4],
        }
    curation: dict[str, object] | None = None
    if row[27] is not None:
        curation = {
            "id": row[27],
            "action": row[28],
            "expected_outcome": row[29],
            "critical": row[30],
            "reason": row[31],
            "actor": row[32],
            "created_at": row[33],
        }
    return ReviewFeedback.model_validate(
        {
            "review_event_id": row[0],
            "review_item_id": row[1],
            "evaluation_id": row[2],
            "snapshot_id": row[3],
            "decision": row[4],
            "target_profile": row[5],
            "primary_reason": row[6],
            "note": row[7],
            "block_company": row[8],
            "actor": row[9],
            "created_at": row[10],
            "original_outcome": row[11],
            "matched_profile": row[12],
            "evaluation_reason": row[13],
            "job": job,
            "curation": curation,
            "frozen_manifest_count": row[34],
        }
    )


def _summarize_review_feedback(feedback: ReviewFeedback) -> ReviewFeedbackSummary:
    curation = None
    if feedback.curation is not None:
        curation = {
            "id": feedback.curation.id,
            "action": feedback.curation.action,
            "expected_outcome": feedback.curation.expected_outcome,
            "critical": feedback.curation.critical,
        }
    return ReviewFeedbackSummary.model_validate(
        {
            "review_event_id": feedback.review_event_id,
            "decision": feedback.decision,
            "target_profile": feedback.target_profile,
            "primary_reason": feedback.primary_reason,
            "created_at": feedback.created_at,
            "original_outcome": feedback.original_outcome,
            "title": feedback.job.title,
            "company": feedback.job.company,
            "curation": curation,
            "frozen_manifest_count": feedback.frozen_manifest_count,
        }
    )


def _record_company_application(
    connection: Connection,
    review_event_id: UUID,
    *,
    job_id: UUID,
    company: str,
    normalized_company: str,
    actor: str,
    effective_at: datetime,
) -> None:
    _ = connection.execute(
        """
        INSERT INTO application_events (
          id, job_id, kind, source_review_event_id, actor, occurred_at
        ) VALUES (%s, %s, 'applied', %s, %s, %s)
        ON CONFLICT (id) DO NOTHING
        """,
        (
            uuid5(NAMESPACE_URL, f"application-event:{review_event_id}"),
            job_id,
            review_event_id,
            actor,
            effective_at,
        ),
    )
    _ = connection.execute(
        """
        INSERT INTO company_policies (
          normalized_company, company, policy, source_review_event_id,
          effective_at, expires_at
        ) VALUES (%s, %s, 'recent_application', %s, %s, %s)
        ON CONFLICT (normalized_company) DO UPDATE
        SET company = EXCLUDED.company,
            policy = 'recent_application',
            source_review_event_id = EXCLUDED.source_review_event_id,
            effective_at = EXCLUDED.effective_at,
            expires_at = EXCLUDED.expires_at
        WHERE company_policies.policy <> 'blocked'
        """,
        (
            normalized_company,
            company,
            review_event_id,
            effective_at,
            effective_at + COMPANY_APPLICATION_COOLDOWN,
        ),
    )


def _block_company(
    connection: Connection,
    review_event_id: UUID,
    *,
    company: str,
    normalized_company: str,
    effective_at: datetime,
) -> None:
    _ = connection.execute(
        """
        INSERT INTO company_policies (
          normalized_company, company, policy, source_review_event_id,
          effective_at, expires_at
        ) VALUES (%s, %s, 'blocked', %s, %s, NULL)
        ON CONFLICT (normalized_company) DO UPDATE
        SET company = EXCLUDED.company,
            policy = 'blocked',
            source_review_event_id = EXCLUDED.source_review_event_id,
            effective_at = EXCLUDED.effective_at,
            expires_at = NULL
        WHERE company_policies.policy <> 'blocked'
        """,
        (normalized_company, company, review_event_id, effective_at),
    )


def _require_autocommit(connection: Connection) -> None:
    if not connection.autocommit:
        raise ValueError("Daily review operations require an autocommit connection")
