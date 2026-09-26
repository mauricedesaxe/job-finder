from __future__ import annotations

from decimal import Decimal
import re
from uuid import UUID

import psycopg
import pytest
from starlette.testclient import TestClient

from job_finder.web.app import create_review_app
from job_finder.review.feedback import (
    ReviewConflict,
    ReviewSaved,
    ReviewSubmission,
    ReviewSubmitResult,
)
from job_finder.review.queue import (
    Compensation,
    ReviewQueue,
)
from job_finder.pipeline.work_recoveries import (
    WorkRecoveryCommand,
    WorkRecoveryResult,
    WorkRecoveryStaleState,
)
from job_finder.pipeline.reevaluations import (
    JobReevaluationCommand,
    JobReevaluationResult,
)
from job_finder.operations.service import OperationsService

from job_finder.review.test_app_support import (
    helper_default_submit_review as _default_submit_review,
    helper_client as _client,
    helper_draining_client as _draining_client,
    helper_authenticate as _authenticate,
    helper_queue as _queue,
    helper_item as _item,
    helper_decided as _decided,
    helper_form as _form,
    helper_csrf as _csrf,
    helper_operations_snapshot as _operations_snapshot,
    helper_accepted_reevaluation as _accepted_reevaluation,
    helper_recovery_receipt as _recovery_receipt,
    TODAY,
    YESTERDAY,
    NOW,
    SETTINGS,
    OWNER_ACCESS,
)


def test_work_recovery_reports_a_stale_expected_state_as_conflict() -> None:
    def recover(command: WorkRecoveryCommand) -> WorkRecoveryResult:
        receipt = _recovery_receipt(command, outcome="stale_state", prior_state="completed")
        return WorkRecoveryStaleState(receipt=receipt, replayed=False)

    client = _client(
        _queue(),
        operations=OperationsService(load=lambda: _operations_snapshot(), recover=recover),
    )

    response = client.post(
        "/operations/recovery",
        data={
            "csrf_token": _csrf(client),
            "job_id": str(UUID(int=31)),
            "action": "retry_now",
            "expected_state": "failed",
            "expected_attempt_count": "2",
            "idempotency_key": "private-key",
        },
    )

    assert response.status_code == 409
    assert "Observed completed" in response.text


def test_job_detail_renders_an_exact_append_only_reevaluation_form() -> None:
    item = _item(TODAY, "qualified")

    response = _client(_queue(item)).get(f"/review/item/{item.id}")

    assert response.status_code == 200
    assert 'action="/operations/reevaluation"' in response.text
    assert "Re-evaluate this job" in response.text
    assert f'name="expected_decision_id" value="{item.evaluation_id}"' in response.text
    assert f'name="expected_snapshot_id" value="{item.snapshot_id}"' in response.text
    assert 'name="idempotency_key"' in response.text
    assert "without changing prior history" not in response.text


def test_job_reevaluation_requires_csrf_before_calling_the_service() -> None:
    calls: list[JobReevaluationCommand] = []
    operations = OperationsService(
        load=lambda: _operations_snapshot(),
        reevaluate=lambda command: calls.append(command) or _accepted_reevaluation(command),
    )
    client = _client(_queue(), operations=operations)

    response = client.post(
        "/operations/reevaluation",
        data={
            "expected_decision_id": "1" * 64,
            "expected_snapshot_id": "2" * 64,
            "idempotency_key": "private-key",
        },
    )

    assert response.status_code == 403
    assert calls == []


def test_job_reevaluation_passes_an_exact_typed_command_and_redirects() -> None:
    calls: list[JobReevaluationCommand] = []

    def reevaluate(command: JobReevaluationCommand) -> JobReevaluationResult:
        calls.append(command)
        return _accepted_reevaluation(command)

    client = _client(
        _queue(),
        operations=OperationsService(load=lambda: _operations_snapshot(), reevaluate=reevaluate),
    )

    response = client.post(
        "/operations/reevaluation",
        data={
            "csrf_token": _csrf(client),
            "expected_decision_id": "1" * 64,
            "expected_snapshot_id": "2" * 64,
            "idempotency_key": "private-key",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/operations/runs?notice=reevaluation-requested"
    assert calls == [
        JobReevaluationCommand(
            idempotency_key="private-key",
            expected_decision_id="1" * 64,
            expected_snapshot_id="2" * 64,
            actor="owner",
            requested_at=NOW,
        )
    ]


def test_unconfigured_job_reevaluation_reports_missing_service(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _client(_queue())

    response = client.post(
        "/operations/reevaluation",
        data={
            "csrf_token": _csrf(client),
            "expected_decision_id": "1" * 64,
            "expected_snapshot_id": "2" * 64,
            "idempotency_key": "private-key",
        },
    )

    assert response.status_code == 503
    assert "Reevaluation is not configured for this deployment" in response.text
    assert "Reevaluation status is uncertain" not in response.text
    assert "Reevaluation request failed: OperationsUnavailable" in caplog.text
    assert "private-key" not in caplog.text


def test_uncertain_job_reevaluation_preserves_the_exact_retry_command(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def reevaluate(_command: JobReevaluationCommand) -> JobReevaluationResult:
        raise psycopg.OperationalError("secret-password")

    client = _client(
        _queue(),
        operations=OperationsService(load=lambda: _operations_snapshot(), reevaluate=reevaluate),
    )

    response = client.post(
        "/operations/reevaluation",
        data={
            "csrf_token": _csrf(client),
            "expected_decision_id": "1" * 64,
            "expected_snapshot_id": "2" * 64,
            "idempotency_key": "the-same-private-key",
        },
    )

    assert response.status_code == 503
    assert "Reevaluation status is uncertain" in response.text
    assert 'action="/operations/reevaluation"' in response.text
    assert response.text.count('name="idempotency_key" value="the-same-private-key"') == 1
    assert 'name="expected_decision_id" value="' + "1" * 64 + '"' in response.text
    assert 'name="expected_snapshot_id" value="' + "2" * 64 + '"' in response.text
    assert "Reevaluation request failed: OperationalError" in caplog.text
    assert "secret-password" not in response.text + caplog.text


def test_the_queue_renders_day_sections_newest_first() -> None:
    older = _item(YESTERDAY, "qualified", value=1)
    newer = _item(TODAY, "qualified", value=2)
    queue = ReviewQueue(items=(newer, older), reviewed_counts={YESTERDAY: 4})

    response = _client(queue).get("/")

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

    response = _client(_queue(qualified, audit)).get("/")

    assert response.text.index("Applied AI Engineer 2") < response.text.index(
        "Applied AI Engineer 1"
    )


def test_pending_rows_carry_the_lane_and_link_to_the_job_page() -> None:
    item = _item(TODAY, "qualified")
    audit = _item(TODAY, "rejected_audit", value=2)

    response = _client(_queue(item, audit)).get("/")

    assert "New result" in response.text
    assert "Second look" in response.text
    assert "Rejected audit" not in response.text
    assert "Applied AI Engineer 1" in response.text
    assert "Applied AI Engineer 2" in response.text
    assert f'href="/review/item/{item.id}"' in response.text
    assert "Acme · Remote" in response.text
    assert "<script" not in response.text


def test_an_empty_queue_renders_a_single_message() -> None:
    response = _client(_queue()).get("/")

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
    assert 'href="/"' in response.text
    assert "2 of 3 waiting" in response.text
    assert 'class="workbench"' in response.text
    assert 'class="evidence-panel"' in response.text
    assert 'class="decision-panel"' in response.text
    assert f'href="/review/item/{first.id}"' in response.text
    assert f'href="/review/item/{last.id}"' in response.text
    assert "← Prev" in response.text
    assert "Next →" in response.text


def test_the_login_uses_the_editorial_split_and_route_line() -> None:
    client = TestClient(
        create_review_app(
            lambda: _queue(),
            SETTINGS,
            submit_review=_default_submit_review,
            owner_access_service=OWNER_ACCESS,
            now=lambda: NOW,
        )
    )

    response = client.get("/login")

    assert response.status_code == 200
    assert 'class="login-editorial"' in response.text
    assert 'class="login-card"' in response.text
    assert "Review the work worth doing." in response.text
    assert "Discover" in response.text
    assert "Filter" in response.text
    assert "Evaluate" in response.text
    assert "Review" in response.text
    assert "<link" not in response.text
    assert "<script" not in response.text


def test_the_theme_follows_the_system_color_scheme() -> None:
    response = _client(_queue()).get("/")

    assert '<meta name="color-scheme" content="light dark">' in response.text
    assert "prefers-color-scheme: dark" in response.text


def test_a_job_page_shows_the_existing_job_metadata() -> None:
    item = _item(TODAY, "qualified")

    response = _client(_queue(item)).get(f"/review/item/{item.id}")

    assert response.status_code == 200
    assert 'aria-label="Job metadata"' in response.text
    assert "Posted" in response.text
    assert "Sep 9, 2026" in response.text
    assert "Source" in response.text
    assert "Other" in response.text
    assert "Profile" in response.text
    assert "applied ai product engineer" in response.text


def test_a_job_page_marks_every_decision_as_not_recorded_before_review() -> None:
    item = _item(TODAY, "qualified")

    response = _client(_queue(item)).get(f"/review/item/{item.id}")

    assert re.findall(r'<button[^>]+aria-pressed="(true|false)"', response.text) == [
        "false",
        "false",
        "false",
    ]


def test_a_job_page_names_the_lane_and_why_it_is_here() -> None:
    audit = _item(TODAY, "rejected_audit")

    response = _client(_queue(audit)).get(f"/review/item/{audit.id}")

    assert "Second look" in response.text
    assert "Open original listing" in response.text
    assert "Why it's here" in response.text
    assert "Strong product delivery fit." in response.text


def test_a_job_page_shows_the_compensation_card_between_body_and_notes() -> None:
    item = _item(TODAY, "qualified")
    paid = item.model_copy(
        update={
            "job": item.job.model_copy(
                update={
                    "compensation": Compensation(
                        minimum=Decimal("80000"),
                        maximum=Decimal("100000"),
                        currency="EUR",
                        period="year",
                        source="ats",
                    )
                }
            )
        }
    )

    response = _client(_queue(paid)).get(f"/review/item/{paid.id}")

    assert response.status_code == 200
    assert "Compensation" in response.text
    assert "€80,000 – €100,000" in response.text
    assert "per year" in response.text
    assert "from the ATS" in response.text
    assert response.text.index('class="compensation-card"') < response.text.index('name="note"')


def test_a_job_page_omits_the_compensation_card_when_unknown() -> None:
    item = _item(TODAY, "qualified")

    response = _client(_queue(item)).get(f"/review/item/{item.id}")

    assert response.status_code == 200
    assert 'class="compensation-card"' not in response.text


def test_a_job_page_renders_the_decision_form_for_a_queued_item() -> None:
    item = _item(TODAY, "qualified")

    response = _client(_queue(item)).get(f"/review/item/{item.id}")

    assert response.status_code == 200
    assert (
        f'<form enctype="multipart/form-data" action="/review/{item.id}" method="post">'
        in response.text
    )
    assert re.search(r'name="csrf_token" value="[^"]+"', response.text) is not None
    assert 'name="evaluation_id" value="0000' in response.text
    assert 'name="snapshot_id" value="0000' in response.text
    assert 'name="note"' in response.text
    assert "maxlength" not in response.text
    assert "Pursue" in response.text
    assert "Unsure" in response.text
    assert "Reject" in response.text
    assert 'name="review_day"' not in response.text


def test_a_day_section_renders_reviewed_jobs_after_the_waiting_ones() -> None:
    waiting = _item(TODAY, "qualified", value=1)
    decided = _decided(_item(TODAY, "qualified", value=2), "reject", "Wrong location.")
    queue = ReviewQueue(items=(waiting,), reviewed_items=(decided,), reviewed_counts={TODAY: 1})

    response = _client(queue).get("/")

    assert response.status_code == 200
    assert "1 waiting · 1 reviewed" in response.text
    assert "Reviewed (1)" in response.text
    assert '<span class="chip decision-chip">reject</span>' in response.text
    assert 'class="job-list-item reviewed-item"' in response.text
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

    response = _client(queue).get("/")

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
    assert 'value="pursue" aria-pressed="false"' in response.text
    assert 'value="reject" aria-pressed="false"' in response.text
    assert re.findall(r'<button[^>]+aria-pressed="(true|false)"', response.text) == [
        "false",
        "true",
        "false",
    ]
    assert 'class="workbench"' in response.text


def test_submitting_a_revision_returns_to_the_item_page_with_the_update() -> None:
    item = _decided(_item(TODAY, "qualified", value=1), "pursue")
    decided = [item]

    def submit(review: ReviewSubmission) -> ReviewSubmitResult:
        decided[0] = decided[0].model_copy(
            update={"decision": review.decision, "note": review.note}
        )
        return ReviewSaved(review_event_id=UUID(int=9))

    def load_queue() -> ReviewQueue:
        return ReviewQueue(reviewed_items=(decided[0],), reviewed_counts={TODAY: 1})

    client = TestClient(
        create_review_app(
            load_queue,
            SETTINGS,
            submit_review=submit,
            owner_access_service=OWNER_ACCESS,
            now=lambda: NOW,
        )
    )
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
        submit=lambda review: (
            submissions.append(review) or ReviewSaved(review_event_id=UUID(int=9))
        ),
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
    assert response.headers["location"] == "/"
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


def test_submits_a_note_longer_than_the_old_limit_unchanged() -> None:
    submissions: list[ReviewSubmission] = []
    item = _item(TODAY, "qualified")
    note = " " + "x" * 1999 + " "
    client = _client(
        _queue(item),
        submit=lambda review: (
            submissions.append(review) or ReviewSaved(review_event_id=UUID(int=9))
        ),
    )

    response = client.post(
        f"/review/{item.id}",
        data=_form(item, client) | {"note": note},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert len(note) == 2001
    assert submissions[0].note == note


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
        submit=lambda review: (
            submissions.append(review) or ReviewSaved(review_event_id=UUID(int=9))
        ),
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
        submit=lambda review: (
            submissions.append(review) or ReviewSaved(review_event_id=UUID(int=9))
        ),
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
        submit=lambda review: (
            submissions.append(review) or ReviewSaved(review_event_id=UUID(int=9))
        ),
    )

    response = client.post(
        f"/review/{item.id}",
        data={key: value for key, value in _form(item, client).items() if key != "csrf_token"},
    )

    assert response.status_code == 409
    assert "form expired" in response.text
    assert submissions == []


def test_rejects_an_invalid_review_decision_without_calling_the_service() -> None:
    submissions: list[ReviewSubmission] = []
    item = _item(TODAY, "qualified")
    client = _client(
        _queue(item),
        submit=lambda review: (
            submissions.append(review) or ReviewSaved(review_event_id=UUID(int=9))
        ),
    )

    response = client.post(f"/review/{item.id}", data=_form(item, client) | {"decision": "later"})

    assert response.status_code == 409
    assert "This review form is invalid" in response.text
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


def test_renders_submit_database_failure_as_uncertain_result(
    caplog: pytest.LogCaptureFixture,
) -> None:
    item = _item(TODAY, "qualified")

    def unavailable(_review: ReviewSubmission) -> ReviewSaved:
        raise psycopg.OperationalError("password=secret-value")

    client = _client(_queue(item), submit=unavailable)

    response = client.post(f"/review/{item.id}", data=_form(item, client))

    assert response.status_code == 503
    assert "Review result is unknown" in response.text
    assert "Check review queue" in response.text
    assert "submit review failed (OperationalError)" in caplog.text
    assert "secret-value" not in caplog.text


@pytest.mark.parametrize("path", ["/", f"/review/item/{UUID(int=1)}"])
def test_review_pages_render_queue_database_failure_as_retryable_unavailable(
    path: str, caplog: pytest.LogCaptureFixture
) -> None:
    def unavailable() -> ReviewQueue:
        raise psycopg.OperationalError("password=secret-value")

    app = create_review_app(
        unavailable,
        SETTINGS,
        submit_review=_default_submit_review,
        owner_access_service=OWNER_ACCESS,
        now=lambda: NOW,
    )
    client = TestClient(app)
    _authenticate(client)

    response = client.get(path)

    assert response.status_code == 503
    assert "Review is unavailable" in response.text
    assert "Retry" in response.text
    assert "previous decisions are unchanged" in response.text
    assert "OperationalError" in caplog.text
    assert "secret-value" not in caplog.text
