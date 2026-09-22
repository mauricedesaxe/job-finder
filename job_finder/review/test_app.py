from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
import re
from typing import Never, cast
from uuid import UUID

import psycopg
import pytest
from starlette.testclient import TestClient

from job_finder.config import ReviewAppSettings
from job_finder.review.app import create_review_app
from job_finder.review.configuration_editor import ConfigurationEditorService
from job_finder.review.control_plane import (
    CONTROL_DEFINITIONS,
    ControlConflict,
    ControlPlaneService,
    ControlPlaneSnapshot,
    RunLaunchUncertain,
    RunNowCommand,
    RunNowResult,
    RunStarted,
    ScheduleChangeCommand,
    ScheduleChangeResult,
    ScheduleChanged,
    ScheduleStateConflict,
    ScheduleStatus,
    ScheduleView,
    unavailable_control_plane_service,
)
from job_finder.review.models import (
    Compensation,
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
from job_finder.review.operations import (
    FailureSample,
    OperationsHealth,
    OperationsService,
    OperationsSnapshot,
    PipelineRunSummary,
    QueueCounts,
    SpendSummary,
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


def test_the_authenticated_home_shows_truthful_owner_operations_status() -> None:
    snapshot = OperationsSnapshot(
        health=OperationsHealth.ACTION_REQUIRED,
        queues=QueueCounts(pending=2, leased=1, retrying=3, completed=20, terminal_error=1),
        spend=SpendSummary(known_usd=Decimal("1.2345"), unknown_attempts=4),
        recent_runs=(
            PipelineRunSummary(
                id=UUID(int=20),
                kind="orchestration",
                status="failed",
                started_at=NOW,
                completed_at=NOW,
            ),
        ),
        failures=(
            FailureSample(
                source="job",
                occurred_at=NOW,
                summary="provider_timeout: OpenRouter did not respond",
            ),
        ),
    )
    client = _client(_queue(), operations=OperationsService(load=lambda: snapshot))

    response = client.get("/")

    assert response.status_code == 200
    assert "Action required" in response.text
    assert "2 pending" in response.text
    assert "1 leased" in response.text
    assert "3 retrying" in response.text
    assert "20 completed" in response.text
    assert "1 terminal error" in response.text
    assert "pendings" not in response.text
    assert "leaseds" not in response.text
    assert "retryings" not in response.text
    assert "completeds" not in response.text
    assert "$1.2345" in response.text
    assert "4 attempts have no recorded cost" in response.text
    assert "OpenRouter did not respond" in response.text
    assert "Dagster could not be reached." in response.text
    assert len(re.findall(r"<button[^>]+disabled", response.text)) == 8
    assert 'aria-current="page">Operations' in response.text
    assert 'href="/review"' in response.text
    assert 'href="/configuration"' in response.text


def test_the_operations_home_requires_the_existing_owner_session() -> None:
    app = create_review_app(
        ReviewService(review_queue=lambda: _queue(), submit=_saved),
        _configuration_service(),
        SETTINGS,
        now=lambda: NOW,
    )

    response = TestClient(app).get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2F"


def test_the_operations_home_reports_database_unavailability() -> None:
    def unavailable() -> OperationsSnapshot:
        raise psycopg.OperationalError("offline")

    client = _client(_queue(), operations=OperationsService(load=unavailable))

    response = client.get("/")

    assert response.status_code == 503
    assert "Operations status is unavailable" in response.text


@pytest.mark.parametrize(
    ("snapshot", "heading"),
    [
        (
            OperationsSnapshot(
                health=OperationsHealth.UNKNOWN,
                queues=QueueCounts(),
                spend=SpendSummary(known_usd=Decimal("0"), unknown_attempts=0),
                recent_runs=(),
                failures=(),
            ),
            "Status unknown",
        ),
        (
            OperationsSnapshot(
                health=OperationsHealth.WORKING,
                queues=QueueCounts(pending=1),
                spend=SpendSummary(known_usd=Decimal("0"), unknown_attempts=0),
                recent_runs=(
                    PipelineRunSummary(
                        id=UUID(int=7),
                        kind="processing",
                        status="running",
                        started_at=NOW,
                        completed_at=None,
                    ),
                ),
                failures=(),
            ),
            "Work is in progress",
        ),
        (
            OperationsSnapshot(
                health=OperationsHealth.CAUGHT_UP,
                queues=QueueCounts(),
                spend=SpendSummary(known_usd=Decimal("0"), unknown_attempts=0),
                recent_runs=(
                    PipelineRunSummary(
                        id=UUID(int=8),
                        kind="processing",
                        status="completed",
                        started_at=NOW,
                        completed_at=NOW,
                    ),
                ),
                failures=(),
            ),
            "Caught up",
        ),
    ],
)
def test_the_owner_home_renders_each_operations_health_state(
    snapshot: OperationsSnapshot, heading: str
) -> None:
    client = _client(_queue(), operations=OperationsService(load=lambda: snapshot))

    response = client.get("/")

    assert response.status_code == 200
    assert heading in response.text
    assert "No recent failures recorded." in response.text
    if snapshot.recent_runs:
        assert snapshot.recent_runs[0].status in response.text
    else:
        assert "No pipeline runs recorded." in response.text


def test_the_operations_home_renders_all_live_schedule_controls() -> None:
    client = _client(_queue(), controls=_control_service())

    response = client.get("/")

    assert response.status_code == 200
    for definition in CONTROL_DEFINITIONS:
        assert definition.label in response.text
        assert definition.cadence in response.text
        assert f'value="{definition.job_name}"' in response.text
        assert f'value="{definition.schedule_name}"' in response.text
    assert "Next: 2026-09-21 13:00 UTC" in response.text
    assert response.text.count('action="/operations/run"') == 4
    assert response.text.count('action="/operations/schedule"') == 4


def test_run_now_requires_csrf_before_calling_the_control_service() -> None:
    client = _client(_queue(), controls=_control_service())

    response = client.post(
        "/operations/run",
        data={"job_name": "job_finder", "idempotency_key": "private-key"},
    )

    assert response.status_code == 403
    assert "This operations form expired" in response.text


@pytest.mark.parametrize("path", ["/operations/run", "/operations/schedule"])
def test_operations_actions_require_the_owner_session_before_service_calls(path: str) -> None:
    app = create_review_app(
        ReviewService(review_queue=lambda: _queue(), submit=_saved),
        _configuration_service(),
        SETTINGS,
        control_service=_control_service(),
        now=lambda: NOW,
    )

    response = TestClient(app).post(path, data={}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")


def test_run_now_rejects_a_malformed_form_without_calling_the_service() -> None:
    client = _client(_queue(), controls=_control_service())

    response = client.post(
        "/operations/run",
        data={"csrf_token": _csrf(client), "idempotency_key": "private-key"},
    )

    assert response.status_code == 400
    assert "Malformed operations form" in response.text


def test_run_now_redirects_and_the_home_renders_the_allowlisted_notice() -> None:
    controls = _control_service(run_now=lambda _command: RunStarted("run-1", False))
    client = _client(_queue(), controls=controls)

    response = client.post(
        "/operations/run",
        data={
            "csrf_token": _csrf(client),
            "job_name": "job_finder",
            "idempotency_key": "private-key",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Run submitted to Dagster." in response.text


def test_uncertain_run_renders_an_exact_retry_form_with_the_same_private_key() -> None:
    controls = _control_service(run_now=lambda _command: RunLaunchUncertain("No confirmation"))
    client = _client(_queue(), controls=controls)

    response = client.post(
        "/operations/run",
        data={
            "csrf_token": _csrf(client),
            "job_name": "job_finder",
            "idempotency_key": "the-same-private-key",
        },
    )

    assert response.status_code == 503
    assert 'action="/operations/run"' in response.text
    assert response.text.count('name="idempotency_key" value="the-same-private-key"') == 1
    assert 'name="job_name" value="job_finder"' in response.text


def test_run_now_integrity_conflict_is_reported_as_conflict() -> None:
    controls = _control_service(run_now=lambda _command: ControlConflict("Duplicate runs"))
    client = _client(_queue(), controls=controls)

    response = client.post(
        "/operations/run",
        data={
            "csrf_token": _csrf(client),
            "job_name": "job_finder",
            "idempotency_key": "private-key",
        },
    )

    assert response.status_code == 409
    assert "Duplicate runs" in response.text


def test_schedule_change_reports_stale_state_without_redirecting() -> None:
    controls = _control_service(
        change_schedule=lambda _command: ScheduleStateConflict(
            expected=ScheduleStatus.RUNNING,
            observed=ScheduleStatus.STOPPED,
        )
    )
    client = _client(_queue(), controls=controls)

    response = client.post(
        "/operations/schedule",
        data={
            "csrf_token": _csrf(client),
            "schedule_name": "job_finder_schedule",
            "expected_state": "RUNNING",
            "desired_state": "STOPPED",
        },
    )

    assert response.status_code == 409
    assert "Expected RUNNING; observed STOPPED" in response.text


def test_schedule_change_redirects_after_a_verified_pause() -> None:
    controls = _control_service(
        change_schedule=lambda _command: ScheduleChanged(
            status=ScheduleStatus.STOPPED, replayed=False
        )
    )
    client = _client(_queue(), controls=controls)

    response = client.post(
        "/operations/schedule",
        data={
            "csrf_token": _csrf(client),
            "schedule_name": "job_finder_schedule",
            "expected_state": "RUNNING",
            "desired_state": "STOPPED",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/?notice=schedule-paused"


def test_schedule_resume_redirects_and_the_home_renders_the_notice() -> None:
    controls = _control_service(
        change_schedule=lambda _command: ScheduleChanged(
            status=ScheduleStatus.RUNNING, replayed=False
        )
    )
    client = _client(_queue(), controls=controls)

    response = client.post(
        "/operations/schedule",
        data={
            "csrf_token": _csrf(client),
            "schedule_name": "job_finder_schedule",
            "expected_state": "STOPPED",
            "desired_state": "RUNNING",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Schedule resumed." in response.text


@pytest.mark.parametrize(
    ("path", "form"),
    [
        (
            "/operations/run",
            {"job_name": "job_finder", "idempotency_key": "private-key"},
        ),
        (
            "/operations/schedule",
            {
                "schedule_name": "job_finder_schedule",
                "expected_state": "RUNNING",
                "desired_state": "STOPPED",
            },
        ),
    ],
)
def test_operations_actions_report_control_plane_unavailability(
    path: str, form: dict[str, str]
) -> None:
    client = _client(_queue(), controls=unavailable_control_plane_service())

    response = client.post(path, data={"csrf_token": _csrf(client), **form}, follow_redirects=False)

    assert response.status_code == 503
    assert "Dagster control is unavailable" in response.text


def test_schedule_change_rejects_an_unknown_state_without_calling_the_service() -> None:
    client = _client(_queue(), controls=_control_service())

    response = client.post(
        "/operations/schedule",
        data={
            "csrf_token": _csrf(client),
            "schedule_name": "job_finder_schedule",
            "expected_state": "PAUSED",
            "desired_state": "STOPPED",
        },
    )

    assert response.status_code == 400
    assert "Malformed operations form" in response.text


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
            ReviewService(review_queue=lambda: _queue(), submit=_saved),
            _configuration_service(),
            SETTINGS,
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
    response = _client(_queue()).get("/review")

    assert '<meta name="color-scheme" content="light dark">' in response.text
    assert "color-scheme: light dark" in response.text
    assert "@media (prefers-color-scheme: dark)" in response.text


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

    response = _client(queue).get("/review")

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

    service = ReviewService(
        review_queue=lambda: ReviewQueue(reviewed_items=(decided[0],), reviewed_counts={TODAY: 1}),
        submit=submit,
    )
    client = TestClient(
        create_review_app(service, _configuration_service(), SETTINGS, now=lambda: NOW)
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


def test_submits_a_note_longer_than_the_old_limit_unchanged() -> None:
    submissions: list[ReviewSubmission] = []
    item = _item(TODAY, "qualified")
    note = " " + "x" * 1999 + " "
    client = _client(
        _queue(item),
        submit=lambda review: submissions.append(review)
        or ReviewSaved(review_event_id=UUID(int=9)),
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
        _configuration_service(),
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
            _configuration_service(),
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
            _configuration_service(),
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
        _configuration_service(),
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
            _configuration_service(),
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


def _client(
    queue: ReviewQueue,
    submit: Submitter = _saved,
    *,
    operations: OperationsService | None = None,
    controls: ControlPlaneService | None = None,
) -> TestClient:
    service = ReviewService(review_queue=lambda: queue, submit=submit)
    client = TestClient(
        create_review_app(
            service,
            _configuration_service(),
            SETTINGS,
            operations_service=operations,
            control_service=controls,
            now=lambda: NOW,
        )
    )
    _authenticate(client)
    return client


def _draining_client(remaining: list[ReviewItem], submit: Submitter) -> TestClient:
    service = ReviewService(review_queue=lambda: ReviewQueue(items=tuple(remaining)), submit=submit)
    client = TestClient(
        create_review_app(service, _configuration_service(), SETTINGS, now=lambda: NOW)
    )
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


def _configuration_service() -> ConfigurationEditorService:
    def unused(*_args: object) -> Never:
        raise AssertionError("configuration service was not expected")

    never = cast(Callable[..., Never], unused)
    return ConfigurationEditorService(
        inspect=never,
        validate=never,
        preview=never,
        save=never,
        publish=never,
        activate=never,
    )


def _control_service(
    *,
    run_now: Callable[[RunNowCommand], RunNowResult] | None = None,
    change_schedule: Callable[[ScheduleChangeCommand], ScheduleChangeResult] | None = None,
) -> ControlPlaneService:
    snapshot = ControlPlaneSnapshot(
        schedules=tuple(
            ScheduleView(
                definition=definition,
                status=ScheduleStatus.RUNNING,
                next_tick=datetime(2026, 9, 21, 13, tzinfo=UTC),
            )
            for definition in CONTROL_DEFINITIONS
        )
    )

    def unexpected(*_args: object) -> Never:
        raise AssertionError("control operation was not expected")

    return ControlPlaneService(
        load=lambda: snapshot,
        run_now=cast(Callable[..., Never], unexpected) if run_now is None else run_now,
        change_schedule=cast(Callable[..., Never], unexpected)
        if change_schedule is None
        else change_schedule,
    )
