from __future__ import annotations

from decimal import Decimal
from http.cookies import SimpleCookie
import re
from typing import Never
from uuid import UUID

import pytest
from pydantic import SecretStr
from starlette.routing import Route
from starlette.testclient import TestClient

from job_finder.config import ReviewAppSettings
from job_finder.web.app import create_review_app
from job_finder.operations.control_plane import (
    CONTROL_DEFINITIONS,
    ControlConflict,
    ControlPlaneService,
    ControlPlaneSnapshot,
    ControlPlaneUnavailable,
    RunLaunchUncertain,
    RunNowCommand,
    RunStarted,
    ScheduleChangeCommand,
    ScheduleChanged,
    ScheduleStateConflict,
    ScheduleStatus,
    unavailable_control_plane_service,
)
from job_finder.pipeline.work_recoveries import (
    RecoveryAction,
    WorkRecoveryCommand,
    WorkRecoveryResult,
)
from job_finder.operations.health import (
    OperationsHealth,
    OperationsSnapshot,
    QueueCounts,
    SpendSummary,
)
from job_finder.operations.service import OperationsService

from job_finder.review.test_app_support import (
    helper_default_submit_review as _default_submit_review,
    helper_unexpected_control_call as _unexpected_control_call,
    helper_client as _client,
    helper_authenticate as _authenticate,
    helper_queue as _queue,
    helper_item as _item,
    helper_csrf as _csrf,
    helper_shell_link as _shell_link,
    helper_control_service as _control_service,
    helper_operations_snapshot as _operations_snapshot,
    helper_activity_run_entry as _activity_run_entry,
    helper_activity_service as _activity_service,
    helper_applied_dismissal as _applied_dismissal,
    helper_applied_recovery as _applied_recovery,
    helper_accepted_reevaluation as _accepted_reevaluation,
    CONTROL_DESCRIPTION_MATCHES as _CONTROL_DESCRIPTION_MATCHES,
    TODAY,
    NOW,
    SETTINGS,
    OWNER_PASSWORD,
    BOOTSTRAP_TOKEN,
    OWNER_ACCESS,
)


def test_http_route_manifest_stays_stable() -> None:
    app = create_review_app(
        lambda: _queue(),
        SETTINGS,
        submit_review=_default_submit_review,
        owner_access_service=OWNER_ACCESS,
        now=lambda: NOW,
    )
    get_paths = {
        "/",
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
        "/setup/test-search",
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


def test_security_and_session_middleware_contract_stays_stable() -> None:
    secure_settings = ReviewAppSettings(
        bootstrap_token=SecretStr(BOOTSTRAP_TOKEN),
        session_secret="s" * 32,
        cookie_secure=True,
    )
    app = create_review_app(
        lambda: _queue(),
        secure_settings,
        submit_review=_default_submit_review,
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
            unexpected_queue_load,
            SETTINGS,
            submit_review=_default_submit_review,
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
        lambda: _queue(),
        SETTINGS,
        submit_review=_default_submit_review,
        owner_access_service=OWNER_ACCESS,
        now=lambda: NOW,
    )

    response = TestClient(app).get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2F"


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


def test_the_control_plane_page_reports_an_unreachable_dagster(
    caplog: pytest.LogCaptureFixture,
) -> None:
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
    assert "Dagster control load failed: Dagster GraphQL request failed" in caplog.text


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
        change_schedule=lambda command: (
            calls.append(command) or ScheduleChanged(ScheduleStatus.STOPPED, False)
        ),
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
        change_schedule=lambda command: (
            calls.append(command) or ScheduleChanged(ScheduleStatus.STOPPED, False)
        ),
    )
    operations = OperationsService(
        load=lambda: _operations_snapshot(),
        recover=lambda command: calls.append(command) or _applied_recovery(command),
        reevaluate=lambda command: calls.append(command) or _accepted_reevaluation(command),
        dismiss=lambda command: calls.append(command) or _applied_dismissal(command),
    )
    app = create_review_app(
        lambda: _queue(),
        SETTINGS,
        submit_review=_default_submit_review,
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
        change_schedule=lambda command: (
            calls.append(command)
            or ScheduleStateConflict(
                expected=ScheduleStatus.RUNNING,
                observed=ScheduleStatus.STOPPED,
            )
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


def test_unconfigured_work_recovery_names_the_missing_service(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _client(_queue(), operations=OperationsService(load=lambda: _operations_snapshot()))

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

    assert response.status_code == 503
    assert "Work recovery is not configured for this deployment" in response.text
    assert "database" not in response.text.lower()
    assert "Work recovery failed: OperationsUnavailable" in caplog.text


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
