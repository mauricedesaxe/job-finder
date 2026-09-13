from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

import psycopg

from job_finder.review.models import (
    ReviewConflict,
    ReviewItem,
    ReviewQueue,
    ReviewSaved,
    ReviewSubmission,
    ReviewSubmitResult,
)

REJECTED_AUDIT_SIZE = 3
Connection = psycopg.Connection[tuple[object, ...]]
ConnectionFactory = Callable[[], AbstractContextManager[Connection]]


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
               s.title, s.company, s.raw_url, s.source, s.description,
               s.location, s.keywords, s.date_posted
        FROM review_items i
        JOIN evaluation_decisions d ON d.id = i.evaluation_id
        JOIN job_snapshots s ON s.id = d.snapshot_id
        WHERE NOT EXISTS (
          SELECT 1 FROM review_events ev WHERE ev.review_item_id = i.id
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
               s.title, s.company, s.raw_url, s.source, s.description,
               s.location, s.keywords, s.date_posted,
               latest.decision, latest.note, latest.block_company
        FROM review_items i
        JOIN evaluation_decisions d ON d.id = i.evaluation_id
        JOIN job_snapshots s ON s.id = d.snapshot_id
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


def record_review(connection: Connection, review: ReviewSubmission) -> ReviewSubmitResult:
    _require_autocommit(connection)
    with connection.transaction():
        row = connection.execute(
            """
            SELECT i.evaluation_id, d.snapshot_id, s.company, s.normalized_company,
                   d.matched_profile
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
        event_id = uuid5(
            NAMESPACE_URL,
            f"review-event-revision:{review.review_item_id}:{content_digest}",
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
        "job": {
            "title": row[9],
            "company": row[10],
            "url": row[11],
            "source": row[12],
            "description": row[13],
            "location": row[14],
            "keywords": row[15],
            "date_posted": row[16],
        },
    }
    if event is not None:
        fields["reviewed"] = True
        fields["decision"] = event[0]
        fields["note"] = event[1]
        fields["block_company"] = event[2]
    return ReviewItem.model_validate(fields)


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
