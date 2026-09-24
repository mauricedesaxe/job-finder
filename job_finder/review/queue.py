from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import ClassVar, Literal, Self
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from job_finder.database import Connection, ConnectionFactory

ReviewLane = Literal["qualified", "rejected_audit"]
ReviewDecision = Literal["pursue", "reject", "unsure"]
ReviewOutcome = Literal["qualified", "rejected"]
REJECTED_AUDIT_SIZE = 3


class _ReviewQueueModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class ReviewJob(_ReviewQueueModel):
    title: str
    company: str
    url: str
    source: str
    description: str
    location: str
    keywords: tuple[str, ...]
    date_posted: date | None
    compensation: Compensation | None = None


class Compensation(_ReviewQueueModel):
    minimum: Decimal | None = None
    maximum: Decimal | None = None
    currency: str | None = None
    period: str | None = None
    source: str | None = None


class ReviewItem(_ReviewQueueModel):
    review_day: date
    id: UUID
    evaluation_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    lane: ReviewLane
    position: int = Field(ge=0)
    outcome: ReviewOutcome
    matched_profile: str | None
    evaluation_reason: str
    job: ReviewJob
    reviewed: bool = False
    decision: ReviewDecision | None = None
    note: str | None = None
    block_company: bool = False

    @model_validator(mode="after")
    def lane_matches_outcome(self) -> Self:
        if (self.lane == "qualified") != (self.outcome == "qualified"):
            raise ValueError("Review lane must match the evaluation outcome")
        if self.reviewed != (self.decision is not None):
            raise ValueError("A reviewed item must carry its recorded decision")
        return self


class ReviewQueue(_ReviewQueueModel):
    items: tuple[ReviewItem, ...] = ()
    reviewed_items: tuple[ReviewItem, ...] = ()
    reviewed_counts: Mapping[date, int] = {}

    @model_validator(mode="after")
    def counts_are_not_negative(self) -> ReviewQueue:
        if any(count < 0 for count in self.reviewed_counts.values()):
            raise ValueError("Reviewed counts cannot be negative")
        return self

    def reviewed_count(self, review_day: date) -> int:
        return self.reviewed_counts.get(review_day, 0)


@dataclass(frozen=True)
class ReviewQueueService:
    review_queue: Callable[[], ReviewQueue]


def postgres_review_queue_service(connect: ConnectionFactory) -> ReviewQueueService:
    def review_queue() -> ReviewQueue:
        with connect() as connection:
            return load_review_queue(connection)

    return ReviewQueueService(review_queue=review_queue)


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


def _require_autocommit(connection: Connection) -> None:
    if not connection.autocommit:
        raise ValueError("Daily review operations require an autocommit connection")
