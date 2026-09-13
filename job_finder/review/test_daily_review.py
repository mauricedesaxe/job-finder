from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID

from job_finder.review.models import ReviewItem, ReviewJob, ReviewQueue, ReviewSubmission
from job_finder.review.postgres import deterministic_rejected_sample


def test_selects_a_small_stable_rejected_audit_sample() -> None:
    evaluation_ids = tuple(f"{value:064x}" for value in range(10))
    review_day = date(2026, 9, 10)

    first = deterministic_rejected_sample(review_day, evaluation_ids)
    second = deterministic_rejected_sample(review_day, tuple(reversed(evaluation_ids)))

    assert first == second
    assert len(first) == 3
    assert set(first).issubset(evaluation_ids)


def test_caps_the_rejected_audit_sample_at_the_requested_size() -> None:
    evaluation_ids = tuple(f"{value:064x}" for value in range(10))
    review_day = date(2026, 9, 10)

    sample = deterministic_rejected_sample(review_day, evaluation_ids, 2)
    oversized = deterministic_rejected_sample(review_day, evaluation_ids[:2], 5)

    assert len(sample) == 2
    assert set(sample).issubset(evaluation_ids)
    assert set(oversized) == set(evaluation_ids[:2])


def test_returns_an_empty_rejected_audit_sample_without_candidates() -> None:
    assert deterministic_rejected_sample(date(2026, 9, 10), ()) == ()


def test_queue_reports_the_reviewed_count_per_day() -> None:
    review_day = date(2026, 9, 10)
    queue = ReviewQueue(
        items=(_item(review_day, 1),),
        reviewed_counts={review_day: 4, date(2026, 9, 9): 1},
    )

    assert queue.reviewed_count(review_day) == 4
    assert queue.reviewed_count(date(2026, 9, 9)) == 1
    assert queue.reviewed_count(date(2026, 9, 8)) == 0


def test_queue_defaults_to_no_items_and_no_reviewed_counts() -> None:
    queue = ReviewQueue()

    assert queue.items == ()
    assert queue.reviewed_counts == {}


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


def _item(review_day: date, value: int) -> ReviewItem:
    return ReviewItem.model_validate(
        {
            "review_day": review_day.isoformat(),
            "id": UUID(int=value),
            "evaluation_id": f"{value:064x}",
            "snapshot_id": f"{value + 10:064x}",
            "lane": "qualified",
            "position": 0,
            "outcome": "qualified",
            "matched_profile": "applied-ai-product-engineer",
            "evaluation_reason": "Matches the role.",
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
