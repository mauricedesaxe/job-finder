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
    ReviewConflict,
    ReviewDecision,
    ReviewItem,
    ReviewJob,
    ReviewLane,
    ReviewQueue,
    ReviewSaved,
    ReviewSubmission,
    ReviewSubmitResult,
)
from job_finder.review.postgres import ReviewService

TODAY = date(2026, 9, 10)
YESTERDAY = date(2026, 9, 9)
NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
SETTINGS = ReviewAppSettings(
    app_password="correct horse battery staple",
    session_secret="s" * 32,
    cookie_secure=False,
)

Submitter = Callable[[ReviewSubmission], ReviewSubmitResult]


def test_the_queue_renders_day_sections_newest_first() -> None:
    older = _item(YESTERDAY, "qualified", value=1)
    newer = _item(TODAY, "qualified", value=2)
    queue = ReviewQueue(items=(newer, older), reviewed_counts={YESTERDAY: 4})

    response = _client(queue).get("/review")

    assert response.status_code == 200
    assert "Thursday, September 10" in response.text
    assert "Wednesday, September 9" in response.text
    assert response.text.index("Thursday, September 10") < response.text.index(
        "Wednesday, September 9"
    )
    assert "1 waiting · 0 reviewed" in response.text
    assert "1 waiting · 4 reviewed" in response.text


def test_qualified_items_precede_the_rejected_audit_within_a_day() -> None:
    audit = _item(TODAY, "rejected_audit", value=1)
    qualified = _item(TODAY, "qualified", value=2)

    response = _client(_queue(qualified, audit)).get("/review")

    assert response.text.index("Applied AI Engineer 2") < response.text.index(
        "Applied AI Engineer 1"
    )


def test_pending_rows_carry_the_lane_and_link_to_the_job_page() -> None:
    item = _item(TODAY, "qualified")
    audit = _item(TODAY, "rejected_audit", value=2)

    response = _client(_queue(item, audit)).get("/review")

    assert "New result" in response.text
    assert "Second look" in response.text
    assert "Rejected audit" not in response.text
    assert "Applied AI Engineer 1" in response.text
    assert "Applied AI Engineer 2" in response.text
    assert f'href="/review/item/{item.id}"' in response.text
    assert "Acme · Remote" in response.text
    assert "<script" not in response.text


def test_an_empty_queue_renders_a_single_message() -> None:
    response = _client(_queue()).get("/review")

    assert "No jobs waiting for review." in response.text
    assert "September 10" not in response.text
    assert "Applied AI Engineer" not in response.text


def test_a_job_page_links_back_and_walks_the_queue() -> None:
    first = _item(TODAY, "qualified", value=1)
    middle = _item(TODAY, "qualified", value=2)
    last = _item(TODAY, "rejected_audit", value=3)

    response = _client(_queue(first, middle, last)).get(f"/review/item/{middle.id}")

    assert response.status_code == 200
    assert "← All jobs" in response.text
    assert 'href="/review"' in response.text
    assert "2 of 3" in response.text
    assert f'href="/review/item/{first.id}"' in response.text
    assert f'href="/review/item/{last.id}"' in response.text
    assert "← Prev" in response.text
    assert "Next →" in response.text


def test_a_job_page_names_the_lane_and_why_it_is_here() -> None:
    audit = _item(TODAY, "rejected_audit")

    response = _client(_queue(audit)).get(f"/review/item/{audit.id}")

    assert "Second look" in response.text
    assert "Open original listing" in response.text
    assert "Why it's here" in response.text
    assert "Strong product delivery fit." in response.text


def test_a_job_page_renders_the_decision_form_for_a_queued_item() -> None:
    item = _item(TODAY, "qualified")

    response = _client(_queue(item)).get(f"/review/item/{item.id}")

    assert response.status_code == 200
    assert 'name="evaluation_id" value="0000' in response.text
    assert 'name="snapshot_id" value="0000' in response.text
    assert 'name="note"' in response.text
    assert "Pursue" in response.text
    assert "Unsure" in response.text
    assert "Reject" in response.text
    assert 'name="review_day"' not in response.text


def test_a_day_section_renders_reviewed_jobs_after_the_waiting_ones() -> None:
    waiting = _item(TODAY, "qualified", value=1)
    decided = _decided(_item(TODAY, "qualified", value=2), "reject", "Wrong location.")
    queue = ReviewQueue(items=(waiting,), reviewed_items=(decided,), reviewed_counts={TODAY: 1})

    response = _client(queue).get("/review")

    assert response.status_code == 200
    assert "1 waiting · 1 reviewed" in response.text
    assert "Reviewed (1)" in response.text
    assert '<span class="chip">reject</span>' in response.text
    assert "Applied AI Engineer 2" in response.text
    assert "Acme · Remote" in response.text
    assert "Wrong location." in response.text
    assert f'href="/review/item/{decided.id}"' in response.text
    assert ">Change</a>" in response.text
    assert response.text.index("Applied AI Engineer 1") < response.text.index("Reviewed (1)")
    assert response.text.index("Reviewed (1)") < response.text.index("Applied AI Engineer 2")


def test_a_fully_reviewed_day_still_renders_its_section() -> None:
    decided = _decided(_item(TODAY, "qualified", value=1), "pursue")
    queue = ReviewQueue(reviewed_items=(decided,), reviewed_counts={TODAY: 1})

    response = _client(queue).get("/review")

    assert response.status_code == 200
    assert "Thursday, September 10" in response.text
    assert "0 waiting · 1 reviewed" in response.text
    assert "Reviewed (1)" in response.text


def test_a_reviewed_item_page_opens_in_revision_mode() -> None:
    item = _decided(_item(TODAY, "qualified"), "unsure", "Need salary detail.").model_copy(
        update={"block_company": True}
    )
    queue = ReviewQueue(reviewed_items=(item,), reviewed_counts={TODAY: 1})

    response = _client(queue).get(f"/review/item/{item.id}")

    assert response.status_code == 200
    assert "Revision" in response.text
    assert "Revise the recorded decision" in response.text
    assert "<legend>Decision</legend>" not in response.text
    assert "Need salary detail." in response.text
    assert '<input type="checkbox" name="block_company" checked>' in response.text
    assert 'value="unsure" aria-pressed="true"' in response.text
    assert response.text.count("aria-pressed") == 1


def test_submitting_a_revision_returns_to_the_item_page_with_the_update() -> None:
    item = _decided(_item(TODAY, "qualified", value=1), "pursue")
    decided = [item]

    def submit(review: ReviewSubmission) -> ReviewSubmitResult:
        decided[0] = decided[0].model_copy(
            update={"decision": review.decision, "note": review.note}
        )
        return ReviewSaved(review_event_id=UUID(int=9))

    service = ReviewService(
        review_queue=lambda: ReviewQueue(reviewed_items=(decided[0],), reviewed_counts={TODAY: 1}),
        submit=submit,
    )
    client = TestClient(create_review_app(service, SETTINGS, now=lambda: NOW))
    _authenticate(client)

    response = client.post(f"/review/{item.id}", data=_form(item, client), follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/review/item/{item.id}"

    updated = client.get(response.headers["location"])

    assert updated.status_code == 200
    assert "Revision" in updated.text
    assert 'value="reject" aria-pressed="true"' in updated.text


def test_an_unknown_job_renders_a_not_found_state() -> None:
    decided = _decided(_item(TODAY, "qualified", value=2), "reject")
    client = _client(ReviewQueue(items=(_item(TODAY, "qualified"),), reviewed_items=(decided,)))

    response = client.get(f"/review/item/{UUID(int=99)}")

    assert response.status_code == 404
    assert "Review item not found" in response.text
    assert "This job is not part of the review." in response.text


def test_escapes_the_plain_job_description() -> None:
    item = _item(TODAY, "qualified").model_copy(
        update={
            "job": _item(TODAY, "qualified").job.model_copy(
                update={"description": "## Role\n<script>alert('no')</script>"}
            )
        }
    )

    response = _client(_queue(item)).get(f"/review/item/{item.id}")

    assert "&lt;script&gt;alert('no')&lt;/script&gt;" in response.text
    assert "<script>alert" not in response.text


def test_submits_feedback_with_the_exact_rendered_identities() -> None:
    submissions: list[ReviewSubmission] = []
    item = _item(TODAY, "qualified")
    client = _client(
        _queue(item),
        submit=lambda review: submissions.append(review)
        or ReviewSaved(review_event_id=UUID(int=9)),
    )

    response = client.post(
        f"/review/{item.id}",
        data={
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
    assert response.headers["location"] == "/review"
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


def test_the_next_queued_job_opens_after_a_decision() -> None:
    first = _item(TODAY, "qualified", value=1)
    second = _item(TODAY, "qualified", value=2)
    remaining = [first, second]

    def submit(review: ReviewSubmission) -> ReviewSaved:
        remaining[:] = [i for i in remaining if i.id != review.review_item_id]
        return ReviewSaved(review_event_id=UUID(int=9))

    client = _draining_client(remaining, submit)

    response = client.post(f"/review/{first.id}", data=_form(first, client), follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/review/item/{second.id}"


def test_a_submitted_job_leaves_the_queue() -> None:
    item = _item(TODAY, "qualified")
    remaining = [item]

    def submit(review: ReviewSubmission) -> ReviewSaved:
        remaining[:] = [i for i in remaining if i.id != review.review_item_id]
        return ReviewSaved(review_event_id=UUID(int=9))

    client = _draining_client(remaining, submit)

    response = client.post(f"/review/{item.id}", data=_form(item, client), follow_redirects=True)

    assert response.status_code == 200
    assert "No jobs waiting for review." in response.text
    assert "Applied AI Engineer 1" not in response.text


def test_submitting_an_item_outside_the_queue_renders_not_found() -> None:
    submissions: list[ReviewSubmission] = []
    queued = _item(TODAY, "qualified")
    missing = _item(TODAY, "qualified", value=2)
    client = _client(
        _queue(queued),
        submit=lambda review: submissions.append(review)
        or ReviewSaved(review_event_id=UUID(int=9)),
    )

    response = client.post(f"/review/{missing.id}", data=_form(queued, client))

    assert response.status_code == 404
    assert "This job is not part of the review." in response.text
    assert submissions == []


def test_submitting_a_stale_form_renders_not_found() -> None:
    submissions: list[ReviewSubmission] = []
    item = _item(TODAY, "qualified")
    client = _client(
        _queue(item),
        submit=lambda review: submissions.append(review)
        or ReviewSaved(review_event_id=UUID(int=9)),
    )

    response = client.post(
        f"/review/{item.id}", data=_form(item, client) | {"evaluation_id": "f" * 64}
    )

    assert response.status_code == 404
    assert "This job is not part of the review." in response.text
    assert submissions == []


def test_rejects_a_review_without_the_signed_session_csrf_token() -> None:
    submissions: list[ReviewSubmission] = []
    item = _item(TODAY, "qualified")
    client = _client(
        _queue(item),
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


def test_renders_a_domain_conflict_as_an_explicit_conflict() -> None:
    item = _item(TODAY, "qualified")
    client = _client(
        _queue(item),
        submit=lambda _review: ReviewConflict(reason="This job already has a review decision."),
    )

    response = client.post(f"/review/{item.id}", data=_form(item, client), follow_redirects=False)

    assert response.status_code == 409
    assert "This review changed" in response.text
    assert "already has a review decision" in response.text
    assert "Back to the review" in response.text


def test_renders_submit_database_failure_as_retryable_unavailable() -> None:
    item = _item(TODAY, "qualified")

    def unavailable(_review: ReviewSubmission) -> ReviewSaved:
        raise psycopg.OperationalError("database down")

    client = _client(_queue(item), submit=unavailable)

    response = client.post(f"/review/{item.id}", data=_form(item, client))

    assert response.status_code == 503
    assert "Review is unavailable" in response.text
    assert "Retry" in response.text


def test_renders_queue_database_failure_as_retryable_unavailable() -> None:
    def unavailable() -> ReviewQueue:
        raise psycopg.OperationalError("database down")

    app = create_review_app(
        ReviewService(review_queue=unavailable, submit=_saved),
        SETTINGS,
        now=lambda: NOW,
    )
    client = TestClient(app)
    _authenticate(client)

    response = client.get("/review")

    assert response.status_code == 503
    assert "Review is unavailable" in response.text
    assert "Retry" in response.text
    assert "previous decisions are unchanged" in response.text


def test_requires_a_signed_session_for_review_routes() -> None:
    client = TestClient(
        create_review_app(
            ReviewService(review_queue=lambda: _queue(), submit=_saved),
            SETTINGS,
            now=lambda: NOW,
        )
    )

    response = client.get("/review", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Freview"


def test_authenticates_and_signs_out_the_owner() -> None:
    client = TestClient(
        create_review_app(
            ReviewService(review_queue=lambda: _queue(), submit=_saved),
            SETTINGS,
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
        ReviewService(review_queue=lambda: _queue(), submit=_saved),
        SETTINGS,
        readiness=ready,
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
            ReviewService(review_queue=lambda: _queue(), submit=_saved),
            SETTINGS,
            readiness=unavailable,
            now=lambda: NOW,
        )
    )

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.text == "database unavailable"


def _saved(_review: ReviewSubmission) -> ReviewSaved:
    return ReviewSaved(review_event_id=UUID(int=9))


def _client(queue: ReviewQueue, submit: Submitter = _saved) -> TestClient:
    service = ReviewService(review_queue=lambda: queue, submit=submit)
    client = TestClient(create_review_app(service, SETTINGS, now=lambda: NOW))
    _authenticate(client)
    return client


def _draining_client(remaining: list[ReviewItem], submit: Submitter) -> TestClient:
    service = ReviewService(review_queue=lambda: ReviewQueue(items=tuple(remaining)), submit=submit)
    client = TestClient(create_review_app(service, SETTINGS, now=lambda: NOW))
    _authenticate(client)
    return client


def _authenticate(client: TestClient) -> None:
    response = client.post(
        "/login",
        data={"password": SETTINGS.app_password, "next": "/review"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def _queue(*items: ReviewItem) -> ReviewQueue:
    return ReviewQueue(items=items)


def _item(review_day: date, lane: ReviewLane, *, value: int = 1) -> ReviewItem:
    return ReviewItem(
        review_day=review_day,
        id=UUID(int=value),
        evaluation_id=f"{value:064x}",
        snapshot_id=f"{value + 10:064x}",
        lane=lane,
        position=value - 1,
        outcome="qualified" if lane == "qualified" else "rejected",
        matched_profile="applied-ai-product-engineer" if lane == "qualified" else None,
        evaluation_reason="Strong product delivery fit.",
        job=ReviewJob(
            title=f"Applied AI Engineer {value}",
            company="Acme",
            url=f"https://example.com/jobs/{value}",
            source="other",
            description="## Overview\nBuild useful tools.",
            location="Remote",
            keywords=("python",),
            date_posted=date(2026, 9, 9),
        ),
    )


def _decided(item: ReviewItem, decision: ReviewDecision, note: str | None = None) -> ReviewItem:
    return item.model_copy(update={"reviewed": True, "decision": decision, "note": note})


def _form(item: ReviewItem, client: TestClient) -> dict[str, str]:
    return {
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
