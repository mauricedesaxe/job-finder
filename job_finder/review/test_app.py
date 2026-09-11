from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
import re
from uuid import UUID

import psycopg
from starlette.testclient import TestClient

from job_finder.config import ReviewAppSettings
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
SETTINGS = ReviewAppSettings(
    app_password="correct horse battery staple",
    session_secret="s" * 32,
    cookie_secure=False,
)


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


def test_requires_a_signed_session_for_review_routes() -> None:
    client = TestClient(
        create_review_app(
            ReviewService(open_day=lambda _day: _review(), submit=_saved),
            SETTINGS,
            today=lambda: TODAY,
            now=lambda: NOW,
        )
    )

    response = client.get("/review?day=2026-09-10", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Freview%3Fday%3D2026-09-10"


def test_authenticates_and_signs_out_the_owner() -> None:
    client = TestClient(
        create_review_app(
            ReviewService(open_day=lambda _day: _review(), submit=_saved),
            SETTINGS,
            today=lambda: TODAY,
            now=lambda: NOW,
        )
    )

    rejected = client.post("/login", data={"password": "wrong password", "next": "/review"})
    accepted = client.post(
        "/login",
        data={"password": SETTINGS.app_password, "next": "/review"},
        follow_redirects=False,
    )
    csrf_token = _csrf(client)
    signed_out = client.post("/logout", data={"csrf_token": csrf_token}, follow_redirects=False)

    assert rejected.status_code == 401
    assert "password is incorrect" in rejected.text
    assert accepted.status_code == 303
    assert accepted.headers["location"] == "/review"
    assert signed_out.status_code == 303
    assert client.get("/review", follow_redirects=False).status_code == 303


def test_exposes_public_health_and_database_readiness() -> None:
    readiness_calls = 0

    def ready() -> None:
        nonlocal readiness_calls
        readiness_calls += 1

    app = create_review_app(
        ReviewService(open_day=lambda _day: _review(), submit=_saved),
        SETTINGS,
        readiness=ready,
        today=lambda: TODAY,
        now=lambda: NOW,
    )
    client = TestClient(app)

    assert client.get("/healthz").text == "ok"
    assert client.get("/readyz").text == "ready"
    assert readiness_calls == 1


def test_reports_database_readiness_failure_without_authentication() -> None:
    def unavailable() -> None:
        raise psycopg.OperationalError("database down")

    client = TestClient(
        create_review_app(
            ReviewService(open_day=lambda _day: _review(), submit=_saved),
            SETTINGS,
            readiness=unavailable,
            today=lambda: TODAY,
            now=lambda: NOW,
        )
    )

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.text == "database unavailable"


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
    complete_review = _review(reviewed_qualified=(_item("qualified", reviewed=True),))
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
        SETTINGS,
        today=lambda: TODAY,
        now=lambda: NOW,
    )

    client = TestClient(app)
    _authenticate(client)
    response = client.get("/review")

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
            "csrf_token": _csrf(client),
            "evaluation_id": item.evaluation_id,
            "snapshot_id": item.snapshot_id,
            "decision": "pursue",
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
            target_profile=None,
            primary_reason=None,
            note="Strong fit.",
            block_company=True,
            actor="owner",
            created_at=NOW,
        )
    ]


def test_rejects_a_review_without_the_signed_session_csrf_token() -> None:
    submissions: list[ReviewSubmission] = []
    item = _item("qualified")
    client = _client(
        _review(qualified=(item,)),
        submit=lambda review: submissions.append(review)
        or ReviewSaved(review_event_id=UUID(int=9)),
    )

    response = client.post(
        f"/review/{item.id}",
        data={key: value for key, value in _form(item, client).items() if key != "csrf_token"},
    )

    assert response.status_code == 409
    assert "form expired" in response.text
    assert submissions == []


def test_renders_a_duplicate_submit_as_an_explicit_conflict() -> None:
    item = _item("qualified")
    client = _client(
        _review(qualified=(item,)),
        submit=lambda _review: ReviewConflict(reason="This job already has a review decision."),
    )

    response = client.post(
        f"/review/{item.id}",
        data=_form(item, client),
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

    response = client.post(f"/review/{item.id}", data=_form(item, client))

    assert response.status_code == 503
    assert "Review is unavailable" in response.text
    assert "Retry" in response.text


def test_a_reviewed_item_renders_its_recorded_feedback_and_a_revision_form() -> None:
    reviewed = _item("qualified", reviewed=True)
    client = _client(_review(reviewed_qualified=(reviewed,)))

    response = client.get(f"/review/item/{reviewed.id}")

    assert response.status_code == 200
    assert "You recorded: reject" in response.text
    assert "Every revision below is kept" in response.text
    assert 'name="note"' in response.text
    assert ">Seen it before.</textarea>" in response.text
    assert 'value="pursue"' in response.text
    assert "Target profile" not in response.text
    assert "Primary reason" not in response.text


def test_the_header_lists_reviewed_items_with_links_back_to_them() -> None:
    reviewed = _item("qualified", reviewed=True)
    client = _client(_review(qualified=(_item("qualified"),), reviewed_qualified=(reviewed,)))

    response = client.get("/review")

    assert "Reviewed (1)" in response.text
    assert f'href="/review/item/{reviewed.id}"' in response.text
    assert "reject" in response.text


def test_a_revision_submit_still_carries_the_exact_rendered_identities() -> None:
    submissions: list[ReviewSubmission] = []

    def submit(review: ReviewSubmission) -> ReviewSaved:
        submissions.append(review)
        return ReviewSaved(review_event_id=UUID(int=9))

    reviewed = _item("qualified", reviewed=True)
    client = _client(_review(reviewed_qualified=(reviewed,)), submit=submit)

    response = client.post(
        f"/review/{reviewed.id}",
        data={
            "review_day": TODAY.isoformat(),
            "csrf_token": _csrf(client),
            "evaluation_id": reviewed.evaluation_id,
            "snapshot_id": reviewed.snapshot_id,
            "decision": "unsure",
            "note": "Second thoughts.",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert submissions == [
        ReviewSubmission(
            review_item_id=reviewed.id,
            evaluation_id=reviewed.evaluation_id,
            snapshot_id=reviewed.snapshot_id,
            decision="unsure",
            target_profile=None,
            primary_reason=None,
            note="Second thoughts.",
            block_company=False,
            actor="owner",
            created_at=NOW,
        )
    ]


def test_an_unknown_review_item_renders_a_not_found_state() -> None:
    client = _client(_review(reviewed_qualified=(_item("qualified", reviewed=True),)))

    response = client.get(f"/review/item/{UUID(int=99)}")

    assert response.status_code == 404
    assert "Review item not found" in response.text


Submitter = Callable[[ReviewSubmission], ReviewSubmitResult]


def _saved(_review: ReviewSubmission) -> ReviewSaved:
    return ReviewSaved(review_event_id=UUID(int=9))


def _client(
    review: DailyReview,
    submit: Submitter = _saved,
) -> TestClient:
    service = ReviewService(open_day=lambda _day: review, submit=submit)
    client = TestClient(create_review_app(service, SETTINGS, today=lambda: TODAY, now=lambda: NOW))
    _authenticate(client)
    return client


def _authenticate(client: TestClient) -> None:
    response = client.post(
        "/login",
        data={"password": SETTINGS.app_password, "next": "/review"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def _review(
    *,
    qualified: tuple[ReviewItem, ...] = (),
    audit: tuple[ReviewItem, ...] = (),
    reviewed_qualified: tuple[ReviewItem, ...] = (),
    reviewed_audit: tuple[ReviewItem, ...] = (),
) -> DailyReview:
    return DailyReview(
        day=TODAY,
        qualified=ReviewLaneState(
            lane="qualified",
            total=len(qualified) + len(reviewed_qualified),
            completed=len(reviewed_qualified),
            pending=qualified,
            reviewed_items=reviewed_qualified,
        ),
        rejected_audit=ReviewLaneState(
            lane="rejected_audit",
            total=len(audit) + len(reviewed_audit),
            completed=len(reviewed_audit),
            pending=audit,
            reviewed_items=reviewed_audit,
        ),
    )


def _item(lane: ReviewLane, *, reviewed: bool = False) -> ReviewItem:
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
        reviewed=reviewed,
        decision="reject" if reviewed else None,
        note="Seen it before." if reviewed else None,
    )


def _form(item: ReviewItem, client: TestClient) -> dict[str, str]:
    return {
        "review_day": TODAY.isoformat(),
        "csrf_token": _csrf(client),
        "evaluation_id": item.evaluation_id,
        "snapshot_id": item.snapshot_id,
        "decision": "reject",
    }


def _csrf(client: TestClient) -> str:
    response = client.get("/review")
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match is not None
    return match.group(1)
