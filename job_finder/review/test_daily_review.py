from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID

from job_finder.review.models import (
    DailyReview,
    ReviewItem,
    ReviewJob,
    ReviewLaneState,
    ReviewSubmission,
)
from job_finder.review.postgres import deterministic_rejected_sample


def test_selects_a_small_stable_rejected_audit_sample() -> None:
    evaluation_ids = tuple(f"{value:064x}" for value in range(10))
    review_day = date(2026, 9, 10)

    first = deterministic_rejected_sample(review_day, evaluation_ids)
    second = deterministic_rejected_sample(review_day, tuple(reversed(evaluation_ids)))

    assert first == second
    assert len(first) == 3
    assert set(first).issubset(evaluation_ids)


def test_prioritizes_qualified_work_before_the_rejected_audit() -> None:
    qualified = _item("qualified", 1)
    audit = _item("rejected_audit", 2)
    reviewed = _item("qualified", 3, reviewed=True)
    review = DailyReview(
        day=date(2026, 9, 10),
        qualified=ReviewLaneState(
            lane="qualified",
            total=2,
            completed=1,
            pending=(qualified,),
            reviewed_items=(reviewed,),
        ),
        rejected_audit=ReviewLaneState(
            lane="rejected_audit", total=1, completed=0, pending=(audit,)
        ),
    )

    assert review.current == qualified
    assert review.completed == 1
    assert review.total == 3


def test_orders_a_day_by_lane_then_position_across_review_states() -> None:
    audit_pending = _item("rejected_audit", 1, position=0)
    qualified_reviewed = _item("qualified", 2, position=1, reviewed=True)
    qualified_pending = _item("qualified", 3, position=0)
    review = DailyReview(
        day=date(2026, 9, 10),
        qualified=ReviewLaneState(
            lane="qualified",
            total=2,
            completed=1,
            pending=(qualified_pending,),
            reviewed_items=(qualified_reviewed,),
        ),
        rejected_audit=ReviewLaneState(
            lane="rejected_audit", total=1, completed=0, pending=(audit_pending,)
        ),
    )

    assert review.ordered_items == (qualified_pending, qualified_reviewed, audit_pending)


def test_treats_a_blank_feedback_note_as_absent() -> None:
    review = ReviewSubmission(
        review_item_id=UUID(int=1),
        evaluation_id="a" * 64,
        snapshot_id="b" * 64,
        decision="unsure",
        target_profile="neither",
        primary_reason="insufficient-information",
        note="   ",
        actor="owner",
        created_at=datetime(2026, 9, 10, 12, 0, tzinfo=UTC),
    )

    assert review.note is None


def _item(lane: str, value: int, *, position: int = 0, reviewed: bool = False) -> ReviewItem:
    return ReviewItem.model_validate(
        {
            "id": UUID(int=value),
            "evaluation_id": f"{value:064x}",
            "snapshot_id": f"{value + 10:064x}",
            "lane": lane,
            "position": position,
            "outcome": "qualified" if lane == "qualified" else "rejected",
            "matched_profile": "applied-ai-product-engineer" if lane == "qualified" else None,
            "evaluation_reason": "Matches the role.",
            "reviewed": reviewed,
            "decision": "pursue" if reviewed else None,
            "job": ReviewJob(
                title="Applied AI Engineer",
                company="Acme",
                url="https://example.com/jobs/1",
                source="other",
                description="Build useful tools.",
                location="Remote",
                keywords=("python",),
                date_posted=date(2026, 9, 9),
            ),
        }
    )
