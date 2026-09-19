from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

import psycopg

from job_finder.review.models import (
    FeedbackCurationFilter,
    ReviewConflict,
    ReviewFeedback,
    ReviewFeedbackPage,
    ReviewFeedbackSummary,
    ReviewItem,
    ReviewQueue,
    ReviewSaved,
    ReviewSubmission,
    ReviewSubmitResult,
)

REJECTED_AUDIT_SIZE = 3
COMPANY_APPLICATION_COOLDOWN = timedelta(days=180)
Connection = psycopg.Connection[tuple[object, ...]]
ConnectionFactory = Callable[[], AbstractContextManager[Connection]]


class ReviewFeedbackNotFound(ValueError):
    pass


@dataclass(frozen=True)
class ReviewService:
    review_queue: Callable[[], ReviewQueue]
    submit: Callable[[ReviewSubmission], ReviewSubmitResult]


def postgres_review_service(connect: ConnectionFactory) -> ReviewService:
    def review_queue() -> ReviewQueue:
        with connect() as connection:
            return load_review_queue(connection)

    def submit(review: ReviewSubmission) -> ReviewSubmitResult:
        with connect() as connection:
            return record_review(connection, review)

    return ReviewService(review_queue=review_queue, submit=submit)


def enqueue_qualified_review_item(
    connection: Connection,
    evaluation_id: str,
    review_day: date,
) -> bool:
    _require_autocommit(connection)
    row = connection.execute(
        """
        SELECT COALESCE(max(position), -1)
        FROM review_items
        WHERE review_day = %s AND lane = 'qualified'
        """,
        (review_day,),
    ).fetchone()
    if row is None:
        raise RuntimeError("Could not read qualified review position")
    item_id = uuid5(
        NAMESPACE_URL,
        f"daily-review:{review_day.isoformat()}:qualified:{evaluation_id}",
    )
    inserted = connection.execute(
        """
        INSERT INTO review_items (
          id, evaluation_id, review_day, lane, position, created_at
        ) VALUES (%s, %s, %s, 'qualified', %s, %s)
        ON CONFLICT (evaluation_id) DO NOTHING
        """,
        (item_id, evaluation_id, review_day, int(str(row[0])) + 1, datetime.now(UTC)),
    ).rowcount
    return inserted == 1


def enqueue_rejected_audit_sample(
    connection: Connection,
    review_day: date,
    size: int = REJECTED_AUDIT_SIZE,
) -> int:
    _require_autocommit(connection)
    if size < 0:
        raise ValueError("Rejected audit size cannot be negative")
    rows = connection.execute(
        """
        SELECT d.id
        FROM evaluation_decisions d
        LEFT JOIN review_items i ON i.evaluation_id = d.id
        WHERE d.outcome = 'rejected'
          AND (d.created_at AT TIME ZONE 'UTC')::date = %s
          AND i.id IS NULL
        ORDER BY d.id
        """,
        (review_day,),
    ).fetchall()
    candidates = tuple(str(row[0]) for row in rows)
    created_at = datetime.now(UTC)
    inserted = 0
    for position, evaluation_id in enumerate(
        deterministic_rejected_sample(review_day, candidates, size)
    ):
        item_id = uuid5(
            NAMESPACE_URL,
            f"daily-review:{review_day.isoformat()}:rejected_audit:{evaluation_id}",
        )
        inserted += connection.execute(
            """
            INSERT INTO review_items (
              id, evaluation_id, review_day, lane, position, created_at
            ) VALUES (%s, %s, %s, 'rejected_audit', %s, %s)
            ON CONFLICT (evaluation_id) DO NOTHING
            """,
            (item_id, evaluation_id, review_day, position, created_at),
        ).rowcount
    return inserted


def load_review_queue(connection: Connection) -> ReviewQueue:
    _require_autocommit(connection)
    rows = connection.execute(
        """
        SELECT i.review_day, i.id, i.evaluation_id, d.snapshot_id, i.lane, i.position,
               d.outcome, d.matched_profile, d.reason,
               s.title, s.company, s.raw_url, s.source,
               COALESCE(sc.description, s.description),
               s.location, s.keywords, s.date_posted,
               COALESCE(sc.compensation_min, s.compensation_min),
               COALESCE(sc.compensation_max, s.compensation_max),
               COALESCE(sc.compensation_currency, s.compensation_currency),
               COALESCE(sc.compensation_period, s.compensation_period),
               COALESCE(sc.compensation_source, s.compensation_source)
        FROM review_items i
        JOIN evaluation_decisions d ON d.id = i.evaluation_id
        JOIN job_snapshots s ON s.id = d.snapshot_id
        LEFT JOIN snapshot_corrections sc ON sc.snapshot_id = s.id
        WHERE NOT EXISTS (
          SELECT 1 FROM review_events ev WHERE ev.review_item_id = i.id
        )
        AND NOT EXISTS (
          SELECT 1 FROM company_policies p
          WHERE p.normalized_company = s.normalized_company
            AND p.effective_at <= CURRENT_TIMESTAMP
            AND (p.expires_at IS NULL OR p.expires_at > CURRENT_TIMESTAMP)
        )
        ORDER BY i.review_day DESC,
                 CASE i.lane WHEN 'qualified' THEN 0 ELSE 1 END,
                 i.position, i.created_at
        """
    ).fetchall()
    reviewed = connection.execute(
        """
        SELECT i.review_day, i.id, i.evaluation_id, d.snapshot_id, i.lane, i.position,
               d.outcome, d.matched_profile, d.reason,
               s.title, s.company, s.raw_url, s.source,
               COALESCE(sc.description, s.description),
               s.location, s.keywords, s.date_posted,
               latest.decision, latest.note, latest.block_company
        FROM review_items i
        JOIN evaluation_decisions d ON d.id = i.evaluation_id
        JOIN job_snapshots s ON s.id = d.snapshot_id
        LEFT JOIN snapshot_corrections sc ON sc.snapshot_id = s.id
        JOIN LATERAL (
          SELECT e.decision, e.note, e.block_company
          FROM review_events e
          WHERE e.review_item_id = i.id
          ORDER BY e.created_at DESC, e.id DESC
          LIMIT 1
        ) latest ON TRUE
        WHERE i.review_day >= CURRENT_DATE - INTERVAL '30 days'
        ORDER BY i.review_day DESC, i.position, i.created_at
        """
    ).fetchall()
    reviewed_items = tuple(_parse_review_item(row, row[17:]) for row in reviewed)
    older_counts = connection.execute(
        """
        SELECT review_day, count(*)
        FROM review_items
        WHERE review_day < CURRENT_DATE - INTERVAL '30 days'
          AND EXISTS (SELECT 1 FROM review_events ev WHERE ev.review_item_id = review_items.id)
        GROUP BY review_day
        """
    ).fetchall()
    reviewed_counts: dict[date, int] = {
        date.fromisoformat(str(row[0])[:10]): int(str(row[1])) for row in older_counts
    }
    for item in reviewed_items:
        reviewed_counts[item.review_day] = reviewed_counts.get(item.review_day, 0) + 1
    return ReviewQueue(
        items=tuple(_parse_review_item(row) for row in rows),
        reviewed_items=reviewed_items,
        reviewed_counts=reviewed_counts,
    )


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


def deterministic_rejected_sample(
    review_day: date,
    evaluation_ids: tuple[str, ...],
    size: int = REJECTED_AUDIT_SIZE,
) -> tuple[str, ...]:
    if size < 0:
        raise ValueError("Rejected audit size cannot be negative")
    ranked = sorted(
        evaluation_ids,
        key=lambda evaluation_id: hashlib.sha256(
            f"{review_day.isoformat()}:{evaluation_id}".encode()
        ).digest(),
    )
    return tuple(ranked[:size])


def _parse_review_item(
    row: tuple[object, ...], event: tuple[object, ...] | None = None
) -> ReviewItem:
    job: dict[str, object] = {
        "title": row[9],
        "company": row[10],
        "url": row[11],
        "source": row[12],
        "description": row[13],
        "location": row[14],
        "keywords": row[15],
        "date_posted": row[16],
    }
    if event is None:
        compensation_fields = row[17:22]
        if any(value is not None for value in compensation_fields):
            job["compensation"] = {
                "minimum": compensation_fields[0],
                "maximum": compensation_fields[1],
                "currency": compensation_fields[2],
                "period": compensation_fields[3],
                "source": compensation_fields[4],
            }
    fields: dict[str, object] = {
        "review_day": row[0],
        "id": row[1],
        "evaluation_id": row[2],
        "snapshot_id": row[3],
        "lane": row[4],
        "position": row[5],
        "outcome": row[6],
        "matched_profile": row[7],
        "evaluation_reason": row[8],
        "job": job,
    }
    if event is not None:
        fields["reviewed"] = True
        fields["decision"] = event[0]
        fields["note"] = event[1]
        fields["block_company"] = event[2]
    return ReviewItem.model_validate(fields)


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
