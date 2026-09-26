# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUntypedFunctionDecorator=false, reportUnusedFunction=false, reportMissingTypeStubs=false
from __future__ import annotations

import json
import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal, assert_never
from urllib.parse import urlencode
from uuid import UUID

import psycopg
from fasthtml.common import (
    H1,
    H2,
    A,
    Button,
    Details,
    Div,
    FastHTML,
    Form,
    Input,
    Label,
    Li,
    Ol,
    Option,
    P,
    Pre,
    Request,
    Script,
    Select,
    Small,
    Span,
    Strong,
    Summary,
    Ul,
)
from starlette.datastructures import FormData, QueryParams
from starlette.responses import HTMLResponse, RedirectResponse

from job_finder.operations._common import OperationsUnavailable
from job_finder.operations.activity import (
    ACTIVITY_STATUSES,
    ActivityEntry,
    ActivityPage,
    ActivityQuery,
    ActivityRun,
    ActivityService,
    ActivityWork,
)
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
    ScheduleView,
)
from job_finder.operations.run_history import RunDetail, RunNotFound, RunsService
from job_finder.operations.service import OperationsService
from job_finder.operations.spend import (
    AnalyticsService,
    DaySpend,
    ModelSpend,
    SpendAnalytics,
)
from job_finder.operations.work_history import (
    JobVerdict,
    ModelCallDetail,
    WorkAttemptSummary,
    WorkItemDetail,
    WorkItemNotFound,
)
from job_finder.pipeline.reevaluations import (
    JobReevaluationAccepted,
    JobReevaluationActiveWork,
    JobReevaluationCommand,
    JobReevaluationKeyConflict,
    JobReevaluationNotFound,
    JobReevaluationSourceChanged,
    JobReevaluationUnsupported,
)
from job_finder.pipeline.work_dismissals import (
    DismissalAction,
    WorkDismissalActiveLease,
    WorkDismissalApplied,
    WorkDismissalCommand,
    WorkDismissalKeyConflict,
    WorkDismissalNotFound,
    WorkDismissalStaleState,
)
from job_finder.pipeline.work_recoveries import (
    RecoveryAction,
    WorkRecoveryActiveLease,
    WorkRecoveryApplied,
    WorkRecoveryCommand,
    WorkRecoveryKeyConflict,
    WorkRecoveryNotFound,
    WorkRecoveryStaleState,
)
from job_finder.web.assets import static_url
from job_finder.web.security import (
    csrf_token,
    verified_control_csrf_token,
)
from job_finder.web.shell import (
    absolute_time,
    document,
    operations_sidebar_page,
    state_response,
    timestamp,
)

DateTimeClock = Callable[[], datetime]

_OPERATIONS_NOTICES = {
    "run-started": "Run submitted to Dagster.",
    "run-replayed": "This run request was already submitted.",
    "schedule-paused": "Schedule paused.",
    "schedule-resumed": "Schedule resumed.",
    "schedule-replayed": "Schedule already had the requested state.",
    "reevaluation-requested": "Reevaluation queued with the current release target.",
    "reevaluation-replayed": "This reevaluation request was already queued.",
}
_WORK_ACTION_NOTICES = {
    "work-retried": "Work is ready for the next worker now.",
    "terminal-recovered": "Terminal work recovered with a fresh attempt budget.",
    "recovery-replayed": "This recovery request was already applied.",
    "work-dismissed": "Dismissed. It stays quiet unless this work fails again.",
    "dismiss-replayed": "This dismissal request was already applied.",
    "dismiss-undone": "Dismissal undone. The failure needs attention again.",
    "dismiss-undo-replayed": "This undo request was already applied.",
}


def register_operations_routes(
    app: FastHTML,
    *,
    operations: OperationsService,
    runs: RunsService,
    activity: ActivityService,
    analytics: AnalyticsService,
    controls: ControlPlaneService,
    dagster_configured: bool,
    actor: str,
    now: DateTimeClock,
) -> None:
    @app.route("/operations", methods=["GET"], name="create_review_app_operations_page")
    def operations_page(request: Request) -> RedirectResponse:
        _ = request
        return RedirectResponse("/operations/runs", status_code=303)

    @app.route("/operations/control", methods=["GET"], name="create_review_app_control_plane_page")
    def control_plane_page(request: Request) -> HTMLResponse:
        token = csrf_token(request)
        if token is None:
            return HTMLResponse(status_code=401)
        try:
            control_snapshot = controls.load()
        except ControlPlaneUnavailable as error:
            control_snapshot = None
            control_error = str(error)
        else:
            control_error = None
        notice = _OPERATIONS_NOTICES.get(request.query_params.get("notice", ""))
        return HTMLResponse(
            document(
                operations_sidebar_page(
                    "control",
                    token,
                    _control_page(
                        control_snapshot,
                        token,
                        dagster_configured=dagster_configured,
                        control_error=control_error,
                        run_key=secrets.token_urlsafe(32),
                        notice=notice,
                        now=now(),
                    ),
                ),
                title="Control plane",
            )
        )

    @app.route("/operations/run", methods=["POST"], name="create_review_app_run_operation")
    async def run_operation(request: Request) -> HTMLResponse | RedirectResponse:
        form = await request.form()
        csrf_token = verified_control_csrf_token(request, form)
        if csrf_token is None:
            return _operations_forbidden_response()
        try:
            job_name = _required_control_form_text(form, "job_name")
            idempotency_key = _required_control_form_text(form, "idempotency_key")
        except ValueError as error:
            return _malformed_operations_response(str(error))
        try:
            result = controls.run_now(
                RunNowCommand(
                    job_name=job_name,
                    idempotency_key=idempotency_key,
                    actor=actor,
                    timestamp=now(),
                )
            )
        except ControlPlaneUnavailable as error:
            return _operations_unavailable_response(str(error))
        if isinstance(result, RunStarted):
            notice = "run-replayed" if result.replayed else "run-started"
            return RedirectResponse(f"/operations/control?notice={notice}", status_code=303)
        if isinstance(result, ControlConflict):
            return _operations_conflict_response(result.reason)
        if isinstance(result, RunLaunchUncertain):
            return _uncertain_run_response(
                csrf_token,
                job_name=job_name,
                idempotency_key=idempotency_key,
                detail=result.reason,
            )
        return _operations_unavailable_response(result.reason)

    @app.route("/operations/schedule", methods=["POST"], name="create_review_app_change_schedule")
    async def change_schedule(request: Request) -> HTMLResponse | RedirectResponse:
        form = await request.form()
        if verified_control_csrf_token(request, form) is None:
            return _operations_forbidden_response()
        try:
            schedule_name = _required_control_form_text(form, "schedule_name")
            expected = ScheduleStatus(_required_control_form_text(form, "expected_state"))
            desired = ScheduleStatus(_required_control_form_text(form, "desired_state"))
        except ValueError as error:
            return _malformed_operations_response(str(error))
        try:
            result = controls.change_schedule(
                ScheduleChangeCommand(
                    schedule_name=schedule_name,
                    expected_state=expected,
                    desired_state=desired,
                    actor=actor,
                    timestamp=now(),
                )
            )
        except ControlPlaneUnavailable as error:
            return _operations_unavailable_response(str(error))
        if isinstance(result, ScheduleChanged):
            notice = (
                "schedule-replayed"
                if result.replayed
                else "schedule-resumed"
                if result.status is ScheduleStatus.RUNNING
                else "schedule-paused"
            )
            return RedirectResponse(f"/operations/control?notice={notice}", status_code=303)
        if isinstance(result, ScheduleStateConflict):
            return _operations_conflict_response(
                "Schedule state changed before this request. "
                + f"Expected {result.expected.value}; observed {result.observed.value}."
            )
        if isinstance(result, ControlConflict):
            return _operations_conflict_response(result.reason)
        return _operations_unavailable_response(result.reason)

    @app.route("/operations/recovery", methods=["POST"], name="create_review_app_recover_operation")
    async def recover_operation(request: Request) -> HTMLResponse | RedirectResponse:
        form = await request.form()
        if verified_control_csrf_token(request, form) is None:
            return _operations_forbidden_response()
        try:
            action = RecoveryAction(_required_control_form_text(form, "action"))
            expected_state = _actionable_work_state(
                _required_control_form_text(form, "expected_state")
            )
            command = WorkRecoveryCommand(
                idempotency_key=_required_control_form_text(form, "idempotency_key"),
                job_id=UUID(_required_control_form_text(form, "job_id")),
                action=action,
                expected_state=expected_state,
                expected_attempt_count=int(
                    _required_control_form_text(form, "expected_attempt_count")
                ),
                actor=actor,
                requested_at=now(),
            )
        except ValueError as error:
            return _malformed_operations_response(str(error))
        try:
            result = operations.recover(command)
        except (OperationsUnavailable, psycopg.Error):
            return _work_recovery_unavailable_response()
        match result:
            case WorkRecoveryApplied():
                notice = (
                    "recovery-replayed"
                    if result.replayed
                    else "work-retried"
                    if command.action is RecoveryAction.RETRY_NOW
                    else "terminal-recovered"
                )
                return RedirectResponse(
                    f"/operations/work/{command.job_id}?notice={notice}", status_code=303
                )
            case WorkRecoveryKeyConflict():
                return _operations_conflict_response(
                    "This recovery request key belongs to another command."
                )
            case WorkRecoveryActiveLease():
                return _operations_conflict_response(
                    "This work item was actively leased when the command was recorded and was not changed."
                )
            case WorkRecoveryStaleState():
                observed = result.receipt.prior_state or "missing"
                return _operations_conflict_response(
                    f"Work state changed before this request. Observed {observed}."
                )
            case WorkRecoveryNotFound():
                return _work_recovery_not_found_response()
            case _:
                assert_never(result)

    @app.route("/operations/runs", methods=["GET"], name="create_review_app_pipeline_runs_page")
    def pipeline_runs_page(request: Request) -> HTMLResponse:
        query = _activity_query_from_params(request.query_params)
        try:
            page = activity.list(query)
        except (OperationsUnavailable, psycopg.Error):
            return state_response(
                "Recent activity is unavailable",
                "The database could not be reached. Reload this page to try again.",
                status_code=503,
            )
        token = csrf_token(request)
        if token is None:
            return HTMLResponse(status_code=401)
        notice = _OPERATIONS_NOTICES.get(request.query_params.get("notice", ""))
        next_href = (
            _activity_href(request.query_params, cursor=page.next_cursor)
            if page.next_cursor is not None
            else None
        )
        return HTMLResponse(
            document(
                operations_sidebar_page(
                    "activity",
                    token,
                    _activity_content(
                        page,
                        filters=request.query_params,
                        next_href=next_href,
                        notice=notice,
                        now=now(),
                    ),
                ),
                title="Recent activity",
            )
        )

    @app.route(
        "/operations/runs/{run_id}", methods=["GET"], name="create_review_app_run_detail_page"
    )
    def run_detail_page(run_id: str, request: Request) -> HTMLResponse:
        try:
            parsed_id = UUID(run_id)
        except ValueError:
            return _run_not_found_response()
        try:
            detail = runs.detail(parsed_id)
        except RunNotFound:
            return _run_not_found_response()
        except psycopg.Error:
            return state_response(
                "Pipeline runs are unavailable",
                "The database could not be reached. Reload this page to try again.",
                status_code=503,
            )
        token = csrf_token(request)
        if token is None:
            return HTMLResponse(status_code=401)
        return HTMLResponse(
            document(
                operations_sidebar_page(
                    "activity",
                    token,
                    _run_detail_page(detail, now=now()),
                ),
                title="Pipeline run",
            )
        )

    @app.route(
        "/operations/work/{job_id}", methods=["GET"], name="create_review_app_work_item_page"
    )
    def work_item_page(job_id: str, request: Request) -> HTMLResponse:
        try:
            parsed_id = UUID(job_id)
        except ValueError:
            return _work_item_not_found_response()
        try:
            detail = operations.work_detail(parsed_id)
        except WorkItemNotFound:
            return _work_item_not_found_response()
        except (OperationsUnavailable, psycopg.Error):
            return state_response(
                "Work item detail is unavailable",
                "The database could not be reached. Reload this page to try again.",
                status_code=503,
            )
        token = csrf_token(request)
        if token is None:
            return HTMLResponse(status_code=401)
        notice = _WORK_ACTION_NOTICES.get(request.query_params.get("notice", ""))
        return HTMLResponse(
            document(
                operations_sidebar_page(
                    "activity",
                    token,
                    _work_item_page(detail, token, notice=notice),
                ),
                title="Job",
            )
        )

    @app.route("/operations/failures", methods=["GET"], name="create_review_app_failures_page")
    def failures_page(request: Request) -> RedirectResponse:
        _ = request
        return RedirectResponse(
            "/operations/runs?status=failed&status=retrying&status=terminal",
            status_code=303,
        )

    @app.route(
        "/operations/analytics", methods=["GET"], name="create_review_app_spend_analytics_page"
    )
    def spend_analytics_page(request: Request) -> HTMLResponse:
        try:
            spend = analytics.load()
        except psycopg.Error:
            return state_response(
                "Model spend analytics are unavailable",
                "The database could not be reached. Reload this page to try again.",
                status_code=503,
            )
        token = csrf_token(request)
        if token is None:
            return HTMLResponse(status_code=401)
        return HTMLResponse(
            document(
                operations_sidebar_page(
                    "analytics",
                    token,
                    _analytics_page(spend),
                ),
                title="Model spend",
                scripts=(
                    Script(src=static_url("frappe-charts.min.umd.js")),
                    Script(src=static_url("spend-chart-init.js")),
                    Script(src=static_url("latency-chart-init.js")),
                ),
            )
        )

    @app.route("/operations/dismiss", methods=["POST"], name="create_review_app_dismiss_operation")
    async def dismiss_operation(request: Request) -> HTMLResponse | RedirectResponse:
        form = await request.form()
        if verified_control_csrf_token(request, form) is None:
            return _operations_forbidden_response()
        try:
            action = DismissalAction(_required_control_form_text(form, "action"))
            command = WorkDismissalCommand(
                idempotency_key=_required_control_form_text(form, "idempotency_key"),
                job_id=UUID(_required_control_form_text(form, "job_id")),
                action=action,
                expected_attempt_count=int(
                    _required_control_form_text(form, "expected_attempt_count")
                ),
                actor=actor,
                requested_at=now(),
            )
        except ValueError as error:
            return _malformed_operations_response(str(error))
        try:
            result = operations.dismiss(command)
        except (OperationsUnavailable, psycopg.Error):
            return _operations_unavailable_response("Work dismissal is unavailable")
        match result:
            case WorkDismissalApplied():
                if command.action is DismissalAction.DISMISS:
                    notice = "dismiss-replayed" if result.replayed else "work-dismissed"
                else:
                    notice = "dismiss-undo-replayed" if result.replayed else "dismiss-undone"
                return RedirectResponse(
                    f"/operations/work/{command.job_id}?notice={notice}", status_code=303
                )
            case WorkDismissalKeyConflict():
                return _operations_conflict_response(
                    "This dismissal request key belongs to another command."
                )
            case WorkDismissalActiveLease():
                return _operations_conflict_response(
                    "This work item was actively leased when the command was recorded and was not changed."
                )
            case WorkDismissalStaleState():
                return _operations_conflict_response(
                    "The work item changed before this request. Reload and try again."
                )
            case WorkDismissalNotFound():
                return _operations_conflict_response(
                    "This work item (or its dismissal) no longer exists."
                )
            case _:
                assert_never(result)

    @app.route(
        "/operations/reevaluation", methods=["POST"], name="create_review_app_request_reevaluation"
    )
    async def request_reevaluation(request: Request) -> HTMLResponse | RedirectResponse:
        form = await request.form()
        csrf_token = verified_control_csrf_token(request, form)
        if csrf_token is None:
            return _operations_forbidden_response()
        try:
            command = JobReevaluationCommand(
                idempotency_key=_required_control_form_text(form, "idempotency_key"),
                expected_decision_id=_required_control_form_text(form, "expected_decision_id"),
                expected_snapshot_id=_required_control_form_text(form, "expected_snapshot_id"),
                actor=actor,
                requested_at=now(),
            )
        except ValueError as error:
            return _malformed_operations_response(str(error))
        try:
            result = operations.reevaluate(command)
        except (OperationsUnavailable, psycopg.Error):
            return _reevaluation_unavailable_response(csrf_token, command)
        match result:
            case JobReevaluationAccepted():
                notice = "reevaluation-replayed" if result.replayed else "reevaluation-requested"
                return RedirectResponse(f"/operations/runs?notice={notice}", status_code=303)
            case JobReevaluationKeyConflict():
                return _operations_conflict_response(
                    "This reevaluation request key belongs to another command."
                )
            case JobReevaluationSourceChanged():
                return _operations_conflict_response(
                    result.receipt.conflict_reason
                    or "The source decision changed before this request."
                )
            case JobReevaluationActiveWork():
                return _operations_conflict_response(
                    result.receipt.conflict_reason
                    or "This job already has active or unresolved work."
                )
            case JobReevaluationUnsupported():
                return _operations_conflict_response(
                    result.receipt.conflict_reason or "This job cannot be reevaluated safely."
                )
            case JobReevaluationNotFound():
                return _reevaluation_not_found_response()
            case _:
                assert_never(result)


_CONTROL_DESCRIPTIONS = {
    "job_finder": (
        "Runs every configured search, registers what it finds in the work queue, then "
        + "pushes a first batch through scrape, filters, and evaluation. Whatever it does "
        + "not finish waits in the queue."
    ),
    "job_work_queue": (
        "Drains the work queue: claims due jobs and pushes them through scrape, filters, "
        + "and evaluation, so a job found this morning is decided today. The queue only "
        + "holds what discovery and retries put there, so quiet ticks finish in seconds."
    ),
    "onboarding_test_search": (
        "Processes only the bounded test search requested during setup. It never claims "
        + "jobs from the normal work queue."
    ),
    "review_sample": (
        "Enqueues a sample of yesterday's rejected jobs so you can audit them in the "
        + "review queue."
    ),
    "langfuse_projection": ("Ships telemetry to Langfuse. It never influences decisions."),
}

_PIPELINE_STEPS = (
    (
        "Search",
        "The Full pipeline run searches Jina with every configured query and registers "
        + "each job URL it finds as a work item in the Postgres work queue. Setup test "
        + "searches use their pinned configuration and stop at their query and URL limits.",
    ),
    (
        "Claim",
        "A worker claims one work item at a time under a short lease, so two runs can "
        + "never work on the same job. Setup test search work has its own request scope.",
    ),
    (
        "Scrape",
        "Jina fetches the full posting. A scrape too thin to read goes back to the "
        + "queue for a later try.",
    ),
    (
        "Filter",
        "Cheap deterministic checks run first: ATS evidence (Greenhouse, Lever, Ashby, "
        + "Workable), then structural rules. Most jobs stop here.",
    ),
    (
        "Evaluate",
        "Surviving jobs are scored by the active prompt release against your criteria. "
        + "Every model call is recorded in Postgres.",
    ),
    (
        "Decide",
        "Qualified jobs enter today's review queue; rejections are recorded, and a "
        + "daily sample of them is queued for audit.",
    ),
)


def _control_page(
    snapshot: ControlPlaneSnapshot | None,
    csrf_token: str,
    *,
    dagster_configured: bool,
    control_error: str | None,
    run_key: str,
    notice: str | None,
    now: datetime,
) -> object:
    return Div(
        P(notice, cls="operations-notice", role="status") if notice else None,
        Div(
            Small("Owner operations", cls="eyebrow"),
            H1("Control plane"),
            P(
                "What runs in the background, when, and why. Dagster owns the schedules; "
                + "this page reads them and commands them. Changes are re-checked before "
                + "applying, and every launch is idempotent.",
                cls="operations-intro",
            ),
            cls="operations-header",
        ),
        Div(
            Small("How it works", cls="eyebrow"),
            H2("From search to decision"),
            Ol(
                *(
                    Li(Strong(name), P(description), cls=f"pipeline-step-{name.lower()}")
                    for name, description in _PIPELINE_STEPS
                ),
                cls="pipeline-steps",
            ),
            P(
                "The Work queue schedule repeats the claim-through-decide steps every 15 "
                + "minutes, so a job found this morning is not decided tomorrow, and failed "
                + "work comes back there on retry. Review sample fills your daily audit. The "
                + "Langfuse projection copies telemetry out after decisions are made — it "
                + "never changes one. A schedule skips its tick while an earlier run of the "
                + "same job is queued or running.",
                cls="operations-muted",
            ),
            cls="operations-section",
        ),
        Div(
            Small("Schedules", cls="eyebrow"),
            H2("Schedules"),
            P(
                "Current state from Dagster. Changes are re-checked before applying.",
                cls="operations-muted",
            )
            if snapshot is not None
            else _controls_off_banner(
                dagster_configured=dagster_configured, control_error=control_error
            ),
            Ul(
                *(
                    _schedule_row(schedule, csrf_token, run_key, now=now)
                    for schedule in snapshot.schedules
                ),
                cls="schedule-list",
            )
            if snapshot is not None
            else Ul(
                *(
                    _unavailable_schedule_row(
                        definition.label,
                        definition.cadence,
                        _CONTROL_DESCRIPTIONS.get(definition.job_name),
                    )
                    for definition in CONTROL_DEFINITIONS
                ),
                cls="schedule-list",
            ),
            cls="operations-section",
        ),
        cls="review-shell operations-shell",
    )


def _controls_off_banner(
    *,
    dagster_configured: bool,
    control_error: str | None,
) -> object:
    if not dagster_configured:
        return Div(
            Strong("Schedule controls are off: Dagster is not configured for this app."),
            P(
                "This page reads live schedule state from the Dagster API. Set "
                + "JOB_FINDER_DAGSTER_GRAPHQL_URL (and optionally "
                + "JOB_FINDER_DAGSTER_REPOSITORY_LOCATION and "
                + "JOB_FINDER_DAGSTER_REPOSITORY_NAME) in the review app's environment, "
                + "then restart. Until then every schedule below shows as Unavailable and "
                + "its buttons do nothing. Pipeline evidence elsewhere stays current.",
            ),
            cls="operations-alert",
        )
    return Div(
        Strong("Schedule controls are off: this app cannot reach Dagster."),
        P(
            f"The Dagster API reported: {control_error}. The schedules live in Dagster; "
            + "this page only reads and commands them, so while it cannot connect, every "
            + "schedule below shows as Unavailable and its buttons do nothing. Check that "
            + "the Dagster webserver is up and that JOB_FINDER_DAGSTER_GRAPHQL_URL points "
            + "at it from this app, then reload. Pipeline evidence elsewhere stays current.",
        ),
        cls="operations-alert",
    )


def _schedule_row(
    schedule: ScheduleView, csrf_token: str, run_key: str, *, now: datetime
) -> object:
    desired = (
        ScheduleStatus.STOPPED
        if schedule.status is ScheduleStatus.RUNNING
        else ScheduleStatus.RUNNING
    )
    return Li(
        Div(
            Div(
                Strong(schedule.definition.label),
                Span(
                    schedule.status.value.title(),
                    cls=f"schedule-state {schedule.status.value.lower()}",
                ),
            ),
            P(schedule.definition.cadence, cls="schedule-cadence"),
            P(
                _CONTROL_DESCRIPTIONS.get(schedule.definition.job_name),
                cls="operations-muted",
            ),
            P(
                "Next: ",
                timestamp(schedule.next_tick, now=now),
                cls="schedule-next",
            )
            if schedule.next_tick is not None
            else P("Next tick unavailable while stopped", cls="schedule-next"),
            Div(
                Form(
                    Input(type="hidden", name="csrf_token", value=csrf_token),
                    Input(type="hidden", name="job_name", value=schedule.definition.job_name),
                    Input(type="hidden", name="idempotency_key", value=run_key),
                    Button("Run now", type="submit", cls="operation-button"),
                    action="/operations/run",
                    method="post",
                ),
                Form(
                    Input(type="hidden", name="csrf_token", value=csrf_token),
                    Input(
                        type="hidden",
                        name="schedule_name",
                        value=schedule.definition.schedule_name,
                    ),
                    Input(
                        type="hidden",
                        name="expected_state",
                        value=schedule.status.value,
                    ),
                    Input(type="hidden", name="desired_state", value=desired.value),
                    Button(
                        "Pause" if desired is ScheduleStatus.STOPPED else "Resume",
                        type="submit",
                        cls="operation-button secondary",
                    ),
                    action="/operations/schedule",
                    method="post",
                ),
                cls="schedule-actions",
            ),
        )
    )


def _unavailable_schedule_row(label: str, cadence: str, description: str | None) -> object:
    return Li(
        Div(
            Div(Strong(label), Span("Unavailable", cls="schedule-state unavailable")),
            P(cadence, cls="schedule-cadence"),
            P(description, cls="operations-muted") if description else None,
            Div(
                Button("Run now", type="button", disabled=True, cls="operation-button"),
                Button(
                    "Pause or resume",
                    type="button",
                    disabled=True,
                    cls="operation-button secondary",
                ),
                cls="schedule-actions",
            ),
        )
    )


def _count_phrase(value: int, singular: str, plural: str) -> str:
    return f"{value} {singular if value == 1 else plural}"


def _activity_content(
    page: ActivityPage,
    *,
    filters: QueryParams,
    next_href: str | None,
    notice: str | None,
    now: datetime,
) -> object:
    query = _activity_query_from_params(filters)
    has_filters = bool(
        query.statuses or query.kind or query.from_at is not None or query.to_at is not None
    )
    hidden_note = (
        P(
            f"{_count_phrase(page.hidden_no_op_count, 'entry', 'entries')} that did nothing "
            + "hidden. Select 'Show entries that did nothing' to see them.",
            cls="operations-muted",
        )
        if not query.show_no_ops
        else None
    )
    empty = (
        P("All matching activity is hidden.", cls="operations-empty")
        if page.hidden_no_op_count and not query.show_no_ops
        else P("No activity matches these filters.", cls="operations-empty")
        if has_filters
        else P("No activity recorded yet.", cls="operations-empty")
    )
    rows = Ul(
        *(_activity_row(entry, now=now) for entry in page.entries),
        cls="operations-list run-list",
    )
    return Div(
        P(notice, cls="operations-notice", role="status") if notice else None,
        Div(
            Small("Owner operations", cls="eyebrow"),
            H1("Recent activity"),
            P(
                "A run is one pipeline pass; a job is one listing being worked on. "
                + "Newest activity appears first.",
                cls="operations-intro",
            ),
            cls="operations-header",
        ),
        _activity_filter_form(filters),
        hidden_note,
        rows if page.entries else empty,
        A("Next page →", href=next_href, cls="retry activity-next")
        if next_href is not None
        else None,
        cls="review-shell operations-shell",
    )


_ACTIVITY_STATUS_OPTIONS: tuple[tuple[str, str], ...] = (
    ("running", "Running"),
    ("completed", "Completed"),
    ("failed", "Failed"),
    ("retrying", "Retrying"),
    ("terminal", "Needs attention"),
    ("dismissed", "Dismissed"),
)
_ACTIVITY_KIND_OPTIONS = {
    "work": "Job",
    "orchestration": "Scheduler tick",
    "discovery": "Discovery run",
    "processing": "Processing run",
    "reconcile": "Reconciliation run",
    "evaluation": "Evaluation run",
}


def _run_kind_label(kind: str) -> str:
    return _ACTIVITY_KIND_OPTIONS.get(kind, f"{kind.replace('_', ' ').title()} run")


def _activity_status_label(status: str) -> str:
    return dict(_ACTIVITY_STATUS_OPTIONS).get(status, status.replace("_", " ").title())


def _activity_filter_form(params: QueryParams) -> object:
    selected = set(params.getlist("status"))
    kind = params.get("kind")
    return Form(
        Div(
            *(
                Label(
                    Input(
                        type="checkbox",
                        name="status",
                        value=value,
                        checked=True if value in selected else None,
                    ),
                    Span(label),
                    cls="filter-check",
                )
                for value, label in _ACTIVITY_STATUS_OPTIONS
            ),
            cls="filter-checks",
        ),
        Label(
            Input(
                type="checkbox",
                name="show_noops",
                value="1",
                checked=True if params.get("show_noops") == "1" else None,
            ),
            Span("Show entries that did nothing"),
            cls="filter-check",
        ),
        Div(
            Label(
                "Kind",
                Select(
                    Option("Any kind", value="", selected=True if not kind else None),
                    *(
                        Option(
                            label,
                            value=value,
                            selected=True if value == kind else None,
                        )
                        for value, label in _ACTIVITY_KIND_OPTIONS.items()
                    ),
                    name="kind",
                ),
                cls="filter-kind",
            ),
            Label(
                "From",
                Input(type="date", name="from", value=params.get("from") or None),
                cls="filter-date",
            ),
            Label(
                "To",
                Input(type="date", name="to", value=params.get("to") or None),
                cls="filter-date",
            ),
            Button("Apply filters", type="submit", cls="operation-button"),
            A("Clear", href="/operations/runs", cls="filter-clear"),
            cls="filter-controls",
        ),
        action="/operations/runs",
        method="get",
        cls="activity-filters",
    )


def _activity_row(entry: ActivityEntry, *, now: datetime) -> object:
    if isinstance(entry.item, ActivityRun):
        return _activity_run_row(entry, now=now)
    return _activity_work_row(entry, now=now)


def _activity_run_row(entry: ActivityEntry, *, now: datetime) -> object:
    run = entry.item
    assert isinstance(run, ActivityRun)
    timing = (timestamp(run.started_at, now=now),)
    if run.completed_at is not None:
        elapsed = max(0, int((run.completed_at - run.started_at).total_seconds()))
        timing = (timestamp(run.started_at, now=now), f" · {_format_duration(elapsed)}")
    headline = (
        "Nothing was due."
        if run.idle_tick
        else " · ".join(
            (
                f"{run.discoveries} discovered",
                f"{run.processed_jobs} processed",
                f"{run.model_calls} model calls",
            )
        )
    )
    return Li(
        A(
            Div(
                Div(
                    Strong(_run_kind_label(run.kind)),
                    Span(_activity_status_label(entry.status), cls="run-status"),
                    cls="row-head",
                ),
                Small(*timing),
                P(headline, cls="operations-muted"),
                Div(Strong("Open run →"), cls="run-link-hint"),
                cls="run-row",
            ),
            href=f"/operations/runs/{run.id}",
            cls="run-link",
        ),
    )


def _activity_work_row(entry: ActivityEntry, *, now: datetime) -> object:
    work = entry.item
    assert isinstance(work, ActivityWork)
    timing: tuple[object, ...] = (f"Attempt {work.attempt_count} · ",)
    if work.retry_at is not None:
        timing += ("Retry ", timestamp(work.retry_at, now=now))
    else:
        timing += (timestamp(work.occurred_at, now=now),)
    return Li(
        A(
            Div(
                Div(
                    Strong("Job"),
                    Span(_activity_status_label(entry.status), cls="run-status"),
                    cls="row-head",
                ),
                Small(*timing),
                P(work.failure_summary, cls="operations-muted") if work.failure_summary else None,
                Div(Strong("Open job →"), cls="run-link-hint"),
                cls="run-row",
            ),
            href=f"/operations/work/{work.job_id}",
            cls="run-link",
        ),
    )


def _activity_date(params: QueryParams, key: str) -> datetime | None:
    raw = params.get(key)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).replace(tzinfo=UTC)
    except ValueError:
        return None


def _activity_query_from_params(params: QueryParams) -> ActivityQuery:
    statuses = frozenset(value for value in params.getlist("status") if value in ACTIVITY_STATUSES)
    kind = params.get("kind")
    if kind not in _ACTIVITY_KIND_OPTIONS:
        kind = None
    try:
        return ActivityQuery(
            statuses=statuses,
            kind=kind,
            from_at=_activity_date(params, "from"),
            to_at=_activity_date(params, "to"),
            limit=50,
            cursor=params.get("cursor") or None,
            show_no_ops=params.get("show_noops") == "1",
        )
    except ValueError:
        return ActivityQuery(limit=50)


def _activity_href(params: QueryParams, *, cursor: str | None = None) -> str:
    pairs: list[tuple[str, str]] = [
        ("status", value) for value in params.getlist("status") if value in ACTIVITY_STATUSES
    ]
    kind = params.get("kind")
    if kind in _ACTIVITY_KIND_OPTIONS:
        pairs.append(("kind", kind))
    for key in ("from", "to"):
        value = params.get(key)
        if value and _activity_date(params, key) is not None:
            pairs.append((key, value))
    if params.get("show_noops") == "1":
        pairs.append(("show_noops", "1"))
    if cursor is not None:
        pairs.append(("cursor", cursor))
    if not pairs:
        return "/operations/runs"
    return "/operations/runs?" + urlencode(pairs)


def _run_detail_page(detail: RunDetail, *, now: datetime) -> object:
    item = detail.item
    timing = (timestamp(item.started_at, now=now),)
    if item.completed_at is not None:
        elapsed = max(0, int((item.completed_at - item.started_at).total_seconds()))
        timing = (timestamp(item.started_at, now=now), f" · {_format_duration(elapsed)}")
    return Div(
        Div(
            Small("Pipeline run", cls="eyebrow"),
            H1(_run_kind_label(item.kind)),
            P(
                Span(_activity_status_label(item.status), cls="run-status"),
                " · ",
                *timing,
                cls="operations-intro",
            ),
            P(
                "Nothing was due, so this run completed instantly."
                if item.idle_tick
                else " · ".join(
                    (
                        f"{item.discoveries} jobs discovered",
                        f"{item.processed_jobs} jobs processed",
                        f"{item.model_calls} model calls",
                        f"${item.known_cost_usd:,.4f} recorded spend",
                    )
                ),
                cls="operations-muted",
            ),
            P(item.error_summary, cls="operations-notice") if item.error_summary else None,
            A("← All runs", href="/operations/runs", cls="back-link"),
            cls="operations-header",
        ),
        Div(
            Small("Search keywords", cls="eyebrow"),
            H2("Discoveries"),
            Ul(
                *(
                    Li(Div(Strong(k.keyword), Span(f"{k.jobs} jobs")), cls="discovery-row")
                    for k in detail.keywords
                ),
                cls="operations-list",
            )
            if detail.keywords
            else P("No jobs were discovered by this run.", cls="operations-empty"),
            cls="operations-section",
        ),
        Div(
            Small("Per model and outcome", cls="eyebrow"),
            H2("Model calls"),
            Ul(
                *(
                    Li(
                        Div(
                            Div(
                                Strong(m.model),
                                Span(m.status, cls="run-status"),
                            ),
                            Small(
                                f"{m.calls} calls · {m.input_tokens} in / {m.output_tokens} out"
                                + f" · up to {m.max_latency_ms} ms · ${m.known_cost_usd:,.4f}"
                            ),
                        )
                        for m in detail.models
                    ),
                ),
                cls="operations-list",
            )
            if detail.models
            else P("No model calls were made by this run.", cls="operations-empty"),
            P(
                _count_phrase(
                    detail.unknown_cost_calls,
                    "call returned no usage, so it has no recorded cost.",
                    "calls returned no usage, so they have no recorded cost.",
                ),
                cls="operations-muted",
            )
            if detail.unknown_cost_calls
            else None,
            cls="operations-section",
        ),
        Div(
            Small("Decisions recorded by this run", cls="eyebrow"),
            H2("Decisions"),
            Ul(
                *(Li(Div(Strong(d.outcome.title()), Span(str(d.count)))) for d in detail.decisions),
                cls="operations-list",
            )
            if detail.decisions
            else P("No decisions were recorded by this run.", cls="operations-empty"),
            cls="operations-section",
        ),
        Div(
            Small("Up to 200 attempts", cls="eyebrow"),
            H2("Processing attempts"),
            Ul(
                *(
                    Li(
                        A(
                            Div(
                                Div(
                                    Strong(a.operation_key),
                                    Span(
                                        f"attempt {a.attempt_number} · {a.status}",
                                        cls="run-status",
                                    ),
                                ),
                                Small(str(a.job_id), cls="recovery-id"),
                                P(a.error_summary, cls="operations-muted")
                                if a.error_summary
                                else None,
                                Div(Strong("Inspect work →"), cls="run-link-hint"),
                            ),
                            href=f"/operations/work/{a.job_id}",
                            cls="run-link",
                        )
                        if a.job_id is not None
                        else Div(
                            Div(
                                Strong(a.operation_key),
                                Span(
                                    f"attempt {a.attempt_number} · {a.status}",
                                    cls="run-status",
                                ),
                            ),
                            P(a.error_summary, cls="operations-muted") if a.error_summary else None,
                        )
                        for a in detail.attempts
                    ),
                ),
                cls="operations-list",
            )
            if detail.attempts
            else P("No processing attempts were recorded by this run.", cls="operations-empty"),
            cls="operations-section",
        ),
        cls="review-shell operations-shell",
    )


def _run_not_found_response() -> HTMLResponse:
    return state_response(
        "Run not found",
        "This pipeline run does not exist.",
        action=A("Back to the runs", href="/operations/runs", cls="retry"),
        status_code=404,
    )


def _analytics_page(spend: SpendAnalytics) -> object:
    return Div(
        Div(
            Small("Owner operations", cls="eyebrow"),
            H1("Model spend"),
            P(
                "What the evaluation pipeline spends on model calls. Model spend only — "
                + "Jina (search and scrape) and Langfuse costs are not tracked.",
                cls="operations-intro",
            ),
            cls="operations-header",
        ),
        Div(
            _spend_metric(
                "Recorded spend", f"${spend.known_usd:,.4f}", "across every recorded call"
            ),
            _spend_metric(
                "Model calls",
                str(spend.calls),
                _count_phrase(spend.accepted, "accepted call", "accepted calls"),
            ),
            _spend_metric(
                "Errors",
                str(spend.errors),
                _count_phrase(spend.errors, "call returned no usage", "calls returned no usage"),
            ),
            _spend_metric(
                "Tokens",
                f"{spend.input_tokens + spend.output_tokens:,}",
                f"{spend.input_tokens:,} in / {spend.output_tokens:,} out",
            ),
            _spend_metric(
                "Slowest call",
                f"{spend.max_latency_ms:,} ms",
                "longest recorded call",
            ),
            cls="operations-metrics",
            aria_label="Model spend totals",
        ),
        _spend_days_section(spend.days),
        _spend_latency_section(spend.days) if spend.days else None,
        _spend_models_section(spend.models),
        cls="review-shell operations-shell",
    )


def _spend_metric(label: str, value: str, detail: str) -> object:
    return Div(
        Small(label),
        Strong(value),
        Span(detail),
    )


def _spend_days_section(days: tuple[DaySpend, ...]) -> object:
    return Div(
        Small("Last 30 days", cls="eyebrow"),
        H2("Spend per day"),
        _spend_chart(days)
        if days
        else P("No model calls were recorded in the last 30 days.", cls="operations-empty"),
        cls="operations-section",
    )


def _day_chart_frame(
    days: tuple[DaySpend, ...],
) -> tuple[tuple[DaySpend, ...], list[str], list[str], list[str]]:
    ordered = tuple(reversed(days))
    labels = [f"{day.day:%b} {day.day.day}" for day in ordered]
    details = []
    for label, day in zip(labels, ordered, strict=True):
        line = _count_phrase(day.accepted, "accepted call", "accepted calls")
        if day.errors:
            line += f" · {day.errors} returned no usage"
        details.append(f"{label}, {day.day.year} · {line}")
    totals: dict[str, Decimal] = {}
    for day in ordered:
        for part in day.by_model:
            totals[part.model] = totals.get(part.model, Decimal(0)) + part.known_cost_usd
    models = sorted(totals, key=lambda name: (-totals[name], name))
    return ordered, labels, details, models


def _spend_chart(days: tuple[DaySpend, ...]) -> object:
    ordered, labels, details, models = _day_chart_frame(days)
    datasets = []
    for model in models:
        model_costs = [
            next((part.known_cost_usd for part in day.by_model if part.model == model), Decimal(0))
            for day in ordered
        ]
        datasets.append(
            {
                "name": model,
                "values": [float(cost) for cost in model_costs],
                "costs": [f"${cost:,.4f}" for cost in model_costs],
            }
        )
    payload = json.dumps(
        {"labels": labels, "details": details, "datasets": datasets},
        ensure_ascii=False,
    ).replace("</", "<\\/")
    return Div(
        Div(id="spend-per-day-chart", cls="spend-chart"),
        Script(payload, type="application/json", id="spend-per-day-data"),
        role="img",
        aria_label="Bar chart of model spend per day over the last 30 days",
    )


def _spend_latency_section(days: tuple[DaySpend, ...]) -> object:
    return Div(
        Small("Last 30 days", cls="eyebrow"),
        H2("Call latency"),
        P(
            "For each model and day, 9 in 10 calls were faster than the bar "
            + "(the 90th percentile call time).",
            cls="operations-muted",
        ),
        _spend_latency_chart(days),
        cls="operations-section",
    )


def _spend_latency_chart(days: tuple[DaySpend, ...]) -> object:
    ordered, labels, details, models = _day_chart_frame(days)
    datasets = []
    for model in models:
        p90_latencies = [
            next((part.p90_latency_ms for part in day.by_model if part.model == model), None)
            for day in ordered
        ]
        datasets.append({"name": model, "values": p90_latencies})
    payload = json.dumps(
        {"labels": labels, "details": details, "datasets": datasets},
        ensure_ascii=False,
    ).replace("</", "<\\/")
    return Div(
        Div(id="latency-per-day-chart", cls="spend-chart"),
        Script(payload, type="application/json", id="latency-per-day-data"),
        role="img",
        aria_label="Bar chart of 90th percentile model call latency by day over the last 30 days",
    )


def _spend_models_section(models: tuple[ModelSpend, ...]) -> object:
    return Div(
        Small("Every recorded call, by requested model", cls="eyebrow"),
        H2("Spend by model"),
        Ul(
            *(
                Li(
                    Div(
                        Div(
                            Strong(m.model),
                            Strong(f"${m.known_cost_usd:,.4f}"),
                            cls="row-head",
                        ),
                        Small(
                            f"{m.calls} calls · {m.input_tokens} in / {m.output_tokens} out"
                            + f" · up to {m.max_latency_ms} ms"
                        ),
                    ),
                )
                for m in models
            ),
            cls="operations-list",
        )
        if models
        else P("No model calls were recorded.", cls="operations-empty"),
        cls="operations-section",
    )


def _format_duration(total_seconds: int) -> str:
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def _operations_forbidden_response() -> HTMLResponse:
    return state_response(
        "This operations form expired",
        "Reload operations and try again.",
        action=A("Reload operations", href="/", cls="retry"),
        status_code=403,
    )


def _malformed_operations_response(detail: str) -> HTMLResponse:
    return state_response(
        "Malformed operations form",
        detail,
        action=A("Reload operations", href="/", cls="retry"),
        status_code=400,
    )


def _operations_conflict_response(detail: str) -> HTMLResponse:
    return state_response(
        "Operations state changed",
        detail,
        action=A("Reload operations", href="/", cls="retry"),
        status_code=409,
    )


def _operations_unavailable_response(detail: str) -> HTMLResponse:
    return state_response(
        "Dagster control is unavailable",
        detail,
        action=A("Reload operations", href="/", cls="retry"),
        status_code=503,
    )


def _work_recovery_unavailable_response() -> HTMLResponse:
    return state_response(
        "Work recovery is unavailable",
        "The database could not apply this command. Reload operations and try again.",
        action=A("Reload operations", href="/", cls="retry"),
        status_code=503,
    )


def _work_recovery_not_found_response() -> HTMLResponse:
    return state_response(
        "Work item was not found",
        "The requested work item no longer exists.",
        action=A("Reload operations", href="/", cls="retry"),
        status_code=404,
    )


def _work_item_not_found_response() -> HTMLResponse:
    return state_response(
        "Work item was not found",
        "The requested work item does not exist or never reached the work queue.",
        action=A("Back to recent activity", href="/operations/runs", cls="retry"),
        status_code=404,
    )


_WORK_STATE_LABELS = {
    "pending": "Pending",
    "leased": "Running",
    "failed": "Retrying",
    "completed": "Completed",
    "terminal_error": "Needs attention",
}


def _work_item_page(
    detail: WorkItemDetail,
    csrf_token: str,
    *,
    notice: str | None,
) -> object:
    status = "Dismissed" if detail.dismissed else _WORK_STATE_LABELS.get(detail.state, detail.state)
    status_class = "dismissed" if detail.dismissed else detail.state
    return Div(
        P(notice, cls="operations-notice", role="status") if notice else None,
        Div(
            Small("Owner operations", cls="eyebrow"),
            H1("Job"),
            P(
                "Everything recorded for this work item, and the actions you can take on it.",
                cls="operations-intro",
            ),
            cls="operations-header",
        ),
        _work_item_verdict(detail.verdict),
        Div(
            Small("Current state", cls="eyebrow"),
            H2("Status"),
            Div(
                Strong("State"),
                Span(status, cls=f"schedule-state {status_class}"),
                Small(str(detail.job_id), cls="recovery-id"),
                cls="work-status-row",
            ),
            cls="operations-section",
        ),
        _work_item_facts(detail),
        _work_item_actions(detail, csrf_token),
        _work_attempt_history(detail.attempts),
        cls="review-shell operations-shell",
    )


_JOB_VERDICT_LABELS = {
    "qualified": "Qualified",
    "rejected": "Rejected",
    "duplicate": "Duplicate",
    "company_blocked": "Company blocked",
    "company_applied": "Applied",
}


def _work_item_verdict(verdict: JobVerdict | None) -> object:
    if verdict is None:
        return Div(
            Small("Latest decision", cls="eyebrow"),
            H2("Pipeline verdict"),
            P("No pipeline decision has been recorded yet.", cls="operations-muted"),
            cls="operations-section",
        )
    return Div(
        Small("Latest decision", cls="eyebrow"),
        H2("Pipeline verdict"),
        Strong(_JOB_VERDICT_LABELS[verdict.outcome]),
        P(verdict.reason),
        P(f"Matched profile: {verdict.matched_profile}") if verdict.matched_profile else None,
        Small(f"Decided {absolute_time(verdict.decided_at)}"),
        cls="operations-section",
    )


def _work_item_facts(detail: WorkItemDetail) -> object:
    rows: list[tuple[str, str]] = [("Attempt count", str(detail.attempt_count))]
    if detail.failure_summary:
        rows.append(("Failure", detail.failure_summary))
    if detail.retry_at is not None:
        rows.append(("Retry scheduled", absolute_time(detail.retry_at)))
    if detail.last_failed_at is not None:
        rows.append(("Last failed", absolute_time(detail.last_failed_at)))
    rows.append(("Created", absolute_time(detail.created_at)))
    if detail.completed_at is not None:
        rows.append(("Completed", absolute_time(detail.completed_at)))
    rows.append(("Dismissed", "Yes" if detail.dismissed else "No"))
    if detail.dismissed:
        if detail.dismissed_at is not None:
            rows.append(("Dismissed at", absolute_time(detail.dismissed_at)))
        if detail.dismissed_by:
            rows.append(("Dismissed by", detail.dismissed_by))
    return Div(
        Small("Work facts", cls="eyebrow"),
        H2("Facts"),
        Ul(
            *(Li(Div(Strong(label), Span(value), cls="row-head")) for label, value in rows),
            cls="operations-list",
        ),
        cls="operations-section",
    )


def _work_item_actions(detail: WorkItemDetail, csrf_token: str) -> object:
    forms: list[object] = []
    if detail.state == "failed":
        forms.append(_work_detail_form(detail, csrf_token, RecoveryAction.RETRY_NOW, "Retry now"))
    if detail.state == "terminal_error" and not detail.dismissed:
        forms.append(
            _work_detail_form(
                detail, csrf_token, RecoveryAction.RECOVER_TERMINAL, "Recover terminal work"
            )
        )
    if detail.state == "terminal_error":
        if detail.dismissed:
            forms.append(
                _work_detail_dismiss_form(
                    detail, csrf_token, DismissalAction.UNDO_DISMISS, "Undo dismissal"
                )
            )
        else:
            forms.append(
                _work_detail_dismiss_form(detail, csrf_token, DismissalAction.DISMISS, "Dismiss")
            )
    if not forms:
        return None
    return Div(
        Small("Actions", cls="eyebrow"),
        H2("Take action"),
        P(
            "Actions are idempotent: repeating one is safe, and conflicting changes are refused.",
            cls="operations-muted",
        ),
        Div(*forms, cls="schedule-actions work-actions"),
        cls="operations-section",
    )


def _work_detail_form(
    detail: WorkItemDetail, csrf_token: str, action: RecoveryAction, label: str
) -> object:
    return Form(
        Input(type="hidden", name="csrf_token", value=csrf_token),
        Input(type="hidden", name="job_id", value=str(detail.job_id)),
        Input(type="hidden", name="action", value=action.value),
        Input(type="hidden", name="expected_state", value=detail.state),
        Input(type="hidden", name="expected_attempt_count", value=str(detail.attempt_count)),
        Input(type="hidden", name="idempotency_key", value=secrets.token_urlsafe(32)),
        Button(label, type="submit", cls="operation-button"),
        action="/operations/recovery",
        method="post",
    )


def _work_detail_dismiss_form(
    detail: WorkItemDetail, csrf_token: str, action: DismissalAction, label: str
) -> object:
    return Form(
        Input(type="hidden", name="csrf_token", value=csrf_token),
        Input(type="hidden", name="job_id", value=str(detail.job_id)),
        Input(type="hidden", name="action", value=action.value),
        Input(type="hidden", name="expected_attempt_count", value=str(detail.attempt_count)),
        Input(type="hidden", name="idempotency_key", value=secrets.token_urlsafe(32)),
        Button(label, type="submit", cls="operation-button secondary"),
        action="/operations/dismiss",
        method="post",
    )


def _work_attempt_history(attempts: tuple[WorkAttemptSummary, ...]) -> object:
    rows = (
        Ul(
            *(
                Li(
                    Div(
                        Strong(f"{attempt.operation_key} · attempt {attempt.attempt_number}"),
                        Span(attempt.status, cls="run-status"),
                        cls="row-head",
                    ),
                    Small(
                        absolute_time(attempt.started_at)
                        if attempt.started_at is not None
                        else "Not started"
                    ),
                    P(attempt.error_summary, cls="operations-muted")
                    if attempt.error_summary
                    else None,
                    Small(f"{attempt.model_calls} model calls · ${attempt.known_cost_usd:,.4f}"),
                    Ul(*(_model_call_row(call) for call in attempt.calls), cls="operations-list")
                    if attempt.calls
                    else None,
                )
                for attempt in attempts
            ),
            cls="operations-list",
        )
        if attempts
        else P("No processing attempts were recorded.", cls="operations-empty")
    )
    return Div(
        Small("Every processing attempt", cls="eyebrow"),
        H2("History"),
        rows,
        cls="operations-section",
    )


def _model_call_row(call: ModelCallDetail) -> object:
    tokens = (
        f"{call.input_tokens if call.input_tokens is not None else 'Unknown'} input tokens"
        f" · {call.output_tokens if call.output_tokens is not None else 'Unknown'} output tokens"
    )
    cost = f"${call.cost_usd:,.4f}" if call.cost_usd is not None else "Cost unavailable"
    return Li(
        Div(
            Strong(call.prompt_name),
            Small(f"Version {call.prompt_version_id[:12]}", title=call.prompt_version_id),
            Span(call.status, cls="run-status"),
            cls="row-head",
        ),
        P(call.requested_model),
        Small(f"{tokens} · {cost} · {call.latency_ms} ms"),
        Pre(json.dumps(call.parsed_output, ensure_ascii=False, indent=2))
        if call.parsed_output is not None
        else P(call.error_summary or "No model answer recorded.", cls="operations-muted"),
        Details(
            Summary("Full request and response"),
            Small(f"Prompt version: {call.prompt_version_id}"),
            P("Request messages"),
            Pre(json.dumps(call.request_messages, ensure_ascii=False, indent=2)),
            P("Raw response"),
            Pre(json.dumps(call.raw_response, ensure_ascii=False, indent=2))
            if call.raw_response is not None
            else P("No raw response recorded.", cls="operations-muted"),
        ),
        cls="model-call-row",
    )


def _reevaluation_not_found_response() -> HTMLResponse:
    return state_response(
        "Source decision was not found",
        "The requested decision no longer exists.",
        action=A("Back to review", href="/", cls="retry"),
        status_code=404,
    )


def _reevaluation_unavailable_response(
    csrf_token: str, command: JobReevaluationCommand
) -> HTMLResponse:
    retry_form = Form(
        Input(type="hidden", name="csrf_token", value=csrf_token),
        Input(
            type="hidden",
            name="expected_decision_id",
            value=command.expected_decision_id,
        ),
        Input(
            type="hidden",
            name="expected_snapshot_id",
            value=command.expected_snapshot_id,
        ),
        Input(
            type="hidden",
            name="idempotency_key",
            value=command.idempotency_key,
        ),
        Button("Retry the same request", type="submit", cls="retry"),
        action="/operations/reevaluation",
        method="post",
    )
    return state_response(
        "Reevaluation status is uncertain",
        "The database did not confirm this request. Retry with the same key.",
        action=retry_form,
        status_code=503,
    )


def _uncertain_run_response(
    csrf_token: str,
    *,
    job_name: str,
    idempotency_key: str,
    detail: str,
) -> HTMLResponse:
    retry_form = Form(
        Input(type="hidden", name="csrf_token", value=csrf_token),
        Input(type="hidden", name="job_name", value=job_name),
        Input(type="hidden", name="idempotency_key", value=idempotency_key),
        Button("Retry the same request", type="submit", cls="retry"),
        action="/operations/run",
        method="post",
    )
    return state_response(
        "Run launch result is uncertain",
        f"{detail}. Retry with the same request so Dagster can be reconciled without relaunching.",
        action=retry_form,
        status_code=503,
    )


def _required_control_form_text(form: FormData, key: str) -> str:
    values = form.getlist(key)
    if len(values) != 1 or not isinstance(values[0], str) or not values[0]:
        raise ValueError(f"Expected one {key} value")
    return values[0]


def _actionable_work_state(value: str) -> Literal["failed", "terminal_error"]:
    if value == "failed" or value == "terminal_error":
        return value
    raise ValueError("Expected failed or terminal_error state")
