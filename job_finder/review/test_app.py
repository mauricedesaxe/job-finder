from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from http.cookies import SimpleCookie
import re
from typing import Never, cast
from uuid import UUID

import psycopg
import pytest
from pydantic import SecretStr
from starlette.routing import Route
from starlette.testclient import TestClient

from job_finder.config import ReviewAppSettings
from job_finder.execution_budget import (
    BudgetSaved,
    BudgetSetupService,
    BudgetSetupState,
    ExecutionBudgetPolicy,
    ExecutionEstimate,
)
from job_finder.provider_credentials import (
    ProviderCapability,
    ProviderCredentialState,
    ProviderCredentialStored,
    ProviderKind,
    ProviderSetupService,
    ProviderSetupSnapshot,
    ProviderStageAdvanced,
)
from job_finder.review.analytics import (
    AnalyticsService,
    DayModelSpend,
    DaySpend,
    ModelSpend,
    SpendAnalytics,
)
from job_finder.review.app import create_review_app
from job_finder.review.configuration_editor import ConfigurationEditorService
from job_finder.review.control_plane import (
    CONTROL_DEFINITIONS,
    ControlConflict,
    ControlPlaneService,
    ControlPlaneSnapshot,
    ControlPlaneUnavailable,
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
from job_finder.review.feedback import (
    ReviewConflict,
    ReviewFeedbackService,
    ReviewSaved,
    ReviewSubmission,
    ReviewSubmitResult,
)
from job_finder.review.queue import (
    Compensation,
    ReviewDecision,
    ReviewItem,
    ReviewJob,
    ReviewLane,
    ReviewQueue,
    ReviewQueueService,
)
from job_finder.review.operations import (
    ActivityEntry,
    ActivityPage,
    ActivityQuery,
    ActivityRun,
    ActivityService,
    ActivityWork,
    JobReevaluationAccepted,
    JobReevaluationCommand,
    JobReevaluationReceipt,
    JobReevaluationResult,
    OperationsHealth,
    OperationsService,
    OperationsSnapshot,
    PipelineRunStatus,
    QueueCounts,
    RecoveryAction,
    RunAttemptSummary,
    RecoveryOutcome,
    SpendSummary,
    WorkDismissalApplied,
    WorkDismissalCommand,
    WorkDismissalReceipt,
    RunDetail,
    RunListItem,
    RunsService,
    WorkRecoveryApplied,
    WorkRecoveryCommand,
    WorkRecoveryReceipt,
    WorkRecoveryResult,
    WorkRecoveryStaleState,
    WorkAttemptSummary,
    WorkItemNotFound,
    WorkItemDetail,
    WorkItemState,
)
from job_finder.review.owner_access import (
    OnboardingStage,
    OwnerAccessService,
    OwnerAccessState,
    OwnerBootstrapped,
    OwnerBootstrapConflict,
)

TODAY = date(2026, 9, 10)
YESTERDAY = date(2026, 9, 9)
NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
SETTINGS = ReviewAppSettings(
    bootstrap_token=SecretStr("bootstrap-token-with-at-least-32-characters"),
    session_secret="s" * 32,
    cookie_secure=False,
)
OWNER_PASSWORD = "correct horse battery staple"
BOOTSTRAP_TOKEN = "bootstrap-token-with-at-least-32-characters"
OWNER_STATE = OwnerAccessState(stage=OnboardingStage.COMPLETE, has_password=True)
OWNER_ACCESS = OwnerAccessService(
    load_state=lambda: OWNER_STATE,
    authenticate=lambda password: password == OWNER_PASSWORD,
    bootstrap=lambda _password: OwnerBootstrapConflict(OWNER_STATE),
)
DEFAULT_FEEDBACK_SERVICE = ReviewFeedbackService(
    submit=lambda _review: ReviewSaved(review_event_id=UUID(int=9))
)

Submitter = Callable[[ReviewSubmission], ReviewSubmitResult]


def test_http_route_manifest_stays_stable() -> None:
    app = create_review_app(
        ReviewQueueService(review_queue=lambda: _queue()),
        _configuration_service(),
        SETTINGS,
        feedback_service=DEFAULT_FEEDBACK_SERVICE,
        owner_access_service=OWNER_ACCESS,
        now=lambda: NOW,
    )
    get_paths = {
        "/",
        "/configuration",
        "/favicon.ico",
        "/healthz",
        "/login",
        "/operations",
        "/operations/analytics",
        "/operations/control",
        "/operations/failures",
        "/operations/runs",
        "/operations/runs/{run_id}",
        "/operations/work/{job_id}",
        "/readyz",
        "/review",
        "/review/item/{review_item_id}",
        "/setup",
        "/setup/budget",
        "/setup/providers",
        "/setup/test-search",
        "/static/{name}",
    }
    post_paths = {
        "/configuration/activate",
        "/configuration/draft",
        "/configuration/edit",
        "/configuration/preview",
        "/configuration/publish",
        "/login",
        "/logout",
        "/operations/dismiss",
        "/operations/recovery",
        "/operations/reevaluation",
        "/operations/run",
        "/operations/schedule",
        "/review/{review_item_id}",
        "/setup",
        "/setup/budget",
        "/setup/providers",
        "/setup/providers/continue",
    }
    expected = sorted(
        [(method, path) for path in get_paths for method in ("GET", "HEAD")]
        + [("POST", path) for path in post_paths]
    )
    routes = [route for route in app.routes if isinstance(route, Route)]
    assert len(routes) == len(app.routes)
    assert all(route.methods is not None for route in routes)
    actual = sorted((method, route.path) for route in routes for method in route.methods or ())

    assert actual == expected
    assert sorted((route.path, route.name) for route in routes) == sorted(
        [
            ("/healthz", "create_review_app_healthz"),
            ("/readyz", "create_review_app_readyz"),
            ("/favicon.ico", "create_review_app_favicon"),
            ("/static/{name}", "create_review_app_static_asset"),
            ("/setup", "create_review_app_setup_form"),
            ("/setup", "create_review_app_setup_submit"),
            ("/setup/providers", "create_review_app_provider_setup_form"),
            ("/setup/providers", "create_review_app_provider_setup_submit"),
            ("/setup/providers/continue", "create_review_app_provider_setup_continue"),
            ("/login", "create_review_app_login_form"),
            ("/setup/budget", "create_review_app_budget_setup_form"),
            ("/setup/budget", "create_review_app_budget_setup_submit"),
            ("/setup/test-search", "create_review_app_test_search_setup"),
            ("/login", "create_review_app_login_submit"),
            ("/", "create_review_app_home"),
            ("/review", "create_review_app_review_page"),
            ("/operations", "create_review_app_operations_page"),
            ("/operations/control", "create_review_app_control_plane_page"),
            ("/operations/run", "create_review_app_run_operation"),
            ("/operations/schedule", "create_review_app_change_schedule"),
            ("/operations/recovery", "create_review_app_recover_operation"),
            ("/operations/runs", "create_review_app_pipeline_runs_page"),
            ("/operations/runs/{run_id}", "create_review_app_run_detail_page"),
            ("/operations/work/{job_id}", "create_review_app_work_item_page"),
            ("/operations/failures", "create_review_app_failures_page"),
            ("/operations/analytics", "create_review_app_spend_analytics_page"),
            ("/operations/dismiss", "create_review_app_dismiss_operation"),
            ("/operations/reevaluation", "create_review_app_request_reevaluation"),
            ("/configuration", "create_review_app_configuration"),
            ("/configuration/edit", "create_review_app_edit_configuration"),
            ("/configuration/preview", "create_review_app_preview_configuration"),
            ("/configuration/draft", "create_review_app_save_configuration"),
            ("/configuration/publish", "create_review_app_publish_configuration"),
            ("/configuration/activate", "create_review_app_activate_configuration"),
            ("/review/{review_item_id}", "create_review_app_submit_review"),
            ("/review/item/{review_item_id}", "create_review_app_review_item_page"),
            ("/logout", "create_review_app_logout"),
        ]
    )


def test_security_and_session_middleware_contract_stays_stable() -> None:
    secure_settings = ReviewAppSettings(
        bootstrap_token=SecretStr(BOOTSTRAP_TOKEN),
        session_secret="s" * 32,
        cookie_secure=True,
    )
    app = create_review_app(
        ReviewQueueService(review_queue=lambda: _queue()),
        _configuration_service(),
        secure_settings,
        feedback_service=DEFAULT_FEEDBACK_SERVICE,
        owner_access_service=OWNER_ACCESS,
        now=lambda: NOW,
    )
    client = TestClient(app, base_url="https://testserver")

    health = client.get("/healthz")
    accepted = client.post(
        "/login",
        data={"password": OWNER_PASSWORD, "next": "/"},
        follow_redirects=False,
    )
    cookie = SimpleCookie()
    cookie.load(accepted.headers["set-cookie"])
    session = cookie["job_finder_review_session"]

    assert {
        name: health.headers[name]
        for name in (
            "cache-control",
            "content-security-policy",
            "referrer-policy",
            "strict-transport-security",
            "x-content-type-options",
            "x-frame-options",
        )
    } == {
        "cache-control": "no-store",
        "content-security-policy": "default-src 'self'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'",
        "referrer-policy": "no-referrer",
        "strict-transport-security": "max-age=63072000; includeSubDomains",
        "x-content-type-options": "nosniff",
        "x-frame-options": "DENY",
    }
    assert "set-cookie" not in health.headers
    assert accepted.status_code == 303
    assert session["path"] == "/"
    assert session["max-age"] == "1209600"
    assert session["httponly"] is True
    assert session["samesite"] == "lax"
    assert session["secure"] is True
    assert client.get("/").status_code == 200


def test_the_operations_entry_point_redirects_to_recent_activity() -> None:
    client = _client(_queue())

    response = client.get("/operations", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/operations/runs"


def test_operations_pages_mark_their_shell_sections() -> None:
    snapshot = OperationsSnapshot(
        health=OperationsHealth.UNKNOWN,
        queues=QueueCounts(completed=2),
        spend=SpendSummary(known_usd=Decimal(0), unknown_attempts=0),
        recent_runs=(),
        failures=(),
    )
    client = _client(
        _queue(),
        operations=OperationsService(load=lambda: snapshot),
        activity=_activity_service(_activity_run_entry(value=1))[0],
    )

    runs = client.get("/operations/runs")
    failures = client.get("/operations/failures", follow_redirects=False)

    assert re.search(_shell_link("/operations/runs", "Operations", current=True), runs.text)
    assert re.search(_shell_link("/", "Review"), runs.text)
    assert re.search(_shell_link("/configuration", "Search setup"), runs.text)
    assert re.search(_shell_link("/operations/runs", "Recent activity", current=True), runs.text)
    assert "just now" in runs.text
    assert 'title="2026-09-10 12:00 UTC"' in runs.text
    assert failures.status_code == 303
    assert (
        failures.headers["location"]
        == "/operations/runs?status=failed&status=retrying&status=terminal"
    )
    assert 'href="/operations/failures"' not in runs.text


def test_the_review_queue_is_the_landing_page() -> None:
    item = _item(TODAY, "qualified")

    response = _client(_queue(item)).get("/")

    assert response.status_code == 200
    assert "Jobs waiting for review" in response.text
    assert 'href="/" aria-current="page" class="shell-link">Review</a>' in response.text


def test_the_old_review_url_redirects_to_the_landing_page() -> None:
    def unexpected_queue_load() -> Never:
        raise AssertionError("legacy redirect must not load the review queue")

    client = TestClient(
        create_review_app(
            ReviewQueueService(review_queue=unexpected_queue_load),
            _configuration_service(),
            SETTINGS,
            feedback_service=DEFAULT_FEEDBACK_SERVICE,
            owner_access_service=OWNER_ACCESS,
            now=lambda: NOW,
        )
    )
    _authenticate(client)

    response = client.get("/review", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_the_operations_home_requires_the_existing_owner_session() -> None:
    app = create_review_app(
        ReviewQueueService(review_queue=lambda: _queue()),
        _configuration_service(),
        SETTINGS,
        feedback_service=DEFAULT_FEEDBACK_SERVICE,
        owner_access_service=OWNER_ACCESS,
        now=lambda: NOW,
    )

    response = TestClient(app).get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2F"


_CONTROL_DESCRIPTION_MATCHES = {
    "job_finder": "registers what it finds in the work queue",
    "job_work_queue": "claims due jobs",
    "onboarding_test_search": "bounded test search requested during setup",
    "review_sample": "a sample of yesterday's rejected jobs",
    "langfuse_projection": "Ships telemetry to Langfuse",
}


def test_the_control_plane_page_renders_all_live_schedule_controls() -> None:
    client = _client(_queue(), controls=_control_service())

    response = client.get("/operations/control")

    assert response.status_code == 200
    for definition in CONTROL_DEFINITIONS:
        assert definition.label in response.text
        assert definition.cadence in response.text
        assert _CONTROL_DESCRIPTION_MATCHES[definition.job_name] in response.text
        assert f'value="{definition.job_name}"' in response.text
        assert f'value="{definition.schedule_name}"' in response.text
    assert "From search to decision" in response.text
    assert "two runs can never work on the same job" in response.text
    assert "a job found this morning is not decided tomorrow" in response.text
    assert "in 11 days" in response.text
    assert 'title="2026-09-21 13:00 UTC"' in response.text
    assert response.text.count('action="/operations/run"') == 5
    assert response.text.count('action="/operations/schedule"') == 5


def test_the_control_plane_page_names_a_missing_configuration() -> None:
    client = _client(_queue())

    response = client.get("/operations/control")

    assert response.status_code == 200
    assert "Dagster is not configured for this app" in response.text
    assert "JOB_FINDER_DAGSTER_GRAPHQL_URL" in response.text
    assert response.text.count("schedule-state unavailable") == 5
    assert len(re.findall(r"<button[^>]+disabled", response.text)) == 10


def _unexpected_control_call(*_args: object) -> Never:
    raise AssertionError("control operation was not expected")


def test_the_control_plane_page_reports_an_unreachable_dagster() -> None:
    def unavailable() -> ControlPlaneSnapshot:
        raise ControlPlaneUnavailable("Dagster GraphQL request failed")

    controls = ControlPlaneService(
        load=unavailable,
        run_now=_unexpected_control_call,
        change_schedule=_unexpected_control_call,
    )
    client = _client(_queue(), controls=controls)

    response = client.get("/operations/control")

    assert response.status_code == 200
    assert "this app cannot reach Dagster" in response.text
    assert "Dagster GraphQL request failed" in response.text
    assert "Schedule controls are off" in response.text
    assert "Pipeline evidence elsewhere stays current" in response.text


def test_run_now_requires_csrf_before_calling_the_control_service() -> None:
    calls: list[RunNowCommand] = []
    controls = _control_service(
        run_now=lambda command: calls.append(command) or RunStarted("x", False)
    )
    client = _client(_queue(), controls=controls)

    response = client.post(
        "/operations/run",
        data={"job_name": "job_finder", "idempotency_key": "private-key"},
    )

    assert response.status_code == 403
    assert "This operations form expired" in response.text
    assert calls == []


@pytest.mark.parametrize(
    "path",
    [
        "/operations/run",
        "/operations/schedule",
        "/operations/recovery",
        "/operations/dismiss",
        "/operations/reevaluation",
    ],
)
def test_operations_actions_reject_a_duplicated_csrf_field_before_service_calls(
    path: str,
) -> None:
    calls: list[object] = []
    controls = _control_service(
        run_now=lambda command: calls.append(command) or RunStarted("x", False),
        change_schedule=lambda command: calls.append(command)
        or ScheduleChanged(ScheduleStatus.STOPPED, False),
    )
    operations = OperationsService(
        load=lambda: _operations_snapshot(),
        recover=lambda command: calls.append(command) or _applied_recovery(command),
        reevaluate=lambda command: calls.append(command) or _accepted_reevaluation(command),
        dismiss=lambda command: calls.append(command) or _applied_dismissal(command),
    )
    client = _client(_queue(), operations=operations, controls=controls)
    token = _csrf(client)

    response = client.post(
        path,
        content=f"csrf_token={token}&csrf_token={token}".encode(),
        headers={"content-type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert "This operations form expired" in response.text
    assert calls == []


@pytest.mark.parametrize(
    "path",
    [
        "/operations/run",
        "/operations/schedule",
        "/operations/recovery",
        "/operations/dismiss",
        "/operations/reevaluation",
    ],
)
def test_operations_actions_require_the_owner_session_before_service_calls(path: str) -> None:
    calls: list[object] = []
    controls = _control_service(
        run_now=lambda command: calls.append(command) or RunStarted("x", False),
        change_schedule=lambda command: calls.append(command)
        or ScheduleChanged(ScheduleStatus.STOPPED, False),
    )
    operations = OperationsService(
        load=lambda: _operations_snapshot(),
        recover=lambda command: calls.append(command) or _applied_recovery(command),
        reevaluate=lambda command: calls.append(command) or _accepted_reevaluation(command),
        dismiss=lambda command: calls.append(command) or _applied_dismissal(command),
    )
    app = create_review_app(
        ReviewQueueService(review_queue=lambda: _queue()),
        _configuration_service(),
        SETTINGS,
        feedback_service=DEFAULT_FEEDBACK_SERVICE,
        owner_access_service=OWNER_ACCESS,
        operations_service=operations,
        control_service=controls,
        now=lambda: NOW,
    )

    response = TestClient(app).post(path, data={}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")
    assert calls == []


def test_run_now_rejects_a_malformed_form_without_calling_the_service() -> None:
    client = _client(_queue(), controls=_control_service())

    response = client.post(
        "/operations/run",
        data={"csrf_token": _csrf(client), "idempotency_key": "private-key"},
    )

    assert response.status_code == 400
    assert "Malformed operations form" in response.text


def test_run_now_passes_owner_provenance_then_renders_the_allowlisted_notice() -> None:
    calls: list[RunNowCommand] = []
    controls = _control_service(
        run_now=lambda command: calls.append(command) or RunStarted("run-1", False)
    )
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
    assert calls == [
        RunNowCommand(
            job_name="job_finder",
            idempotency_key="private-key",
            actor="owner",
            timestamp=NOW,
        )
    ]


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
    calls: list[ScheduleChangeCommand] = []
    controls = _control_service(
        change_schedule=lambda command: calls.append(command)
        or ScheduleStateConflict(
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
    assert calls[0].actor == "owner"
    assert calls[0].timestamp == NOW


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
    assert response.headers["location"] == "/operations/control?notice=schedule-paused"


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


def test_work_recovery_requires_csrf_before_calling_the_service() -> None:
    calls: list[WorkRecoveryCommand] = []
    operations = OperationsService(
        load=lambda: _operations_snapshot(),
        recover=lambda command: calls.append(command) or _applied_recovery(command),
    )
    client = _client(_queue(), operations=operations)

    response = client.post(
        "/operations/recovery",
        data={
            "job_id": str(UUID(int=31)),
            "action": "retry_now",
            "expected_state": "failed",
            "expected_attempt_count": "2",
            "idempotency_key": "private-key",
        },
    )

    assert response.status_code == 403
    assert calls == []


def test_work_recovery_passes_an_exact_typed_command_and_redirects() -> None:
    calls: list[WorkRecoveryCommand] = []

    def recover(command: WorkRecoveryCommand) -> WorkRecoveryResult:
        calls.append(command)
        return _applied_recovery(command)

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
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert (
        response.headers["location"]
        == "/operations/work/00000000-0000-0000-0000-00000000001f?notice=work-retried"
    )
    assert calls == [
        WorkRecoveryCommand(
            idempotency_key="private-key",
            job_id=UUID(int=31),
            action=RecoveryAction.RETRY_NOW,
            expected_state="failed",
            expected_attempt_count=2,
            actor="owner",
            requested_at=NOW,
        )
    ]


def test_work_recovery_rejects_malformed_identity_before_calling_the_service() -> None:
    calls: list[WorkRecoveryCommand] = []
    client = _client(
        _queue(),
        operations=OperationsService(
            load=lambda: _operations_snapshot(),
            recover=lambda command: calls.append(command) or _applied_recovery(command),
        ),
    )

    response = client.post(
        "/operations/recovery",
        data={
            "csrf_token": _csrf(client),
            "job_id": "not-a-uuid",
            "action": "retry_now",
            "expected_state": "failed",
            "expected_attempt_count": "2",
            "idempotency_key": "private-key",
        },
    )

    assert response.status_code == 400
    assert "Malformed operations form" in response.text
    assert calls == []


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


def test_uncertain_job_reevaluation_preserves_the_exact_retry_command() -> None:
    client = _client(_queue())

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
            ReviewQueueService(review_queue=lambda: _queue()),
            _configuration_service(),
            SETTINGS,
            feedback_service=DEFAULT_FEEDBACK_SERVICE,
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

    queue_service = ReviewQueueService(
        review_queue=lambda: ReviewQueue(reviewed_items=(decided[0],), reviewed_counts={TODAY: 1})
    )
    client = TestClient(
        create_review_app(
            queue_service,
            _configuration_service(),
            SETTINGS,
            feedback_service=ReviewFeedbackService(submit=submit),
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


def test_rejects_an_invalid_review_decision_without_calling_the_service() -> None:
    submissions: list[ReviewSubmission] = []
    item = _item(TODAY, "qualified")
    client = _client(
        _queue(item),
        submit=lambda review: submissions.append(review)
        or ReviewSaved(review_event_id=UUID(int=9)),
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


def test_renders_submit_database_failure_as_retryable_unavailable() -> None:
    item = _item(TODAY, "qualified")

    def unavailable(_review: ReviewSubmission) -> ReviewSaved:
        raise psycopg.OperationalError("database down")

    client = _client(_queue(item), submit=unavailable)

    response = client.post(f"/review/{item.id}", data=_form(item, client))

    assert response.status_code == 503
    assert "Review is unavailable" in response.text
    assert "Retry" in response.text


@pytest.mark.parametrize("path", ["/", f"/review/item/{UUID(int=1)}"])
def test_review_pages_render_queue_database_failure_as_retryable_unavailable(path: str) -> None:
    def unavailable() -> ReviewQueue:
        raise psycopg.OperationalError("database down")

    app = create_review_app(
        ReviewQueueService(review_queue=unavailable),
        _configuration_service(),
        SETTINGS,
        feedback_service=DEFAULT_FEEDBACK_SERVICE,
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


@pytest.mark.parametrize(
    ("method", "path", "location"),
    [
        ("GET", "/review", "/login?next=%2Freview"),
        (
            "GET",
            f"/review/item/{UUID(int=1)}",
            f"/login?next=%2Freview%2Fitem%2F{UUID(int=1)}",
        ),
        (
            "POST",
            f"/review/{UUID(int=1)}",
            f"/login?next=%2Freview%2F{UUID(int=1)}",
        ),
    ],
)
def test_requires_a_signed_session_for_review_routes(method: str, path: str, location: str) -> None:
    def unused(*_args: object) -> Never:
        raise AssertionError("review services must not run before authentication")

    client = TestClient(
        create_review_app(
            ReviewQueueService(review_queue=unused),
            _configuration_service(),
            SETTINGS,
            feedback_service=ReviewFeedbackService(submit=unused),
            owner_access_service=OWNER_ACCESS,
            now=lambda: NOW,
        )
    )

    response = client.request(method, path, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == location


def test_fresh_install_redirects_to_one_time_owner_setup() -> None:
    state = [OwnerAccessState(stage=OnboardingStage.OWNER_ACCOUNT, has_password=False)]
    calls: list[str] = []

    def bootstrap(password: str) -> OwnerBootstrapped:
        calls.append(password)
        state[0] = OwnerAccessState(stage=OnboardingStage.PROVIDERS, has_password=True)
        return OwnerBootstrapped(state[0])

    owner_access = OwnerAccessService(
        load_state=lambda: state[0],
        authenticate=lambda _password: False,
        bootstrap=bootstrap,
    )

    def replace_provider(
        _provider: ProviderKind,
        _secret: SecretStr,
        _generation: int,
        _actor: str,
        _timestamp: datetime,
    ) -> Never:
        pytest.fail("provider credential was unexpectedly replaced")

    provider_setup = ProviderSetupService(
        inspect=lambda: ProviderSetupSnapshot(
            credentials=tuple(
                ProviderCredentialState(provider=provider, generation=0)
                for provider in ProviderKind
            )
        ),
        replace=replace_provider,
        advance=lambda: pytest.fail("provider setup unexpectedly advanced"),
        resolve=lambda _provider: pytest.fail("provider credential was unexpectedly resolved"),
    )
    client = TestClient(
        create_review_app(
            ReviewQueueService(review_queue=lambda: _queue()),
            _configuration_service(),
            SETTINGS,
            feedback_service=DEFAULT_FEEDBACK_SERVICE,
            owner_access_service=owner_access,
            provider_setup_service=provider_setup,
            now=lambda: NOW,
        )
    )

    protected = client.get("/review", follow_redirects=False)
    forbidden = client.post(
        "/setup",
        data={"password": OWNER_PASSWORD, "password_confirmation": OWNER_PASSWORD},
    )
    setup = client.get("/setup")
    csrf_match = re.search(r'name="csrf_token" value="([^"]+)"', setup.text)
    assert csrf_match is not None
    rejected_token = client.post(
        "/setup",
        data={
            "csrf_token": csrf_match.group(1),
            "bootstrap_token": "incorrect-bootstrap-token-with-32-characters",
            "password": OWNER_PASSWORD,
            "password_confirmation": OWNER_PASSWORD,
        },
    )
    completed = client.post(
        "/setup",
        data={
            "csrf_token": csrf_match.group(1),
            "bootstrap_token": BOOTSTRAP_TOKEN,
            "password": OWNER_PASSWORD,
            "password_confirmation": OWNER_PASSWORD,
        },
        follow_redirects=False,
    )

    assert protected.status_code == 303
    assert protected.headers["location"] == "/setup"
    assert forbidden.status_code == 403
    assert rejected_token.status_code == 401
    assert "Create the owner password" in setup.text
    assert completed.status_code == 303
    assert completed.headers["location"] == "/setup/providers"
    assert calls == [OWNER_PASSWORD]
    review = client.get("/review", follow_redirects=False)
    assert review.status_code == 303
    assert review.headers["location"] == "/setup/providers"
    assert "Connect the services" in client.get("/setup/providers").text


def test_provider_setup_never_echoes_credentials_and_advances_when_ready() -> None:
    state = [OwnerAccessState(stage=OnboardingStage.PROVIDERS, has_password=True)]
    stored: dict[ProviderKind, ProviderCredentialState] = {}

    def inspect() -> ProviderSetupSnapshot:
        return ProviderSetupSnapshot(
            credentials=tuple(
                stored.get(provider, ProviderCredentialState(provider=provider, generation=0))
                for provider in ProviderKind
            )
        )

    def replace(
        provider: ProviderKind,
        secret: SecretStr,
        expected_generation: int,
        _actor: str,
        _timestamp: datetime,
    ) -> ProviderCredentialStored:
        assert secret.get_secret_value() == "credential-that-must-not-be-rendered"
        credential = ProviderCredentialState(
            provider=provider,
            generation=expected_generation + 1,
            capabilities={
                ProviderKind.JINA: (
                    ProviderCapability.SEARCH,
                    ProviderCapability.SCRAPE,
                ),
                ProviderKind.OPENROUTER: (
                    ProviderCapability.STRUCTURED_GENERATION,
                    ProviderCapability.USAGE_COST,
                ),
                ProviderKind.TYPESAFE: (
                    ProviderCapability.RELEVANCE_EVALUATION,
                    ProviderCapability.USAGE_COST,
                ),
            }[provider],
            validated_at=NOW,
        )
        stored[provider] = credential
        return ProviderCredentialStored(state=credential)

    def advance() -> ProviderStageAdvanced:
        assert inspect().ready
        state[0] = OwnerAccessState(stage=OnboardingStage.PREFERENCES, has_password=True)
        return ProviderStageAdvanced(state=state[0])

    owner_access = OwnerAccessService(
        load_state=lambda: state[0],
        authenticate=lambda password: password == OWNER_PASSWORD,
        bootstrap=lambda _password: pytest.fail("owner was unexpectedly bootstrapped"),
    )
    providers = ProviderSetupService(
        inspect=inspect,
        replace=replace,
        advance=advance,
        resolve=lambda _provider: pytest.fail("provider credential was unexpectedly resolved"),
    )
    client = TestClient(
        create_review_app(
            ReviewQueueService(review_queue=lambda: _queue()),
            _configuration_service(),
            SETTINGS,
            feedback_service=DEFAULT_FEEDBACK_SERVICE,
            owner_access_service=owner_access,
            provider_setup_service=providers,
            now=lambda: NOW,
        )
    )
    _authenticate(client)
    csrf_token = _csrf(client)

    for provider in ProviderKind:
        response = client.post(
            "/setup/providers",
            data={
                "csrf_token": csrf_token,
                "provider": provider.value,
                "expected_generation": "0",
                "credential": "credential-that-must-not-be-rendered",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "credential-that-must-not-be-rendered" not in response.text

    completed = client.post(
        "/setup/providers/continue",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )

    assert completed.status_code == 303
    assert completed.headers["location"] == "/configuration"
    assert client.get("/review", follow_redirects=False).headers["location"] == "/configuration"


def test_budget_setup_shows_bounds_and_advances_to_test_search() -> None:
    owner_state = [OwnerAccessState(stage=OnboardingStage.BUDGET, has_password=True)]
    estimate = ExecutionEstimate(
        search_queries=12,
        jobs_per_run=25,
        logical_model_calls_per_job=8,
        maximum_provider_attempts=1600,
    )

    def save_budget(
        expected_version: int,
        monthly_limit: Decimal,
        run_limit: Decimal,
        max_jobs: int,
        _actor: str,
        _timestamp: datetime,
    ) -> BudgetSaved:
        policy = ExecutionBudgetPolicy(
            version=expected_version + 1,
            monthly_limit_usd=monthly_limit,
            run_allowance_usd=run_limit,
            max_jobs_per_run=max_jobs,
            max_search_queries_per_run=estimate.search_queries,
            max_provider_attempts_per_run=estimate.maximum_provider_attempts,
        )
        owner_state[0] = OwnerAccessState(stage=OnboardingStage.TEST_SEARCH, has_password=True)
        return BudgetSaved(policy=policy, owner_state=owner_state[0])

    budget = BudgetSetupService(
        inspect=lambda _max_jobs: BudgetSetupState(policy=None, estimate=estimate),
        save=save_budget,
    )
    owner = OwnerAccessService(
        load_state=lambda: owner_state[0],
        authenticate=lambda password: password == OWNER_PASSWORD,
        bootstrap=lambda _password: pytest.fail("owner was unexpectedly bootstrapped"),
    )
    client = TestClient(
        create_review_app(
            ReviewQueueService(review_queue=lambda: _queue()),
            _configuration_service(),
            SETTINGS,
            feedback_service=DEFAULT_FEEDBACK_SERVICE,
            owner_access_service=owner,
            budget_setup_service=budget,
            now=lambda: NOW,
        )
    )
    _authenticate(client)
    page = client.get("/setup/budget")
    csrf_token = _csrf(client)

    completed = client.post(
        "/setup/budget",
        data={
            "csrf_token": csrf_token,
            "expected_version": "0",
            "monthly_limit_usd": "20.00",
            "run_allowance_usd": "2.00",
            "max_jobs_per_run": "25",
        },
        follow_redirects=False,
    )

    assert "12 searches per discovery run" in page.text
    assert "1600 provider attempts" in page.text
    assert completed.status_code == 303
    assert completed.headers["location"] == "/setup/test-search"
    assert "Ready for a bounded test search" in client.get("/setup/test-search").text


def test_completed_upgrade_without_a_budget_is_routed_to_budget_setup() -> None:
    estimate = ExecutionEstimate(
        search_queries=12,
        jobs_per_run=25,
        logical_model_calls_per_job=8,
        maximum_provider_attempts=1600,
    )

    def save_budget(
        _expected_version: int,
        _monthly_limit: Decimal,
        _run_allowance: Decimal,
        _max_jobs: int,
        _actor: str,
        _timestamp: datetime,
    ) -> Never:
        pytest.fail("budget was unexpectedly saved")

    budget = BudgetSetupService(
        inspect=lambda _max_jobs: BudgetSetupState(policy=None, estimate=estimate),
        save=save_budget,
    )
    owner = OwnerAccessService(
        load_state=lambda: OwnerAccessState(stage=OnboardingStage.COMPLETE, has_password=True),
        authenticate=lambda password: password == OWNER_PASSWORD,
        bootstrap=lambda _password: pytest.fail("owner was unexpectedly bootstrapped"),
    )
    client = TestClient(
        create_review_app(
            ReviewQueueService(review_queue=lambda: _queue()),
            _configuration_service(),
            SETTINGS,
            feedback_service=DEFAULT_FEEDBACK_SERVICE,
            owner_access_service=owner,
            budget_setup_service=budget,
        )
    )
    _authenticate(client)

    response = client.get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/setup/budget"


def test_legacy_install_fails_closed_until_password_import() -> None:
    legacy = OwnerAccessService(
        load_state=lambda: OwnerAccessState(
            stage=OnboardingStage.LEGACY_OWNER_IMPORT, has_password=False
        ),
        authenticate=lambda _password: False,
        bootstrap=lambda _password: pytest.fail("legacy installation became claimable"),
    )
    client = TestClient(
        create_review_app(
            ReviewQueueService(review_queue=lambda: _queue()),
            _configuration_service(),
            SETTINGS,
            feedback_service=DEFAULT_FEEDBACK_SERVICE,
            owner_access_service=legacy,
            now=lambda: NOW,
        )
    )

    response = client.get("/setup")

    assert response.status_code == 503
    assert "Legacy owner import is required" in response.text


def test_authenticates_and_signs_out_the_owner() -> None:
    client = TestClient(
        create_review_app(
            ReviewQueueService(review_queue=lambda: _queue()),
            _configuration_service(),
            SETTINGS,
            feedback_service=DEFAULT_FEEDBACK_SERVICE,
            owner_access_service=OWNER_ACCESS,
            now=lambda: NOW,
        )
    )

    rejected = client.post("/login", data={"password": "wrong password", "next": "/review"})
    accepted = client.post(
        "/login",
        data={"password": OWNER_PASSWORD, "next": "/review"},
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


@pytest.mark.parametrize(
    "next_value", ["//evil.example", "https://evil.example", "/\\evil.example"]
)
def test_login_redirects_unsafe_next_targets_to_the_review_home(next_value: str) -> None:
    client = TestClient(
        create_review_app(
            ReviewQueueService(review_queue=lambda: _queue()),
            _configuration_service(),
            SETTINGS,
            feedback_service=DEFAULT_FEEDBACK_SERVICE,
            owner_access_service=OWNER_ACCESS,
            now=lambda: NOW,
        )
    )

    response = client.post(
        "/login",
        data={"password": OWNER_PASSWORD, "next": next_value},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_exposes_public_health_and_database_readiness() -> None:
    readiness_calls = 0

    def ready() -> None:
        nonlocal readiness_calls
        readiness_calls += 1

    app = create_review_app(
        ReviewQueueService(review_queue=lambda: _queue()),
        _configuration_service(),
        SETTINGS,
        feedback_service=DEFAULT_FEEDBACK_SERVICE,
        owner_access_service=OWNER_ACCESS,
        readiness=ready,
        now=lambda: NOW,
    )
    client = TestClient(app)

    assert client.get("/healthz").text == "ok"
    assert client.get("/readyz").text == "ready"
    favicon = client.get("/favicon.ico")
    assert favicon.status_code == 204
    assert favicon.content == b""
    assert readiness_calls == 1


def test_reports_database_readiness_failure_without_authentication() -> None:
    def unavailable() -> None:
        raise psycopg.OperationalError("database down")

    client = TestClient(
        create_review_app(
            ReviewQueueService(review_queue=lambda: _queue()),
            _configuration_service(),
            SETTINGS,
            feedback_service=DEFAULT_FEEDBACK_SERVICE,
            owner_access_service=OWNER_ACCESS,
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
    runs: RunsService | None = None,
    activity: ActivityService | None = None,
    analytics: AnalyticsService | None = None,
    controls: ControlPlaneService | None = None,
) -> TestClient:
    queue_service = ReviewQueueService(review_queue=lambda: queue)
    client = TestClient(
        create_review_app(
            queue_service,
            _configuration_service(),
            SETTINGS,
            feedback_service=ReviewFeedbackService(submit=submit),
            owner_access_service=OWNER_ACCESS,
            operations_service=operations,
            runs_service=runs,
            activity_service=activity,
            analytics_service=analytics,
            control_service=controls,
            now=lambda: NOW,
        )
    )
    _authenticate(client)
    return client


def _draining_client(remaining: list[ReviewItem], submit: Submitter) -> TestClient:
    queue_service = ReviewQueueService(review_queue=lambda: ReviewQueue(items=tuple(remaining)))
    client = TestClient(
        create_review_app(
            queue_service,
            _configuration_service(),
            SETTINGS,
            feedback_service=ReviewFeedbackService(submit=submit),
            owner_access_service=OWNER_ACCESS,
            now=lambda: NOW,
        )
    )
    _authenticate(client)
    return client


def _authenticate(client: TestClient) -> None:
    response = client.post(
        "/login",
        data={"password": OWNER_PASSWORD, "next": "/review"},
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


def _shell_link(href: str, label: str, *, current: bool = False) -> str:
    pattern = rf'<a(?=[^>]*href="{re.escape(href)}")'
    if current:
        pattern += r'(?=[^>]*aria-current="page")'
    pattern += rf'(?=[^>]*class="shell-link")[^>]*>{re.escape(label)}</a>'
    return pattern


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


def _operations_snapshot() -> OperationsSnapshot:
    return OperationsSnapshot(
        health=OperationsHealth.UNKNOWN,
        queues=QueueCounts(),
        spend=SpendSummary(known_usd=Decimal(0), unknown_attempts=0),
        recent_runs=(),
        failures=(),
    )


def test_dismiss_undo_redirects_with_its_own_notice() -> None:
    client = _client(
        _queue(),
        operations=OperationsService(
            load=lambda: _operations_snapshot(),
            dismiss=lambda command: _applied_dismissal(command),
        ),
    )

    response = client.post(
        "/operations/dismiss",
        data={
            "csrf_token": _csrf(client),
            "job_id": str(UUID(int=32)),
            "action": "undo_dismiss",
            "expected_attempt_count": "3",
            "idempotency_key": "private-key",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert (
        response.headers["location"]
        == "/operations/work/00000000-0000-0000-0000-000000000020?notice=dismiss-undone"
    )


def test_dismissal_requires_csrf_before_calling_the_service() -> None:
    calls: list[WorkDismissalCommand] = []
    operations = OperationsService(
        load=lambda: _operations_snapshot(),
        dismiss=lambda command: calls.append(command) or _applied_dismissal(command),
    )
    client = _client(_queue(), operations=operations)

    response = client.post(
        "/operations/dismiss",
        data={
            "job_id": str(UUID(int=32)),
            "action": "dismiss",
            "expected_attempt_count": "3",
            "idempotency_key": "private-key",
        },
    )

    assert response.status_code == 403
    assert calls == []


def _run_item(
    *,
    value: int = 1,
    kind: str = "orchestration",
    status: PipelineRunStatus = "completed",
    idle: bool = False,
) -> RunListItem:
    return RunListItem(
        id=UUID(int=value),
        kind=kind,
        status=status,
        started_at=NOW,
        completed_at=NOW if status != "running" else None,
        discoveries=0 if idle else 4,
        processing_attempts=0 if idle else 5,
        processed_jobs=0 if idle else 3,
        model_calls=0 if idle else 2,
        known_cost_usd=Decimal("0.5010") if not idle else Decimal(0),
        error_summary=None,
    )


def _activity_run_entry(
    *, value: int = 1, kind: str = "orchestration", idle: bool = False
) -> ActivityEntry:
    item = _run_item(value=value, kind=kind, idle=idle)
    return ActivityEntry(
        occurred_at=item.started_at,
        ref=str(item.id),
        item=ActivityRun(
            id=item.id,
            kind=item.kind,
            status=item.status,
            started_at=item.started_at,
            completed_at=item.completed_at,
            discoveries=item.discoveries,
            processing_attempts=item.processing_attempts,
            processed_jobs=item.processed_jobs,
            model_calls=item.model_calls,
            known_cost_usd=item.known_cost_usd,
            error_summary=item.error_summary,
        ),
    )


def _activity_work_entry(
    *,
    value: int = 9,
    state: str = "failed",
    dismissed: bool = False,
) -> ActivityEntry:
    return ActivityEntry(
        occurred_at=NOW,
        ref=str(UUID(int=value)),
        item=ActivityWork(
            job_id=UUID(int=value),
            state=cast(WorkItemState, state),
            attempt_count=2,
            occurred_at=NOW,
            retry_at=NOW if state == "failed" else None,
            failure_summary="provider_timeout: OpenRouter did not respond",
            dismissed=dismissed,
        ),
    )


def _activity_service(
    *entries: ActivityEntry, cursor: str | None = None
) -> tuple[ActivityService, list[ActivityQuery]]:
    captured: list[ActivityQuery] = []

    def list_page(query: ActivityQuery) -> ActivityPage:
        captured.append(query)
        return ActivityPage(entries=tuple(entries), next_cursor=cursor)

    return ActivityService(list=list_page), captured


def test_the_activity_page_renders_runs_work_and_pagination() -> None:
    activity, _ = _activity_service(
        _activity_run_entry(value=1, idle=True),
        _activity_run_entry(value=2, kind="discovery"),
        _activity_work_entry(value=9, state="failed"),
        cursor="next-cursor-token",
    )
    client = _client(_queue(), activity=activity)

    listing = client.get("/operations/runs")

    assert listing.status_code == 200
    assert "Idle tick — nothing was due." in listing.text
    assert "4 discovered · 3 processed · 2 model calls" in listing.text
    assert 'href="/operations/runs/00000000-0000-0000-0000-000000000002"' in listing.text
    assert "Job work" in listing.text
    assert ">retrying</span>" in listing.text
    assert "provider_timeout: OpenRouter did not respond" in listing.text
    assert 'href="/operations/work/00000000-0000-0000-0000-000000000009"' in listing.text
    assert "Open work →" in listing.text
    assert 'class="row-head"' in listing.text
    assert 'href="/operations/runs?cursor=next-cursor-token"' in listing.text
    assert "Next page →" in listing.text


def test_the_activity_page_renders_the_filter_form() -> None:
    activity, _ = _activity_service()
    client = _client(_queue(), activity=activity)

    listing = client.get("/operations/runs")

    assert listing.status_code == 200
    assert 'name="status" value="failed"' in listing.text
    assert 'name="kind"' in listing.text
    assert 'name="from"' in listing.text
    assert "Apply filters" in listing.text


def test_the_run_detail_page_keeps_its_run_content() -> None:
    runs = RunsService(
        detail=lambda _run_id: RunDetail(
            item=_run_item(value=2, kind="discovery"),
            parameters={},
            unknown_cost_calls=1,
            keywords=(),
            attempts=(),
            models=(),
            decisions=(),
        ),
    )
    client = _client(_queue(), runs=runs)

    detail = client.get("/operations/runs/00000000-0000-0000-0000-000000000002")

    assert detail.status_code == 200
    assert "Discovery" in detail.text
    assert "No jobs were discovered by this run." in detail.text
    assert "1 call returned no usage, so it has no recorded cost." in detail.text
    assert 'href="/operations/runs"' in detail.text


def test_the_activity_page_passes_filters_to_the_query() -> None:
    activity, captured = _activity_service()
    client = _client(_queue(), activity=activity)

    filtered = client.get(
        "/operations/runs?status=failed&status=retrying&kind=work&from=2026-09-01&to=2026-09-10"
    )

    assert filtered.status_code == 200
    query = captured[0]
    assert query.statuses == frozenset({"failed", "retrying"})
    assert query.kind == "work"
    assert query.from_at == datetime(2026, 9, 1, tzinfo=UTC)
    assert query.to_at == datetime(2026, 9, 10, tzinfo=UTC)
    assert "No activity matches these filters." in filtered.text

    empty = client.get("/operations/runs")

    assert "No activity recorded yet." in empty.text


def test_the_activity_page_falls_back_to_the_first_page_for_a_broken_cursor() -> None:
    activity, captured = _activity_service(_activity_run_entry(value=1))
    client = _client(_queue(), activity=activity)

    response = client.get("/operations/runs?cursor=broken-cursor")

    assert response.status_code == 200
    assert "Open run →" in response.text
    assert captured[0].cursor is None
    assert captured[0].statuses == frozenset()
    assert captured[0].limit == 50


def test_the_activity_page_keeps_filters_on_the_next_page_link() -> None:
    activity, _ = _activity_service(cursor="next-cursor-token")
    client = _client(_queue(), activity=activity)

    listing = client.get("/operations/runs?status=failed&kind=work&from=2026-09-01")

    assert listing.status_code == 200
    assert (
        'href="/operations/runs?status=failed&amp;kind=work&amp;from=2026-09-01&amp;cursor=next-cursor-token"'
        in listing.text
    )


def _work_item_detail(
    *,
    state: WorkItemState = "terminal_error",
    retry_at: datetime | None = None,
    dismissed: bool = False,
    dismissed_at: datetime | None = None,
    dismissed_by: str | None = None,
) -> WorkItemDetail:
    return WorkItemDetail(
        job_id=UUID(int=31),
        state=state,
        attempt_count=3,
        created_at=NOW - timedelta(hours=2),
        completed_at=NOW - timedelta(hours=1, minutes=59),
        last_failed_at=NOW - timedelta(minutes=30),
        retry_at=retry_at,
        failure_summary="provider_timeout: OpenRouter did not respond",
        discovery_run_id=UUID(int=7),
        dismissed=dismissed,
        dismissed_at=dismissed_at,
        dismissed_by=dismissed_by,
        attempts=(
            WorkAttemptSummary(
                operation_key="evaluation",
                attempt_number=2,
                status="failed",
                started_at=NOW - timedelta(minutes=30),
                completed_at=NOW - timedelta(minutes=29),
                run_id=UUID(int=7),
                model_calls=2,
                known_cost_usd=Decimal("0.25"),
                error_summary="provider_timeout: OpenRouter did not respond",
            ),
        ),
    )


def test_the_work_item_page_shows_failure_context_and_actions() -> None:
    operations = OperationsService(
        load=lambda: _operations_snapshot(),
        work_detail=lambda _job: _work_item_detail(),
    )
    client = _client(_queue(), operations=operations)

    response = client.get(f"/operations/work/{UUID(int=31)}")

    assert response.status_code == 200
    assert "Job work" in response.text
    assert 'class="schedule-state terminal_error"' in response.text
    assert ">Terminal<" in response.text
    assert "provider_timeout: OpenRouter did not respond" in response.text
    assert "Attempt count" in response.text
    assert "Dismissed" in response.text
    assert "Recover terminal work" in response.text
    assert "Dismiss" in response.text
    assert 'action="/operations/recovery"' in response.text
    assert 'action="/operations/dismiss"' in response.text
    assert "attempt 2" in response.text
    assert "2 model calls · $0.2500" in response.text
    assert re.search(
        _shell_link("/operations/runs", "Recent activity", current=True), response.text
    )


def test_the_work_item_page_offers_retry_for_failed_work() -> None:
    operations = OperationsService(
        load=lambda: _operations_snapshot(),
        work_detail=lambda _job: _work_item_detail(
            state="failed",
            retry_at=NOW + timedelta(minutes=9),
        ),
    )
    client = _client(_queue(), operations=operations)

    response = client.get(f"/operations/work/{UUID(int=31)}")

    assert response.status_code == 200
    assert ">Retrying<" in response.text
    assert "Retry now" in response.text
    assert "Recover terminal work" not in response.text
    assert ">Dismiss</button>" not in response.text


def test_the_work_item_page_offers_undo_for_dismissed_work() -> None:
    operations = OperationsService(
        load=lambda: _operations_snapshot(),
        work_detail=lambda _job: _work_item_detail(
            dismissed=True,
            dismissed_at=NOW - timedelta(hours=1),
            dismissed_by="owner",
        ),
    )
    client = _client(_queue(), operations=operations)

    response = client.get(f"/operations/work/{UUID(int=31)}")

    assert response.status_code == 200
    assert 'class="schedule-state dismissed"' in response.text
    assert ">Dismissed<" in response.text
    assert "Undo dismissal" in response.text
    assert "Dismiss<" not in response.text


def test_the_work_item_page_hides_actions_for_healthy_work() -> None:
    operations = OperationsService(
        load=lambda: _operations_snapshot(),
        work_detail=lambda _job: _work_item_detail(state="completed"),
    )
    client = _client(_queue(), operations=operations)

    response = client.get(f"/operations/work/{UUID(int=31)}")

    assert response.status_code == 200
    assert "Take action" not in response.text


def test_an_unknown_work_item_renders_the_not_found_page() -> None:
    def missing_work(_job: UUID) -> WorkItemDetail:
        raise WorkItemNotFound("Work item does not exist")

    client = _client(
        _queue(),
        operations=OperationsService(load=lambda: _operations_snapshot(), work_detail=missing_work),
    )

    missing = client.get(f"/operations/work/{UUID(int=99)}")
    malformed = client.get("/operations/work/not-a-uuid")

    assert missing.status_code == 404
    assert "Work item was not found" in missing.text
    assert malformed.status_code == 404


def test_recovery_from_the_detail_page_returns_to_the_detail_page() -> None:
    calls: list[WorkRecoveryCommand] = []

    def recover(command: WorkRecoveryCommand) -> WorkRecoveryResult:
        calls.append(command)
        return _applied_recovery(command)

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
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert (
        response.headers["location"]
        == "/operations/work/00000000-0000-0000-0000-00000000001f?notice=work-retried"
    )
    assert calls[0].job_id == UUID(int=31)


def test_dismissal_from_the_detail_page_returns_to_the_detail_page() -> None:
    client = _client(
        _queue(),
        operations=OperationsService(
            load=lambda: _operations_snapshot(),
            dismiss=lambda command: WorkDismissalApplied(
                receipt=WorkDismissalReceipt(
                    idempotency_key=command.idempotency_key,
                    job_id=command.job_id,
                    action=command.action,
                    expected_attempt_count=command.expected_attempt_count,
                    actor=command.actor,
                    requested_at=command.requested_at,
                    outcome="applied",
                    prior_state="terminal_error",
                    prior_attempt_count=3,
                    resulting_attempt_count=None,
                ),
                replayed=False,
            ),
        ),
    )

    response = client.post(
        "/operations/dismiss",
        data={
            "csrf_token": _csrf(client),
            "job_id": str(UUID(int=31)),
            "action": "dismiss",
            "expected_attempt_count": "3",
            "idempotency_key": "private-key",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert (
        response.headers["location"]
        == "/operations/work/00000000-0000-0000-0000-00000000001f?notice=work-dismissed"
    )


def test_the_run_detail_page_links_attempts_to_work_detail() -> None:
    runs = RunsService(
        detail=lambda _run_id: RunDetail(
            item=_run_item(value=2, kind="discovery"),
            parameters={},
            unknown_cost_calls=0,
            keywords=(),
            attempts=(
                RunAttemptSummary(
                    job_id=UUID(int=31),
                    operation_key="evaluation",
                    attempt_number=1,
                    status="failed",
                    error_summary="provider_timeout: OpenRouter did not respond",
                ),
                RunAttemptSummary(
                    job_id=None,
                    operation_key="orchestration",
                    attempt_number=0,
                    status="completed",
                    error_summary=None,
                ),
            ),
            models=(),
            decisions=(),
        ),
    )
    client = _client(_queue(), runs=runs)

    response = client.get("/operations/runs/00000000-0000-0000-0000-000000000002")

    assert response.status_code == 200
    assert 'href="/operations/work/00000000-0000-0000-0000-00000000001f"' in response.text
    assert "Inspect work →" in response.text
    assert response.text.count("Inspect work →") == 1


def test_an_unknown_run_id_renders_the_not_found_page() -> None:
    client = _client(_queue(), runs=RunsService())

    response = client.get("/operations/runs/not-a-uuid")

    assert response.status_code == 404
    assert "Run not found" in response.text


def _spend_analytics() -> SpendAnalytics:
    return SpendAnalytics(
        known_usd=Decimal("1.2345"),
        calls=6,
        accepted=4,
        errors=2,
        input_tokens=900,
        output_tokens=300,
        max_latency_ms=5200,
        days=(
            DaySpend(
                day=NOW.date(),
                calls=4,
                accepted=3,
                errors=1,
                known_cost_usd=Decimal("1.0000"),
                by_model=(
                    DayModelSpend(
                        model="z-ai/glm-4.6",
                        known_cost_usd=Decimal("0.9000"),
                        p90_latency_ms=3000,
                    ),
                    DayModelSpend(
                        model="openai/gpt-5-mini",
                        known_cost_usd=Decimal("0.1000"),
                        p90_latency_ms=1200,
                    ),
                ),
            ),
            DaySpend(
                day=NOW.date() - timedelta(days=1),
                calls=2,
                accepted=1,
                errors=1,
                known_cost_usd=Decimal("0.2345"),
                by_model=(
                    DayModelSpend(
                        model="z-ai/glm-4.6",
                        known_cost_usd=Decimal("0.2345"),
                        p90_latency_ms=4100,
                    ),
                ),
            ),
        ),
        models=(
            ModelSpend(
                model="z-ai/glm-4.6",
                calls=4,
                accepted=3,
                errors=1,
                input_tokens=700,
                output_tokens=200,
                known_cost_usd=Decimal("1.1000"),
                max_latency_ms=5200,
            ),
            ModelSpend(
                model="openai/gpt-5-mini",
                calls=2,
                accepted=1,
                errors=1,
                input_tokens=200,
                output_tokens=100,
                known_cost_usd=Decimal("0.1345"),
                max_latency_ms=2100,
            ),
        ),
    )


def test_the_analytics_page_answers_spend_by_day_model_and_run() -> None:
    client = _client(_queue(), analytics=AnalyticsService(load=lambda: _spend_analytics()))

    response = client.get("/operations/analytics")

    assert response.status_code == 200
    assert "$1.2345" in response.text
    assert "Model spend only" in response.text
    assert "Jina (search and scrape) and Langfuse costs are not tracked." in response.text
    assert "4 accepted calls" in response.text
    assert "2 calls returned no usage" in response.text
    assert "1,200" in response.text
    assert "900 in / 300 out" in response.text
    assert "5,200 ms" in response.text
    assert "z-ai/glm-4.6" in response.text
    assert "$1.1000" in response.text
    assert "row-head" in response.text
    assert "up to 5200 ms" in response.text
    assert re.search(_shell_link("/operations/analytics", "Analytics", current=True), response.text)
    assert 'href="/operations" class="back-link"' not in response.text


def test_the_analytics_page_charts_spend_per_day_with_readable_dates() -> None:
    client = _client(_queue(), analytics=AnalyticsService(load=lambda: _spend_analytics()))

    response = client.get("/operations/analytics")

    assert response.status_code == 200
    library_match = re.search(r'src="(/static/frappe-charts[^"]+)"', response.text)
    init_match = re.search(r'src="(/static/spend-chart-init[^"]+)"', response.text)
    latency_init_match = re.search(r'src="(/static/latency-chart-init[^"]+)"', response.text)
    assert library_match is not None
    assert init_match is not None
    assert latency_init_match is not None
    assert re.fullmatch(
        r"/static/frappe-charts\.min\.umd\.[0-9a-f]{10}\.js", library_match.group(1)
    )
    assert re.fullmatch(r"/static/spend-chart-init\.[0-9a-f]{10}\.js", init_match.group(1))
    assert re.fullmatch(
        r"/static/latency-chart-init\.[0-9a-f]{10}\.js", latency_init_match.group(1)
    )
    assert 'id="spend-per-day-chart"' in response.text
    assert "Sep 9" in response.text
    assert "Sep 10" in response.text
    assert "Sep 10, 2026 · 3 accepted calls · 1 returned no usage" in response.text
    assert (
        '{"name": "z-ai/glm-4.6", "values": [0.2345, 0.9], "costs": ["$0.2345", "$0.9000"]}'
        in response.text
    )
    assert (
        '{"name": "openai/gpt-5-mini", "values": [0.0, 0.1], "costs": ["$0.0000", "$0.1000"]}'
        in response.text
    )
    script_tags: list[str] = re.findall(r"<script[^>]*>", response.text)
    executable_inline_scripts = [
        tag for tag in script_tags if "src=" not in tag and "application/json" not in tag
    ]
    assert executable_inline_scripts == []


def test_the_analytics_page_charts_p90_latency_per_model_per_day() -> None:
    client = _client(_queue(), analytics=AnalyticsService(load=lambda: _spend_analytics()))

    response = client.get("/operations/analytics")

    assert response.status_code == 200
    assert 'id="latency-per-day-chart"' in response.text
    assert "Call latency" in response.text
    assert "9 in 10 calls were faster than the bar" in response.text
    assert (
        '{"name": "z-ai/glm-4.6", "values": [4100, 3000]}, '
        + '{"name": "openai/gpt-5-mini", "values": [null, 1200]}'
        in response.text
    )


def test_the_analytics_chart_assets_are_served_as_static_files() -> None:
    client = _client(_queue(), analytics=AnalyticsService(load=lambda: _spend_analytics()))

    page = client.get("/operations/analytics")
    library_match = re.search(r'src="(/static/frappe-charts[^"]+)"', page.text)
    init_match = re.search(r'src="(/static/spend-chart-init[^"]+)"', page.text)
    latency_init_match = re.search(r'src="(/static/latency-chart-init[^"]+)"', page.text)
    assert library_match is not None
    assert init_match is not None
    assert latency_init_match is not None

    library = client.get(library_match.group(1))
    init_script = client.get(init_match.group(1))
    latency_init_script = client.get(latency_init_match.group(1))
    stale_url = client.get("/static/spend-chart-init.js")
    missing = client.get("/static/nope.js")

    assert library.status_code == 200
    assert "javascript" in library.headers["content-type"]
    assert library.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert init_script.status_code == 200
    assert init_script.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert "frappe.Chart" in init_script.text
    assert "stacked: true" in init_script.text
    assert latency_init_script.status_code == 200
    assert latency_init_script.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert "frappe.Chart" in latency_init_script.text
    assert "stacked" not in latency_init_script.text
    assert stale_url.status_code == 404
    assert missing.status_code == 404


def test_static_assets_require_the_owner_session() -> None:
    app = create_review_app(
        ReviewQueueService(review_queue=lambda: _queue()),
        _configuration_service(),
        SETTINGS,
        feedback_service=DEFAULT_FEEDBACK_SERVICE,
        owner_access_service=OWNER_ACCESS,
        now=lambda: NOW,
    )

    response = TestClient(app).get("/static/nope.js", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Fstatic%2Fnope.js"


def test_the_analytics_page_renders_an_empty_state_without_calls() -> None:
    empty = SpendAnalytics(
        known_usd=Decimal(0),
        calls=0,
        accepted=0,
        errors=0,
        input_tokens=0,
        output_tokens=0,
        max_latency_ms=0,
        days=(),
        models=(),
    )
    client = _client(_queue(), analytics=AnalyticsService(load=lambda: empty))

    response = client.get("/operations/analytics")

    assert response.status_code == 200
    assert "$0.0000" in response.text
    assert "No model calls were recorded in the last 30 days." in response.text
    assert "No model calls were recorded." in response.text
    assert "latency-per-day-chart" not in response.text


def test_the_analytics_page_degrades_when_the_database_is_unreachable() -> None:
    def broken() -> SpendAnalytics:
        raise psycopg.Error("connection refused")

    client = _client(_queue(), analytics=AnalyticsService(load=broken))

    response = client.get("/operations/analytics")

    assert response.status_code == 503
    assert "Model spend analytics are unavailable" in response.text


def test_the_operations_pages_link_to_each_other() -> None:
    client = _client(_queue(), activity=_activity_service()[0])

    runs = client.get("/operations/runs")

    assert 'href="/operations/analytics"' in runs.text
    assert 'href="/operations/control"' in runs.text
    assert 'href="/operations/failures"' not in runs.text


def _applied_dismissal(command: WorkDismissalCommand) -> WorkDismissalApplied:
    return WorkDismissalApplied(
        receipt=WorkDismissalReceipt(
            idempotency_key=command.idempotency_key,
            job_id=command.job_id,
            action=command.action,
            expected_attempt_count=command.expected_attempt_count,
            actor=command.actor,
            requested_at=command.requested_at,
            outcome="applied",
            prior_state="terminal_error",
            prior_attempt_count=command.expected_attempt_count,
            resulting_attempt_count=command.expected_attempt_count,
        ),
        replayed=False,
    )


def _applied_recovery(command: WorkRecoveryCommand) -> WorkRecoveryApplied:
    return WorkRecoveryApplied(
        receipt=_recovery_receipt(command, outcome="applied", prior_state=command.expected_state),
        replayed=False,
    )


def _accepted_reevaluation(command: JobReevaluationCommand) -> JobReevaluationAccepted:
    return JobReevaluationAccepted(
        receipt=JobReevaluationReceipt(
            idempotency_key=command.idempotency_key,
            expected_decision_id=command.expected_decision_id,
            expected_snapshot_id=command.expected_snapshot_id,
            actor=command.actor,
            requested_at=command.requested_at,
            outcome="accepted",
        ),
        replayed=False,
    )


def _recovery_receipt(
    command: WorkRecoveryCommand,
    *,
    outcome: RecoveryOutcome,
    prior_state: WorkItemState,
) -> WorkRecoveryReceipt:
    return WorkRecoveryReceipt(
        idempotency_key=command.idempotency_key,
        job_id=command.job_id,
        action=command.action,
        expected_state=command.expected_state,
        expected_attempt_count=command.expected_attempt_count,
        actor=command.actor,
        requested_at=command.requested_at,
        outcome=outcome,
        prior_state=prior_state,
        prior_attempt_count=2,
        prior_retry_at=NOW if prior_state == "failed" else None,
        prior_failed_at=NOW,
        prior_error={"code": "failure", "reason": "failed"},
        resulting_state=prior_state,
        resulting_attempt_count=2,
        resulting_retry_at=NOW if prior_state == "failed" else None,
    )
