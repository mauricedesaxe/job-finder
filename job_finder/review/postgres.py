from __future__ import annotations

import hashlib
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

import psycopg

from job_finder.review.models import (
    DailyReview,
    ReviewConflict,
    ReviewItem,
    ReviewLane,
    ReviewLaneState,
    ReviewSaved,
    ReviewSubmission,
    ReviewSubmitResult,
)

REJECTED_AUDIT_SIZE = 3
Connection = psycopg.Connection[tuple[object, ...]]
ConnectionFactory = Callable[[], AbstractContextManager[Connection]]
Clock = Callable[[], datetime]


@dataclass(frozen=True)
class ReviewService:
    open_day: Callable[[date], DailyReview]
    submit: Callable[[ReviewSubmission], ReviewSubmitResult]


def postgres_review_service(
    connect: ConnectionFactory,
    *,
    now: Clock = lambda: datetime.now(UTC),
    rejected_audit_size: int = REJECTED_AUDIT_SIZE,
) -> ReviewService:
    def open_day(review_day: date) -> DailyReview:
        with connect() as connection:
            prepare_daily_review(
                connection,
                review_day,
                created_at=now(),
                rejected_audit_size=rejected_audit_size,
            )
            return load_daily_review(connection, review_day)

    def submit(review: ReviewSubmission) -> ReviewSubmitResult:
        with connect() as connection:
            return record_review(connection, review)

    return ReviewService(open_day=open_day, submit=submit)


def prepare_daily_review(
    connection: Connection,
    review_day: date,
    *,
    created_at: datetime,
    rejected_audit_size: int = REJECTED_AUDIT_SIZE,
) -> None:
    _require_autocommit(connection)
    if rejected_audit_size < 0:
        raise ValueError("Rejected audit size cannot be negative")
    with connection.transaction():
        _ = connection.execute("LOCK TABLE review_items IN SHARE ROW EXCLUSIVE MODE")
        _append_qualified_items(connection, review_day, created_at)
        _create_rejected_audit_items(
            connection,
            review_day,
            created_at,
            rejected_audit_size,
        )


def load_daily_review(connection: Connection, review_day: date) -> DailyReview:
    _require_autocommit(connection)
    rows = connection.execute(
        """
        SELECT i.id, i.evaluation_id, d.snapshot_id, i.lane, i.position,
               d.outcome, d.matched_profile, d.reason,
               s.title, s.company, s.raw_url, s.source, s.description,
               s.location, s.keywords, s.date_posted,
               (e.id IS NOT NULL) AS reviewed
        FROM review_items i
        JOIN evaluation_decisions d ON d.id = i.evaluation_id
        JOIN job_snapshots s ON s.id = d.snapshot_id
        LEFT JOIN review_events e ON e.review_item_id = i.id
        WHERE i.review_day = %s
        ORDER BY CASE i.lane WHEN 'qualified' THEN 0 ELSE 1 END, i.position
        """,
        (review_day,),
    ).fetchall()
    items = tuple(_parse_review_item(row) for row in rows)
    reviewed = tuple(bool(row[16]) for row in rows)
    return DailyReview(
        day=review_day,
        qualified=_lane_state("qualified", items, reviewed),
        rejected_audit=_lane_state("rejected_audit", items, reviewed),
    )


def record_review(connection: Connection, review: ReviewSubmission) -> ReviewSubmitResult:
    _require_autocommit(connection)
    with connection.transaction():
        row = connection.execute(
            """
            SELECT i.evaluation_id, d.snapshot_id, s.company, s.normalized_company
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

        event_id = uuid5(NAMESPACE_URL, f"review-event:{review.review_item_id}")
        inserted = connection.execute(
            """
            INSERT INTO review_events (
              id, review_item_id, decision, target_profile, primary_reason,
              note, block_company, actor, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (review_item_id) DO NOTHING
            RETURNING id
            """,
            (
                event_id,
                review.review_item_id,
                review.decision,
                review.target_profile,
                review.primary_reason,
                review.note,
                review.block_company,
                review.actor,
                review.created_at,
            ),
        ).fetchone()
        if inserted is None:
            return ReviewConflict(reason="This job already has a review decision.")
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


def _append_qualified_items(connection: Connection, review_day: date, created_at: datetime) -> None:
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
    position = int(str(row[0])) + 1
    candidates = connection.execute(
        """
        SELECT d.id
        FROM evaluation_decisions d
        LEFT JOIN review_items i ON i.evaluation_id = d.id
        WHERE d.outcome = 'qualified'
          AND (d.created_at AT TIME ZONE 'UTC')::date = %s
          AND i.id IS NULL
        ORDER BY d.created_at, d.id
        """,
        (review_day,),
    ).fetchall()
    for row in candidates:
        evaluation_id = str(row[0])
        _insert_review_item(
            connection,
            evaluation_id,
            review_day,
            "qualified",
            position,
            created_at,
        )
        position += 1


def _create_rejected_audit_items(
    connection: Connection,
    review_day: date,
    created_at: datetime,
    rejected_audit_size: int,
) -> None:
    row = connection.execute(
        """
        SELECT count(*)
        FROM review_items
        WHERE review_day = %s AND lane = 'rejected_audit'
        """,
        (review_day,),
    ).fetchone()
    if row is None:
        raise RuntimeError("Could not read rejected audit membership")
    if int(str(row[0])) > 0 or rejected_audit_size == 0:
        return
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
    for position, evaluation_id in enumerate(
        deterministic_rejected_sample(review_day, candidates, rejected_audit_size)
    ):
        _insert_review_item(
            connection,
            evaluation_id,
            review_day,
            "rejected_audit",
            position,
            created_at,
        )


def _insert_review_item(
    connection: Connection,
    evaluation_id: str,
    review_day: date,
    lane: ReviewLane,
    position: int,
    created_at: datetime,
) -> None:
    item_id = uuid5(
        NAMESPACE_URL,
        f"daily-review:{review_day.isoformat()}:{lane}:{evaluation_id}",
    )
    _ = connection.execute(
        """
        INSERT INTO review_items (
          id, evaluation_id, review_day, lane, position, created_at
        ) VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (evaluation_id) DO NOTHING
        """,
        (item_id, evaluation_id, review_day, lane, position, created_at),
    )


def _parse_review_item(row: tuple[object, ...]) -> ReviewItem:
    return ReviewItem.model_validate(
        {
            "id": row[0],
            "evaluation_id": row[1],
            "snapshot_id": row[2],
            "lane": row[3],
            "position": row[4],
            "outcome": row[5],
            "matched_profile": row[6],
            "evaluation_reason": row[7],
            "job": {
                "title": row[8],
                "company": row[9],
                "url": row[10],
                "source": row[11],
                "description": row[12],
                "location": row[13],
                "keywords": row[14],
                "date_posted": row[15],
            },
        }
    )


def _lane_state(
    lane: ReviewLane,
    items: tuple[ReviewItem, ...],
    reviewed: tuple[bool, ...],
) -> ReviewLaneState:
    lane_items = tuple(item for item in items if item.lane == lane)
    pending = tuple(
        item
        for item, is_reviewed in zip(items, reviewed, strict=True)
        if item.lane == lane and not is_reviewed
    )
    return ReviewLaneState(
        lane=lane,
        total=len(lane_items),
        completed=len(lane_items) - len(pending),
        pending=pending,
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
