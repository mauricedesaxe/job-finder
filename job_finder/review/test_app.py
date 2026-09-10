from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from uuid import UUID

import psycopg
from starlette.testclient import TestClient

from job_finder.review.app import create_review_app
from job_finder.review.models import (
    DailyReview,
    ReviewConflict,
    ReviewItem,
    ReviewJob,
    ReviewLane,
    ReviewLaneState,
    ReviewSaved,
    ReviewSubmission,
    ReviewSubmitResult,
)
from job_finder.review.postgres import ReviewService

TODAY = date(2026, 9, 10)
NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


def test_renders_one_focused_qualified_job_without_client_javascript() -> None:
    client = _client(_review(qualified=(_item("qualified"),)))

    response = client.get("/review")

    assert response.status_code == 200
    assert "Choose the next move" in response.text
    assert "0 of 1 reviewed" in response.text
    assert "Qualified match" in response.text
    assert "Applied AI Engineer" in response.text
    assert "Pursue" in response.text
    assert "Unsure" in response.text
    assert "Reject" in response.text
    assert "<script" not in response.text
    assert 'name="evaluation_id" value="aaaaaaaa' in response.text
    assert 'name="snapshot_id" value="bbbbbbbb' in response.text


def test_escapes_the_plain_job_description() -> None:
    item = _item("qualified").model_copy(
        update={
            "job": _item("qualified").job.model_copy(
                update={"description": "## Role\n<script>alert('no')</script>"}
            )
        }
    )
    client = _client(_review(qualified=(item,)))

    response = client.get("/review")

    assert "&lt;script&gt;alert('no')&lt;/script&gt;" in response.text
    assert "<script>alert" not in response.text


def test_renders_the_rejected_audit_as_a_quiet_secondary_lane() -> None:
    client = _client(_review(audit=(_item("rejected_audit"),)))

    response = client.get("/review")

    assert response.status_code == 200
    assert "Rejected audit" in response.text
    assert 'class="job-card audit-card"' in response.text
    assert "Audit 0/1" in response.text


def test_distinguishes_empty_and_complete_days() -> None:
    empty = _client(_review()).get("/review")
    complete_review = _review(completed_qualified=1)
    complete = _client(complete_review).get("/review")

    assert "Nothing to review" in empty.text
    assert "No qualified jobs or rejected audit cases" in empty.text
    assert "Review complete" in complete.text
    assert "reviewed all 1 jobs" in complete.text


def test_renders_database_failure_as_retryable_unavailable() -> None:
    def unavailable(_day: date) -> DailyReview:
        raise psycopg.OperationalError("database down")

    app = create_review_app(
        ReviewService(
            open_day=unavailable, submit=lambda _review: ReviewSaved(review_event_id=UUID(int=9))
        ),
        today=lambda: TODAY,
        now=lambda: NOW,
    )

    response = TestClient(app).get("/review")

    assert response.status_code == 503
    assert "Review is unavailable" in response.text
    assert "Retry" in response.text
    assert "previous decisions are unchanged" in response.text


def test_submits_feedback_with_the_exact_rendered_identities() -> None:
    submissions: list[ReviewSubmission] = []

    def submit(review: ReviewSubmission) -> ReviewSaved:
        submissions.append(review)
        return ReviewSaved(review_event_id=UUID(int=9))

    client = _client(_review(qualified=(_item("qualified"),)), submit=submit)
    item = _item("qualified")
    response = client.post(
        f"/review/{item.id}",
        data={
            "review_day": TODAY.isoformat(),
            "evaluation_id": item.evaluation_id,
            "snapshot_id": item.snapshot_id,
            "decision": "pursue",
            "target_profile": "applied-ai-product-engineer",
            "primary_reason": "technology-fit",
            "note": "Strong fit.",
            "block_company": "on",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/review?day=2026-09-10"
    assert submissions == [
        ReviewSubmission(
            review_item_id=item.id,
            evaluation_id=item.evaluation_id,
            snapshot_id=item.snapshot_id,
            decision="pursue",
            target_profile="applied-ai-product-engineer",
            primary_reason="technology-fit",
            note="Strong fit.",
            block_company=True,
            actor="owner",
            created_at=NOW,
        )
    ]


def test_renders_a_duplicate_submit_as_an_explicit_conflict() -> None:
    item = _item("qualified")
    client = _client(
        _review(qualified=(item,)),
        submit=lambda _review: ReviewConflict(reason="This job already has a review decision."),
    )

    response = client.post(
        f"/review/{item.id}",
        data=_form(item),
        follow_redirects=False,
    )

    assert response.status_code == 409
    assert "This review changed" in response.text
    assert "already has a review decision" in response.text
    assert "Load the current job" in response.text


def test_renders_submit_database_failure_as_retryable_unavailable() -> None:
    item = _item("qualified")

    def unavailable(_review: ReviewSubmission) -> ReviewSaved:
        raise psycopg.OperationalError("database down")

    client = _client(_review(qualified=(item,)), submit=unavailable)

    response = client.post(f"/review/{item.id}", data=_form(item))

    assert response.status_code == 503
    assert "Review is unavailable" in response.text
    assert "Retry" in response.text


Submitter = Callable[[ReviewSubmission], ReviewSubmitResult]


def _saved(_review: ReviewSubmission) -> ReviewSaved:
    return ReviewSaved(review_event_id=UUID(int=9))


def _client(
    review: DailyReview,
    submit: Submitter = _saved,
) -> TestClient:
    service = ReviewService(open_day=lambda _day: review, submit=submit)
    return TestClient(create_review_app(service, today=lambda: TODAY, now=lambda: NOW))


def _review(
    *,
    qualified: tuple[ReviewItem, ...] = (),
    audit: tuple[ReviewItem, ...] = (),
    completed_qualified: int = 0,
) -> DailyReview:
    return DailyReview(
        day=TODAY,
        qualified=ReviewLaneState(
            lane="qualified",
            total=len(qualified) + completed_qualified,
            completed=completed_qualified,
            pending=qualified,
        ),
        rejected_audit=ReviewLaneState(
            lane="rejected_audit",
            total=len(audit),
            completed=0,
            pending=audit,
        ),
    )


def _item(lane: ReviewLane) -> ReviewItem:
    return ReviewItem(
        id=UUID(int=1),
        evaluation_id="a" * 64,
        snapshot_id="b" * 64,
        lane=lane,
        position=0,
        outcome="qualified" if lane == "qualified" else "rejected",
        matched_profile="applied-ai-product-engineer" if lane == "qualified" else None,
        evaluation_reason="Strong product delivery fit.",
        job=ReviewJob(
            title="Applied AI Engineer",
            company="Acme",
            url="https://example.com/jobs/1",
            source="other",
            description="## Overview\nBuild useful tools.",
            location="Remote",
            keywords=("python",),
            date_posted=date(2026, 9, 9),
        ),
    )


def _form(item: ReviewItem) -> dict[str, str]:
    return {
        "review_day": TODAY.isoformat(),
        "evaluation_id": item.evaluation_id,
        "snapshot_id": item.snapshot_id,
        "decision": "reject",
        "target_profile": "neither",
        "primary_reason": "role-scope",
    }
