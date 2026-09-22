# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUntypedFunctionDecorator=false, reportUnusedFunction=false, reportMissingTypeStubs=false
from __future__ import annotations

import hmac
import secrets
import time
from collections import deque
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Literal, assert_never, cast
from urllib.parse import quote
from uuid import UUID

import psycopg
from anyio import Lock, to_thread
from fasthtml.common import (
    A,
    Beforeware,
    Body,
    Button,
    Div,
    Fieldset,
    Form,
    H1,
    H2,
    Head,
    Html,
    Input,
    Label,
    Legend,
    Li,
    Main,
    Meta,
    P,
    Pre,
    Section,
    Small,
    Span,
    Strong,
    Style,
    Textarea,
    Title,
    FastHTML,
    Request,
    Ul,
    to_xml,
)
from pydantic import SecretStr, ValidationError
from starlette.responses import PlainTextResponse
from starlette.datastructures import FormData
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from job_finder.config import ReviewAppSettings
from job_finder.configuration_service import (
    ActivateConfigurationCommand,
    ActiveConfigurationChanged,
    ConfigurationActivated,
    ConfigurationInvalid,
    ConfigurationPublished,
    DraftSaved,
    PublishConfigurationCommand,
    PublishDraftChanged,
    SaveDraftCommand,
)
from job_finder.review.configuration_editor import (
    apply_configuration_edit,
    CONFIGURATION_CSS,
    ConfigurationEditorService,
    MalformedConfigurationForm,
    RawConfigurationForm,
    authenticated_masthead,
    configuration_page,
    parse_configuration_form,
    publication_retry_page,
)
from job_finder.review.control_plane import (
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
    unavailable_control_plane_service,
)
from job_finder.review.models import (
    Compensation,
    ReviewConflict,
    ReviewItem,
    ReviewJob,
    ReviewQueue,
    ReviewSubmission,
)
from job_finder.review.operations import (
    ActionableWork,
    FailureSample,
    JobReevaluationAccepted,
    JobReevaluationActiveWork,
    JobReevaluationCommand,
    JobReevaluationKeyConflict,
    JobReevaluationNotFound,
    JobReevaluationSourceChanged,
    JobReevaluationUnsupported,
    OperationsHealth,
    OperationsService,
    OperationsSnapshot,
    OperationsUnavailable,
    PipelineRunSummary,
    RecoveryAction,
    WorkRecoveryActiveLease,
    WorkRecoveryApplied,
    WorkRecoveryCommand,
    WorkRecoveryKeyConflict,
    WorkRecoveryNotFound,
    WorkRecoveryStaleState,
    unknown_operations_service,
)
from job_finder.review.onboarding import OnboardingProgressService
from job_finder.review.owner_access import (
    MAXIMUM_PASSWORD_INPUT_LENGTH,
    MAXIMUM_PASSWORD_LENGTH,
    MINIMUM_PASSWORD_LENGTH,
    OnboardingStage,
    OwnerAccessService,
    OwnerBootstrapConflict,
)
from job_finder.review.postgres import ReviewService
from job_finder.provider_credentials import (
    ProviderCredentialChanged,
    ProviderCredentialRejected,
    ProviderKind,
    ProviderSetupService,
    ProviderSetupSnapshot,
    ProviderStageBlocked,
)
from job_finder.search_configuration import SearchConfigurationRevisionId

DateTimeClock = Callable[[], datetime]
ReadinessProbe = Callable[[], None]
SESSION_COOKIE = "job_finder_review_session"
SESSION_MAX_AGE = 14 * 24 * 60 * 60
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_CLIENTS = 1024
_SECURITY_HEADERS = (
    (b"cache-control", b"no-store"),
    (
        b"content-security-policy",
        b"default-src 'self'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'",
    ),
    (b"referrer-policy", b"no-referrer"),
    (b"strict-transport-security", b"max-age=63072000; includeSubDomains"),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
)
_CONFIGURATION_NOTICES = {
    "draft-saved": "Draft saved. It is not published or active yet.",
    "published": "Saved draft published. Active search configuration is unchanged.",
    "publication-replayed": "Publication confirmed from the original request. Active search configuration is unchanged.",
    "activated": "Published draft activated.",
}
_OPERATIONS_NOTICES = {
    "run-started": "Run submitted to Dagster.",
    "run-replayed": "This run request was already submitted.",
    "schedule-paused": "Schedule paused.",
    "schedule-resumed": "Schedule resumed.",
    "schedule-replayed": "Schedule already had the requested state.",
    "work-retried": "Work is ready for the next worker now.",
    "terminal-recovered": "Terminal work recovered with a fresh attempt budget.",
    "recovery-replayed": "This recovery request was already applied.",
    "reevaluation-requested": "Reevaluation queued with the current release target.",
    "reevaluation-replayed": "This reevaluation request was already queued.",
}


def create_review_app(
    service: ReviewService,
    configuration_service: ConfigurationEditorService,
    settings: ReviewAppSettings,
    *,
    owner_access_service: OwnerAccessService,
    provider_setup_service: ProviderSetupService | None = None,
    onboarding_progress_service: OnboardingProgressService | None = None,
    readiness: ReadinessProbe = lambda: None,
    operations_service: OperationsService | None = None,
    control_service: ControlPlaneService | None = None,
    actor: str = "owner",
    now: DateTimeClock = lambda: datetime.now(UTC),
) -> FastHTML:
    operations = operations_service or unknown_operations_service()
    controls = control_service or unavailable_control_plane_service()

    def require_owner(request: Request) -> Response | None:
        return _require_owner(request, owner_access_service)

    app = FastHTML(
        before=Beforeware(
            require_owner,
            skip=[r"/healthz", r"/readyz", r"/favicon.ico"],
        ),
        default_hdrs=False,
        htmx=False,
        surreal=False,
        secret_key=settings.session_secret,
        session_cookie=SESSION_COOKIE,
        max_age=SESSION_MAX_AGE,
        same_site="lax",
        sess_https_only=settings.cookie_secure,
    )
    login_failures: dict[str, deque[float]] = {}
    login_attempt_lock = Lock()

    @app.route("/healthz", methods=["GET"])
    def healthz() -> PlainTextResponse:
        return PlainTextResponse("ok")

    @app.route("/readyz", methods=["GET"])
    def readyz() -> PlainTextResponse:
        try:
            readiness()
        except psycopg.Error:
            return PlainTextResponse("database unavailable", status_code=503)
        return PlainTextResponse("ready")

    @app.route("/favicon.ico", methods=["GET"])
    def favicon() -> Response:
        return Response(status_code=204)

    @app.route("/setup", methods=["GET"])
    def setup_form(request: Request) -> HTMLResponse:
        csrf_token = request.session.get("csrf_token")
        if not isinstance(csrf_token, str):
            csrf_token = secrets.token_urlsafe(32)
            request.session["csrf_token"] = csrf_token
        return HTMLResponse(_document(_setup_content(csrf_token)))

    @app.route("/setup", methods=["POST"])
    async def setup_submit(request: Request) -> HTMLResponse | RedirectResponse:
        form = await request.form()
        if not _valid_csrf(request, _form_text(form, "csrf_token")):
            return HTMLResponse(
                _document(_setup_content("", "This setup form expired. Reload and try again.")),
                status_code=403,
            )
        csrf_token = request.session.get("csrf_token")
        assert isinstance(csrf_token, str)
        password = _form_text(form, "password")
        configured_token = settings.bootstrap_token
        supplied_token = _form_text(form, "bootstrap_token")
        if configured_token is None or not hmac.compare_digest(
            supplied_token, configured_token.get_secret_value()
        ):
            return HTMLResponse(
                _document(
                    _setup_content(
                        csrf_token,
                        "The bootstrap token is incorrect.",
                    )
                ),
                status_code=401,
            )
        if password != _form_text(form, "password_confirmation"):
            return HTMLResponse(
                _document(_setup_content(csrf_token, "Passwords differ.")),
                status_code=400,
            )
        try:
            result = await to_thread.run_sync(owner_access_service.bootstrap, password)
        except ValueError as error:
            return HTMLResponse(
                _document(_setup_content(csrf_token, str(error))),
                status_code=400,
            )
        except psycopg.Error:
            return _state_response(
                "Owner setup is unavailable",
                "The password was not confirmed. Reload this page and try again.",
                status_code=503,
            )
        if isinstance(result, OwnerBootstrapConflict):
            return _state_response(
                "Owner setup is already complete",
                "Sign in with the owner password that was created first.",
                action=A("Go to sign in", href="/login", cls="retry"),
                status_code=409,
            )
        _authenticate_session(request)
        return RedirectResponse("/setup/providers", status_code=303)

    @app.route("/setup/providers", methods=["GET"])
    def provider_setup_form(request: Request) -> HTMLResponse:
        if provider_setup_service is None:
            return _state_response(
                "Provider setup is unavailable",
                "Provider credential storage is not configured for this deployment.",
                status_code=503,
            )
        try:
            snapshot = provider_setup_service.inspect()
        except (psycopg.Error, RuntimeError):
            return _state_response(
                "Provider setup is unavailable",
                "Provider state could not be loaded. Try again after the database recovers.",
                status_code=503,
            )
        return HTMLResponse(
            _document(_provider_setup_content(_ensure_csrf_token(request), snapshot))
        )

    @app.route("/setup/providers", methods=["POST"])
    async def provider_setup_submit(request: Request) -> HTMLResponse | RedirectResponse:
        form = await request.form()
        if not _valid_csrf(request, _form_text(form, "csrf_token")):
            return _state_response(
                "Provider setup failed",
                "This setup form expired. Reload and try again.",
                status_code=403,
            )
        if provider_setup_service is None:
            return _state_response(
                "Provider setup is unavailable",
                "Provider credential storage is not configured for this deployment.",
                status_code=503,
            )
        try:
            provider = ProviderKind(_form_text(form, "provider"))
            expected_generation = int(_form_text(form, "expected_generation"))
        except (ValueError, TypeError):
            return _state_response(
                "Provider setup failed",
                "The provider request was invalid. Reload and try again.",
                status_code=400,
            )
        credential = _form_text(form, "credential")
        if not credential:
            return _state_response(
                "Provider setup failed",
                "Enter a provider credential.",
                status_code=400,
            )
        try:
            result = await to_thread.run_sync(
                provider_setup_service.replace,
                provider,
                SecretStr(credential),
                expected_generation,
                "owner",
                now(),
            )
        except (psycopg.Error, RuntimeError, ValueError):
            return _state_response(
                "Provider setup is unavailable",
                "The credential was not stored. Reload and try again.",
                status_code=503,
            )
        try:
            snapshot = provider_setup_service.inspect()
        except (psycopg.Error, RuntimeError):
            return _state_response(
                "Provider state is unavailable",
                "The credential request finished, but current state could not be reloaded.",
                status_code=503,
            )
        if isinstance(result, ProviderCredentialRejected):
            message = (
                "The provider rejected that credential."
                if result.error_code == "invalid_credentials"
                else "The provider could not be reached. The credential was not stored."
            )
            return HTMLResponse(
                _document(_provider_setup_content(_ensure_csrf_token(request), snapshot, message)),
                status_code=422,
            )
        if isinstance(result, ProviderCredentialChanged):
            return HTMLResponse(
                _document(
                    _provider_setup_content(
                        _ensure_csrf_token(request),
                        snapshot,
                        "This credential changed in another session. Review the current state.",
                    )
                ),
                status_code=409,
            )
        return RedirectResponse("/setup/providers", status_code=303)

    @app.route("/setup/providers/continue", methods=["POST"])
    async def provider_setup_continue(request: Request) -> HTMLResponse | RedirectResponse:
        form = await request.form()
        if not _valid_csrf(request, _form_text(form, "csrf_token")):
            return _state_response(
                "Provider setup failed",
                "This setup form expired. Reload and try again.",
                status_code=403,
            )
        if provider_setup_service is None:
            return _state_response(
                "Provider setup is unavailable",
                "Provider credential storage is not configured for this deployment.",
                status_code=503,
            )
        try:
            result = provider_setup_service.advance()
            snapshot = provider_setup_service.inspect()
        except (psycopg.Error, RuntimeError):
            return _state_response(
                "Provider setup is unavailable",
                "Provider state could not be confirmed. Reload and try again.",
                status_code=503,
            )
        if isinstance(result, ProviderStageBlocked):
            if result.state.stage is OnboardingStage.PREFERENCES:
                return RedirectResponse("/configuration", status_code=303)
            return HTMLResponse(
                _document(
                    _provider_setup_content(
                        _ensure_csrf_token(request),
                        snapshot,
                        "Validate every required provider before continuing.",
                    )
                ),
                status_code=409,
            )
        return RedirectResponse("/configuration", status_code=303)

    @app.route("/login", methods=["GET"])
    def login_form(request: Request) -> HTMLResponse:
        return HTMLResponse(
            _document(_login_content(_safe_next(request.query_params.get("next", "/"))))
        )

    @app.route("/login", methods=["POST"])
    async def login_submit(request: Request) -> HTMLResponse | RedirectResponse:
        return await _submit_login(
            request, owner_access_service, login_failures, login_attempt_lock
        )

    @app.route("/", methods=["GET"])
    def home(request: Request) -> HTMLResponse:
        try:
            snapshot = operations.load()
        except psycopg.Error:
            return _state_response(
                "Operations status is unavailable",
                "The database could not be reached. Reload this page to try again.",
                status_code=503,
            )
        csrf_token = request.session.get("csrf_token")
        if not isinstance(csrf_token, str):
            return HTMLResponse(status_code=401)
        try:
            control_snapshot = controls.load()
        except ControlPlaneUnavailable:
            control_snapshot = None
        return HTMLResponse(
            _document(
                _operations_page(
                    snapshot,
                    control_snapshot,
                    csrf_token,
                    run_key=secrets.token_urlsafe(32),
                    notice=_OPERATIONS_NOTICES.get(request.query_params.get("notice", "")),
                ),
                title="Job Finder operations",
            )
        )

    @app.route("/operations/run", methods=["POST"])
    async def run_operation(request: Request) -> HTMLResponse | RedirectResponse:
        form = await request.form()
        csrf_token = _verified_control_csrf_token(request, form)
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
            return RedirectResponse(f"/?notice={notice}", status_code=303)
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

    @app.route("/operations/schedule", methods=["POST"])
    async def change_schedule(request: Request) -> HTMLResponse | RedirectResponse:
        form = await request.form()
        if _verified_control_csrf_token(request, form) is None:
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
            return RedirectResponse(f"/?notice={notice}", status_code=303)
        if isinstance(result, ScheduleStateConflict):
            return _operations_conflict_response(
                "Schedule state changed before this request. "
                + f"Expected {result.expected.value}; observed {result.observed.value}."
            )
        if isinstance(result, ControlConflict):
            return _operations_conflict_response(result.reason)
        return _operations_unavailable_response(result.reason)

    @app.route("/operations/recovery", methods=["POST"])
    async def recover_operation(request: Request) -> HTMLResponse | RedirectResponse:
        form = await request.form()
        if _verified_control_csrf_token(request, form) is None:
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
                return RedirectResponse(f"/?notice={notice}", status_code=303)
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

    @app.route("/operations/reevaluation", methods=["POST"])
    async def request_reevaluation(request: Request) -> HTMLResponse | RedirectResponse:
        form = await request.form()
        csrf_token = _verified_control_csrf_token(request, form)
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
                return RedirectResponse(f"/?notice={notice}", status_code=303)
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

    @app.route("/review", methods=["GET"])
    def review_page(request: Request) -> HTMLResponse:
        try:
            queue = service.review_queue()
        except psycopg.Error:
            return _unavailable_response()
        csrf_token = request.session.get("csrf_token")
        if not isinstance(csrf_token, str):
            return HTMLResponse(status_code=401)
        return HTMLResponse(_document(_review_page(queue, csrf_token)))

    @app.route("/configuration", methods=["GET"])
    def configuration(request: Request) -> HTMLResponse:
        csrf_token = _csrf_token(request)
        if csrf_token is None:
            return HTMLResponse(status_code=401)
        try:
            state = configuration_service.inspect()
        except psycopg.Error:
            return _configuration_unavailable_response()
        notice = _CONFIGURATION_NOTICES.get(request.query_params.get("notice", ""))
        return HTMLResponse(
            _document(
                configuration_page(
                    state,
                    RawConfigurationForm.from_draft(state.draft),
                    csrf_token,
                    publication_key=secrets.token_urlsafe(32),
                    notice=notice,
                ),
                title="Search setup",
            )
        )

    @app.route("/configuration/edit", methods=["POST"])
    async def edit_configuration(request: Request) -> HTMLResponse:
        form = await request.form()
        csrf_token = _verified_csrf_token(request, form)
        if csrf_token is None:
            return _configuration_forbidden_response()
        try:
            action = _required_form_text(form, "action")
            changed = apply_configuration_edit(form, action)
            state = configuration_service.inspect()
        except MalformedConfigurationForm as error:
            return _malformed_configuration_response(str(error))
        except psycopg.Error:
            return _configuration_unavailable_response()
        return HTMLResponse(
            _document(
                configuration_page(
                    state,
                    changed,
                    csrf_token,
                    publication_key=secrets.token_urlsafe(32),
                    expanded=_affected_rows(action, changed),
                ),
                title="Search setup",
            )
        )

    @app.route("/configuration/preview", methods=["POST"])
    async def preview_configuration(request: Request) -> HTMLResponse:
        form = await request.form()
        csrf_token = _verified_csrf_token(request, form)
        if csrf_token is None:
            return _configuration_forbidden_response()
        try:
            raw = parse_configuration_form(form)
            validation = configuration_service.validate(raw.candidate())
            state = configuration_service.inspect()
        except MalformedConfigurationForm as error:
            return _malformed_configuration_response(str(error))
        except psycopg.Error:
            return _configuration_unavailable_response()
        if isinstance(validation, ConfigurationInvalid):
            return HTMLResponse(
                _document(
                    configuration_page(
                        state,
                        raw,
                        csrf_token,
                        publication_key=secrets.token_urlsafe(32),
                        validation=validation,
                    ),
                    title="Search setup",
                ),
                status_code=422,
            )
        detailed = configuration_service.preview(validation.configuration)
        return HTMLResponse(
            _document(
                configuration_page(
                    state,
                    raw,
                    csrf_token,
                    publication_key=secrets.token_urlsafe(32),
                    preview=detailed,
                ),
                title="Search setup preview",
            )
        )

    @app.route("/configuration/draft", methods=["POST"])
    async def save_configuration(request: Request) -> HTMLResponse | RedirectResponse:
        form = await request.form()
        csrf_token = _verified_csrf_token(request, form)
        if csrf_token is None:
            return _configuration_forbidden_response()
        try:
            raw = parse_configuration_form(form)
            expected_version = _form_integer(raw.expected_draft_version, "draft version")
            validation = configuration_service.validate(raw.candidate())
        except MalformedConfigurationForm as error:
            return _malformed_configuration_response(str(error))
        if isinstance(validation, ConfigurationInvalid):
            try:
                state = configuration_service.inspect()
            except psycopg.Error:
                return _configuration_unavailable_response()
            return HTMLResponse(
                _document(
                    configuration_page(
                        state,
                        raw,
                        csrf_token,
                        publication_key=secrets.token_urlsafe(32),
                        validation=validation,
                    ),
                    title="Search setup",
                ),
                status_code=422,
            )
        try:
            command = SaveDraftCommand(
                expected_version=expected_version,
                configuration=validation.configuration,
                actor=actor,
                timestamp=now(),
            )
        except ValidationError as error:
            return _malformed_configuration_response(str(error))
        try:
            result = configuration_service.save(command)
        except psycopg.Error:
            return _configuration_unavailable_response()
        if isinstance(result, DraftSaved):
            return RedirectResponse("/configuration?notice=draft-saved", status_code=303)
        try:
            state = configuration_service.inspect()
        except psycopg.Error:
            return _configuration_unavailable_response()
        rebound = replace(raw, expected_draft_version=str(result.current_draft.version))
        return HTMLResponse(
            _document(
                configuration_page(
                    state,
                    rebound,
                    csrf_token,
                    publication_key=secrets.token_urlsafe(32),
                    alert=(
                        "The saved draft changed while you were editing. Your values are still "
                        f"here. Save again to explicitly overwrite draft version "
                        f"{result.current_draft.version}."
                    ),
                ),
                title="Search setup conflict",
            ),
            status_code=409,
        )

    @app.route("/configuration/publish", methods=["POST"])
    async def publish_configuration(request: Request) -> HTMLResponse | RedirectResponse:
        form = await request.form()
        csrf_token = _verified_csrf_token(request, form)
        if csrf_token is None:
            return _configuration_forbidden_response()
        try:
            idempotency_key = _required_form_text(form, "idempotency_key")
            expected_version_text = _required_form_text(form, "expected_draft_version")
            expected_revision_id = _required_form_text(form, "expected_configuration_revision_id")
            command = PublishConfigurationCommand(
                idempotency_key=idempotency_key,
                expected_draft_version=_form_integer(expected_version_text, "draft version"),
                expected_configuration_revision_id=SearchConfigurationRevisionId(
                    expected_revision_id
                ),
                actor=actor,
                timestamp=now(),
            )
        except (MalformedConfigurationForm, ValidationError) as error:
            return _malformed_configuration_response(str(error))
        try:
            result = configuration_service.publish(command)
        except psycopg.Error:
            return HTMLResponse(
                _document(
                    publication_retry_page(
                        csrf_token,
                        publication_key=idempotency_key,
                        expected_draft_version=expected_version_text,
                        expected_revision_id=expected_revision_id,
                    ),
                    title="Publication result unknown",
                ),
                status_code=503,
            )
        if isinstance(result, ConfigurationPublished):
            notice = "publication-replayed" if result.replayed else "published"
            return RedirectResponse(f"/configuration?notice={notice}", status_code=303)
        message = (
            "The saved draft changed before publication. Nothing was published. "
            + f"Expected version {result.expected_draft_version} at revision "
            + f"{result.expected_configuration_revision_id}; observed version "
            + f"{result.observed_draft_version} at revision "
            + f"{result.observed_configuration_revision_id}. Reloaded state is shown below."
            if isinstance(result, PublishDraftChanged)
            else "This publication key belongs to a different request. Nothing was published."
        )
        return _configuration_conflict_page(configuration_service, csrf_token, message)

    @app.route("/configuration/activate", methods=["POST"])
    async def activate_configuration(request: Request) -> HTMLResponse | RedirectResponse:
        form = await request.form()
        csrf_token = _verified_csrf_token(request, form)
        if csrf_token is None:
            return _configuration_forbidden_response()
        try:
            command = ActivateConfigurationCommand(
                target_revision_id=SearchConfigurationRevisionId(
                    _required_form_text(form, "target_revision_id")
                ),
                expected_active_revision_id=SearchConfigurationRevisionId(
                    _required_form_text(form, "expected_active_revision_id")
                ),
                expected_generation=_form_integer(
                    _required_form_text(form, "expected_generation"), "active generation"
                ),
                actor=actor,
                timestamp=now(),
            )
        except (MalformedConfigurationForm, ValidationError) as error:
            return _malformed_configuration_response(str(error))
        try:
            result = (
                configuration_service.activate(command)
                if onboarding_progress_service is None
                else onboarding_progress_service.activate_preferences(command)
            )
        except psycopg.Error:
            return _configuration_unavailable_response()
        if isinstance(result, ConfigurationActivated):
            try:
                owner_state = owner_access_service.load_state()
            except (psycopg.Error, RuntimeError):
                return _configuration_unavailable_response()
            if owner_state.stage is OnboardingStage.BUDGET:
                return RedirectResponse("/setup/budget", status_code=303)
            return RedirectResponse("/configuration?notice=activated", status_code=303)
        if isinstance(result, ActiveConfigurationChanged):
            observed = result.active_configuration.active
            message = (
                "The active configuration changed before activation. Nothing was overwritten. "
                + f"The rejected form expected revision {command.expected_active_revision_id} "
                + f"at generation {command.expected_generation}; the operation observed revision "
                + f"{observed.revision.id} at generation {observed.generation}."
            )
        else:
            message = (
                f"Revision {command.target_revision_id} is not published. Nothing was activated."
            )
        return _configuration_conflict_page(configuration_service, csrf_token, message)

    @app.route("/review/{review_item_id}", methods=["POST"])
    async def submit_review(
        review_item_id: str, request: Request
    ) -> HTMLResponse | RedirectResponse:
        return await _submit_review(request, review_item_id, service, actor=actor, now=now)

    @app.route("/review/item/{review_item_id}", methods=["GET"])
    def review_item_page(review_item_id: str, request: Request) -> HTMLResponse:
        try:
            queue = service.review_queue()
        except psycopg.Error:
            return _unavailable_response()
        csrf_token = request.session.get("csrf_token")
        if not isinstance(csrf_token, str):
            return HTMLResponse(status_code=401)
        try:
            item_id = UUID(review_item_id)
        except ValueError:
            return _item_not_found_response()
        item = _find_item(queue, item_id)
        if item is None:
            return _item_not_found_response()
        if item.reviewed:
            return HTMLResponse(_document(_revision_page(item, csrf_token)))
        return HTMLResponse(_document(_item_page(queue.items, item, csrf_token)))

    @app.route("/logout", methods=["POST"])
    async def logout(request: Request) -> HTMLResponse | RedirectResponse:
        return await _submit_logout(request)

    app.add_middleware(SecurityHeadersMiddleware)
    return app


async def _submit_login(
    request: Request,
    owner_access: OwnerAccessService,
    login_failures: dict[str, deque[float]],
    login_attempt_lock: Lock,
) -> HTMLResponse | RedirectResponse:
    form = await request.form()
    password = _form_text(form, "password")
    next_url = _safe_next(_form_text(form, "next"))
    client_id = request.client.host if request.client else "unknown"
    async with login_attempt_lock:
        return await _check_login(
            request, owner_access, login_failures, password, next_url, client_id
        )


async def _check_login(
    request: Request,
    owner_access: OwnerAccessService,
    login_failures: dict[str, deque[float]],
    password: str,
    next_url: str,
    client_id: str,
) -> HTMLResponse | RedirectResponse:
    checked_at = time.monotonic()
    recent = login_failures.get(client_id, deque())
    while recent and checked_at - recent[0] >= LOGIN_WINDOW_SECONDS:
        recent.popleft()
    if len(recent) >= LOGIN_MAX_FAILURES:
        return HTMLResponse(
            _document(_login_content(next_url, "Too many attempts. Try again in a few minutes.")),
            status_code=429,
        )
    try:
        authenticated = await to_thread.run_sync(owner_access.authenticate, password)
    except psycopg.Error:
        return _state_response(
            "Sign in is unavailable",
            "The password could not be checked. Reload this page and try again.",
            status_code=503,
        )
    if authenticated:
        login_failures.pop(client_id, None)
        _authenticate_session(request)
        return RedirectResponse(next_url, status_code=303)
    if client_id not in login_failures and len(login_failures) >= LOGIN_MAX_CLIENTS:
        login_failures.pop(next(iter(login_failures)))
    recent.append(checked_at)
    login_failures[client_id] = recent
    return HTMLResponse(
        _document(_login_content(next_url, "The password is incorrect.")), status_code=401
    )


async def _submit_logout(request: Request) -> HTMLResponse | RedirectResponse:
    form = await request.form()
    if not _valid_csrf(request, _form_text(form, "csrf_token")):
        return _state_response(
            "Sign out failed",
            "Reload the page and try again.",
            status_code=403,
        )
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


async def _submit_review(
    request: Request,
    review_item_id: str,
    service: ReviewService,
    *,
    actor: str,
    now: DateTimeClock,
) -> HTMLResponse | RedirectResponse:
    form = await request.form()
    if not _valid_csrf(request, _form_text(form, "csrf_token")):
        return _conflict_response("This review form expired. Reload the page.")
    try:
        item_id = UUID(review_item_id)
    except ValueError:
        return _item_not_found_response()
    try:
        queue = service.review_queue()
    except psycopg.Error:
        return _unavailable_response()
    item = _find_item(queue, item_id)
    if item is None:
        return _item_not_found_response()
    if (
        _form_text(form, "evaluation_id") != item.evaluation_id
        or _form_text(form, "snapshot_id") != item.snapshot_id
    ):
        return _item_not_found_response()
    try:
        submission = ReviewSubmission.model_validate(
            {
                "review_item_id": review_item_id,
                "evaluation_id": _form_text(form, "evaluation_id"),
                "snapshot_id": _form_text(form, "snapshot_id"),
                "decision": _form_text(form, "decision"),
                "note": _form_text(form, "note"),
                "block_company": _form_text(form, "block_company") == "on",
                "actor": actor,
                "created_at": now(),
            }
        )
    except ValidationError:
        return _conflict_response("This review form is invalid. Reload the page and try again.")
    try:
        result = service.submit(submission)
    except psycopg.Error:
        return _unavailable_response()
    if isinstance(result, ReviewConflict):
        return _conflict_response(result.reason)
    if item.reviewed:
        return RedirectResponse(_item_url(item.id), status_code=303)
    position = next(i for i, candidate in enumerate(queue.items) if candidate.id == item_id)
    return RedirectResponse(_successor_url(queue.items, position), status_code=303)


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app: ASGIApp = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = cast(list[tuple[bytes, bytes]], message.setdefault("headers", []))
                present = {name.lower() for name, _value in headers}
                headers.extend(
                    (name, value) for name, value in _SECURITY_HEADERS if name not in present
                )
            await send(message)

        await self.app(scope, receive, send_with_headers)


def _require_owner(request: Request, owner_access: OwnerAccessService) -> Response | None:
    if ".." in request.url.path.split("/"):
        return Response(status_code=404)
    try:
        state = owner_access.load_state()
    except (psycopg.Error, RuntimeError):
        return _state_response(
            "Owner access is unavailable",
            "The installation state could not be loaded. Try again after the database recovers.",
            status_code=503,
        )
    if state.stage is OnboardingStage.LEGACY_OWNER_IMPORT:
        return _state_response(
            "Legacy owner import is required",
            "Restore JOB_FINDER_REVIEW_PASSWORD for one startup to import the existing owner securely.",
            status_code=503,
        )
    if state.stage is OnboardingStage.OWNER_ACCOUNT:
        if request.url.path == "/setup":
            return None
        return RedirectResponse("/setup", status_code=303)
    onboarding_path = {
        OnboardingStage.PROVIDERS: "/setup/providers",
        OnboardingStage.PREFERENCES: "/configuration",
        OnboardingStage.BUDGET: "/setup/budget",
        OnboardingStage.TEST_SEARCH: "/setup/test-search",
    }.get(state.stage)
    if onboarding_path is not None:
        if request.url.path == "/login":
            return None
        if request.session.get("authenticated") is not True:
            return RedirectResponse(
                f"/login?next={quote(onboarding_path, safe='')}", status_code=303
            )
        allowed_prefix = (
            "/configuration" if state.stage is OnboardingStage.PREFERENCES else onboarding_path
        )
        if request.url.path == "/logout" or request.url.path.startswith(allowed_prefix):
            return None
        return RedirectResponse(onboarding_path, status_code=303)
    if request.url.path == "/setup":
        destination = "/" if request.session.get("authenticated") is True else "/login"
        return RedirectResponse(destination, status_code=303)
    if request.url.path == "/login":
        return None
    if request.session.get("authenticated") is True:
        return None
    next_url = request.url.path
    if request.url.query:
        next_url = f"{next_url}?{request.url.query}"
    return RedirectResponse(f"/login?next={quote(next_url, safe='')}", status_code=303)


def _authenticate_session(request: Request) -> None:
    request.session.clear()
    request.session.update({"authenticated": True, "csrf_token": secrets.token_urlsafe(32)})


def _ensure_csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if isinstance(token, str):
        return token
    token = secrets.token_urlsafe(32)
    request.session["csrf_token"] = token
    return token


def _provider_setup_content(
    csrf_token: str,
    snapshot: ProviderSetupSnapshot,
    error: str | None = None,
) -> object:
    labels = {
        ProviderKind.JINA: ("Jina", "Searches job boards and reads job pages."),
        ProviderKind.OPENROUTER: (
            "OpenRouter",
            "Enriches and deduplicates jobs with structured model calls.",
        ),
        ProviderKind.TYPESAFE: (
            "Typesafe",
            "Evaluates relevance against your criteria. Validation makes one small paid request.",
        ),
    }
    cards = []
    for credential in snapshot.credentials:
        name, explanation = labels[credential.provider]
        status = "Validated" if credential.configured else "Required"
        cards.append(
            Section(
                Div(
                    Div(Small(credential.provider.value.upper(), cls="eyebrow"), H2(name)),
                    Span(status, cls="status-pill"),
                    cls="section-heading",
                ),
                P(explanation),
                Form(
                    Input(type="hidden", name="csrf_token", value=csrf_token),
                    Input(type="hidden", name="provider", value=credential.provider.value),
                    Input(
                        type="hidden",
                        name="expected_generation",
                        value=str(credential.generation),
                    ),
                    Label(
                        "Replace credential" if credential.configured else "Credential",
                        Input(
                            type="password",
                            name="credential",
                            required=True,
                            autocomplete="off",
                        ),
                    ),
                    Button("Validate and save", type="submit"),
                    action="/setup/providers",
                    method="post",
                ),
                cls="editor-section",
            )
        )
    return Main(
        Div(
            Small("JF / FIRST RUN", cls="eyebrow"),
            H1("Connect the services that do the work."),
            P(
                "Credentials are encrypted before storage and are never shown again. "
                + "Validation checks only the capabilities Job Finder needs.",
                cls="login-intro",
            ),
            cls="review-header",
        ),
        P(error, cls="error", role="alert") if error else None,
        *cards,
        Form(
            Input(type="hidden", name="csrf_token", value=csrf_token),
            Button("Continue to preferences", type="submit", disabled=not snapshot.ready),
            action="/setup/providers/continue",
            method="post",
        ),
        cls="configuration-shell",
    )


def _setup_content(csrf_token: str, error: str | None = None) -> object:
    return Main(
        Div(
            Small("JF / FIRST RUN", cls="eyebrow"),
            H1("Create the owner password."),
            P(
                "This password protects configuration, operations, and every job decision. "
                + "It is hashed before storage and cannot be recovered.",
                cls="login-intro",
            ),
            cls="login-editorial",
        ),
        Div(
            Small("Owner setup", cls="eyebrow"),
            H2("Secure this installation"),
            P(error, cls="error", role="alert") if error else None,
            Form(
                Input(type="hidden", name="csrf_token", value=csrf_token),
                Label(
                    "Bootstrap token",
                    Input(
                        type="password",
                        name="bootstrap_token",
                        required=True,
                        autocomplete="one-time-code",
                    ),
                ),
                Label(
                    "Password",
                    Input(
                        type="password",
                        name="password",
                        minlength=str(MINIMUM_PASSWORD_LENGTH),
                        maxlength=str(MAXIMUM_PASSWORD_LENGTH),
                        required=True,
                        autocomplete="new-password",
                    ),
                ),
                Label(
                    "Confirm password",
                    Input(
                        type="password",
                        name="password_confirmation",
                        minlength=str(MINIMUM_PASSWORD_LENGTH),
                        maxlength=str(MAXIMUM_PASSWORD_LENGTH),
                        required=True,
                        autocomplete="new-password",
                    ),
                ),
                Button("Create owner", type="submit", cls="primary-action"),
                action="/setup",
                method="post",
            ),
            cls="login-card",
        ),
        cls="login-shell",
    )


def _login_content(next_url: str, error: str | None = None) -> object:
    return Main(
        Div(
            Small("JF / PRIVATE REVIEW", cls="eyebrow"),
            H1("Review the work worth doing."),
            P(
                Span("Discover", cls="route-stage"),
                Span("/", cls="route-divider", aria_hidden="true"),
                Span("Filter", cls="route-stage"),
                Span("/", cls="route-divider", aria_hidden="true"),
                Span("Evaluate", cls="route-stage"),
                Span("/", cls="route-divider", aria_hidden="true"),
                Span("Review", cls="route-stage"),
                cls="route-line",
                aria_label="Discover, Filter, Evaluate, Review",
            ),
            P("A focused editorial pass over today's job matches.", cls="login-intro"),
            cls="login-editorial",
        ),
        Div(
            Small("Owner access", cls="eyebrow"),
            H2("Enter the workbench"),
            P(error, cls="error", role="alert") if error else None,
            Form(
                Input(type="hidden", name="next", value=next_url),
                Label(
                    "Password",
                    Input(
                        type="password",
                        name="password",
                        maxlength=str(MAXIMUM_PASSWORD_INPUT_LENGTH),
                        required=True,
                        autocomplete="current-password",
                    ),
                ),
                Button("Sign in", type="submit"),
                action="/login",
                method="post",
            ),
            cls="login-card",
        ),
        cls="login-shell",
    )


def _review_page(queue: ReviewQueue, csrf_token: str) -> object:
    return Main(
        authenticated_masthead(csrf_token, current="review"),
        Div(
            Small("Review queue", cls="eyebrow"),
            H1("Jobs waiting for review"),
            cls="review-header",
        ),
        *_day_sections(queue),
        cls="review-shell",
    )


def _operations_page(
    snapshot: OperationsSnapshot,
    controls: ControlPlaneSnapshot | None,
    csrf_token: str,
    *,
    run_key: str,
    notice: str | None,
) -> object:
    health_title, health_detail = {
        OperationsHealth.CAUGHT_UP: (
            "Caught up",
            "No queued work or current failures need attention.",
        ),
        OperationsHealth.WORKING: (
            "Work is in progress",
            "The pipeline has active or retryable work.",
        ),
        OperationsHealth.ACTION_REQUIRED: (
            "Action required",
            "A terminal failure or the latest pipeline run needs attention.",
        ),
        OperationsHealth.UNKNOWN: (
            "Status unknown",
            "No pipeline run or queued work has been recorded yet.",
        ),
    }[snapshot.health]
    return Main(
        authenticated_masthead(csrf_token, current="operations"),
        P(notice, cls="operations-notice", role="status") if notice else None,
        Div(
            Small("Owner operations", cls="eyebrow"),
            H1(health_title),
            P(health_detail, cls="operations-intro"),
            cls=f"operations-header health-{snapshot.health.value}",
        ),
        Div(
            _metric("Pending", snapshot.queues.pending, "pending", "pending"),
            _metric("Leased", snapshot.queues.leased, "leased", "leased"),
            _metric("Retrying", snapshot.queues.retrying, "retrying", "retrying"),
            _metric("Completed", snapshot.queues.completed, "completed", "completed"),
            _metric(
                "Terminal",
                snapshot.queues.terminal_error,
                "terminal error",
                "terminal errors",
            ),
            cls="operations-metrics",
            aria_label="Job work queue",
        ),
        Div(
            Div(
                Small("Recorded model spend", cls="eyebrow"),
                Strong(f"${snapshot.spend.known_usd:,.4f}", cls="spend-value"),
                P(
                    _count_phrase(
                        snapshot.spend.unknown_attempts,
                        "attempt has no recorded cost",
                        "attempts have no recorded cost",
                    ),
                    cls="operations-muted",
                ),
                cls="operations-panel spend-panel",
            ),
            Div(
                _schedule_controls(controls, csrf_token, run_key),
                cls="operations-panel",
            ),
            cls="operations-grid",
        ),
        _runs_panel(snapshot.recent_runs),
        _work_recovery_panel(
            snapshot.actionable_work,
            snapshot.actionable_work_total,
            csrf_token,
        ),
        _failures_panel(snapshot.failures),
        cls="review-shell operations-shell",
    )


def _schedule_controls(
    snapshot: ControlPlaneSnapshot | None, csrf_token: str, run_key: str
) -> tuple[object, ...]:
    if snapshot is None:
        return (
            Small("Dagster control plane", cls="eyebrow"),
            H2("Schedule status unavailable"),
            P(
                "Dagster could not be reached. Pipeline evidence above is still current, but "
                + "schedule controls are disabled.",
                cls="operations-muted",
            ),
            Ul(
                *(
                    _unavailable_schedule_row(definition.label, definition.cadence)
                    for definition in CONTROL_DEFINITIONS
                ),
                cls="schedule-list",
            ),
        )
    return (
        Small("Dagster control plane", cls="eyebrow"),
        H2("Schedules"),
        P(
            "Current state from Dagster. Changes are re-checked before applying.",
            cls="operations-muted",
        ),
        Ul(
            *(_schedule_row(schedule, csrf_token, run_key) for schedule in snapshot.schedules),
            cls="schedule-list",
        ),
    )


def _schedule_row(schedule: ScheduleView, csrf_token: str, run_key: str) -> object:
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
                f"Next: {_format_timestamp(schedule.next_tick)}"
                if schedule.next_tick is not None
                else "Next tick unavailable while stopped",
                cls="schedule-next",
            ),
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


def _unavailable_schedule_row(label: str, cadence: str) -> object:
    return Li(
        Div(
            Div(Strong(label), Span("Unavailable", cls="schedule-state unavailable")),
            P(cadence, cls="schedule-cadence"),
            Div(
                Button("Run now", type="button", disabled=True, cls="operation-button"),
                Button(
                    "Change schedule",
                    type="button",
                    disabled=True,
                    cls="operation-button secondary",
                ),
                cls="schedule-actions",
            ),
        )
    )


def _metric(label: str, value: int, singular: str, plural: str) -> object:
    return Div(
        Small(label),
        Strong(str(value)),
        Span(_count_phrase(value, singular, plural)),
    )


def _count_phrase(value: int, singular: str, plural: str) -> str:
    return f"{value} {singular if value == 1 else plural}"


def _runs_panel(runs: tuple[PipelineRunSummary, ...]) -> object:
    rows = (
        Ul(*(_run_row(run) for run in runs), cls="operations-list")
        if runs
        else P("No pipeline runs recorded.", cls="operations-empty")
    )
    return Div(
        Small("Recent activity", cls="eyebrow"),
        H2("Pipeline runs"),
        rows,
        cls="operations-section",
    )


def _run_row(run: PipelineRunSummary) -> object:
    timing = _format_timestamp(run.started_at)
    if run.completed_at is not None:
        elapsed = max(0, int((run.completed_at - run.started_at).total_seconds()))
        timing = f"{timing} · {_format_duration(elapsed)}"
    return Li(
        Div(Strong(run.kind.replace("_", " ").title()), Span(run.status, cls="run-status")),
        Small(timing),
    )


def _failures_panel(failures: tuple[FailureSample, ...]) -> object:
    rows = (
        Ul(*(_failure_row(failure) for failure in failures), cls="operations-list failure-list")
        if failures
        else P("No recent failures recorded.", cls="operations-empty")
    )
    return Div(
        Small("Bounded sample", cls="eyebrow"),
        H2("Recent failures"),
        rows,
        cls="operations-section",
    )


def _work_recovery_panel(work: tuple[ActionableWork, ...], total: int, csrf_token: str) -> object:
    rows = (
        Ul(*(_work_recovery_row(item, csrf_token) for item in work), cls="recovery-list")
        if work
        else P("No failed work needs recovery.", cls="operations-empty")
    )
    return Div(
        Small("Explicit bounded targets", cls="eyebrow"),
        H2("Work recovery"),
        P(
            "Retry delayed work now or give terminal work one fresh bounded attempt budget.",
            cls="operations-muted",
        ),
        P(
            f"Showing the newest {len(work)} of {total}; recover these to reveal older work.",
            cls="operations-muted",
        )
        if total > len(work)
        else None,
        rows,
        cls="operations-section",
    )


def _work_recovery_row(item: ActionableWork, csrf_token: str) -> object:
    action = RecoveryAction.RETRY_NOW if item.state == "failed" else RecoveryAction.RECOVER_TERMINAL
    timing = (
        f"Scheduled retry: {_format_timestamp(item.retry_at)}"
        if item.retry_at is not None
        else f"Terminal since: {_format_timestamp(item.failed_at)}"
    )
    return Li(
        Div(
            Strong("Retryable" if item.state == "failed" else "Terminal"),
            Span(f"Attempt {item.attempt_count}", cls="run-status"),
        ),
        Small(str(item.job_id), cls="recovery-id"),
        P(item.failure_summary),
        Small(timing),
        Form(
            Input(type="hidden", name="csrf_token", value=csrf_token),
            Input(type="hidden", name="job_id", value=str(item.job_id)),
            Input(type="hidden", name="action", value=action.value),
            Input(type="hidden", name="expected_state", value=item.state),
            Input(
                type="hidden",
                name="expected_attempt_count",
                value=str(item.attempt_count),
            ),
            Input(
                type="hidden",
                name="idempotency_key",
                value=secrets.token_urlsafe(32),
            ),
            Button(
                "Retry now" if action is RecoveryAction.RETRY_NOW else "Recover terminal work",
                type="submit",
                cls="operation-button",
            ),
            action="/operations/recovery",
            method="post",
        ),
    )


def _failure_row(failure: FailureSample) -> object:
    return Li(
        Div(Strong(failure.source.title()), Span(_format_timestamp(failure.occurred_at))),
        P(failure.summary),
    )


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _format_duration(total_seconds: int) -> str:
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def _day_sections(queue: ReviewQueue) -> list[object]:
    days = sorted(
        {item.review_day for item in queue.items}
        | {item.review_day for item in queue.reviewed_items},
        reverse=True,
    )
    if not days:
        return [
            Div(Small("Queue clear", cls="eyebrow"), H2("No jobs waiting for review."), cls="state")
        ]
    sections: list[object] = []
    for review_day in days:
        grouped = tuple(item for item in queue.items if item.review_day == review_day)
        reviewed = tuple(item for item in queue.reviewed_items if item.review_day == review_day)
        content: list[object] = [
            H2(_day_title(review_day)),
            P(
                f"{len(grouped)} waiting · {queue.reviewed_count(review_day)} reviewed",
                cls="day-summary",
            ),
        ]
        if grouped:
            content.append(Ul(*(_pending_row(item) for item in grouped), cls="job-list"))
        if reviewed:
            content.append(P(f"Reviewed ({len(reviewed)})", cls="day-summary"))
            content.append(Ul(*(_reviewed_row(item) for item in reviewed), cls="job-list"))
        sections.append(Div(*content, cls="day-section"))
    return sections


def _reviewed_row(item: ReviewItem) -> object:
    return Li(
        Div(
            Span(item.decision or "", cls="chip decision-chip"),
            Div(
                Strong(item.job.title, cls="job-title"),
                Span(_job_subline(item.job), cls="job-subline"),
                Span(item.note[:120], cls="job-subline") if item.note else None,
                cls="row-copy",
            ),
            A("Change", href=_item_url(item.id), cls="change-link"),
            cls="reviewed-row",
        ),
        cls="job-list-item reviewed-item",
    )


def _pending_row(item: ReviewItem) -> object:
    lane_chip = "chip lane-new" if item.lane == "qualified" else "chip lane-second-look"
    lane_name = "New result" if item.lane == "qualified" else "Second look"
    return Li(
        A(
            Span(lane_name, cls=lane_chip),
            Strong(item.job.title, cls="job-title"),
            Span(_job_subline(item.job), cls="job-subline"),
            href=_item_url(item.id),
            cls="job-row",
        ),
        cls="job-list-item",
    )


def _job_subline(job: ReviewJob) -> str:
    location = job.location or "Location not specified"
    return f"{job.company} · {location}"


def _item_page(items: tuple[ReviewItem, ...], item: ReviewItem, csrf_token: str) -> object:
    position = next(i for i, candidate in enumerate(items) if candidate.id == item.id)
    previous_item = items[position - 1] if position > 0 else None
    next_item = items[position + 1] if position + 1 < len(items) else None
    return Main(
        Div(
            A("← All jobs", href="/review", cls="back-link"),
            Span(f"{position + 1} of {len(items)} waiting", cls="position-marker"),
            Div(
                _item_arrow("← Prev", previous_item),
                _item_arrow("Next →", next_item),
                cls="item-nav",
            ),
            cls="item-topbar",
        ),
        _job_card(item, csrf_token),
        cls="review-shell",
    )


def _item_arrow(glyph: str, target: ReviewItem | None) -> object:
    if target is None:
        return Span(glyph, cls="item-nav-link item-nav-off", aria_hidden="true")
    return A(glyph, href=_item_url(target.id), cls="item-nav-link")


def _revision_page(item: ReviewItem, csrf_token: str) -> object:
    return Main(
        Div(
            A("← All jobs", href="/review", cls="back-link"),
            Span("Revision", cls="position-marker"),
            cls="item-topbar",
        ),
        _job_card(item, csrf_token),
        cls="review-shell",
    )


def _job_card(item: ReviewItem, csrf_token: str) -> object:
    second_look = item.lane == "rejected_audit"
    return Div(
        Div(
            Div(
                Span("Second look" if second_look else "New result", cls="status-kicker"),
                A("Open original listing", href=item.job.url, target="_blank", rel="noreferrer"),
                cls="card-topline",
            ),
            H2(item.job.title),
            P(
                Span(item.job.company, cls="company"),
                Span(" / ", aria_hidden="true"),
                Span(item.job.location or "Location not specified"),
                cls="job-meta",
            ),
            _metadata_strip(item),
            Div(
                Small("Why it's here", cls="why-label"),
                P(item.evaluation_reason, cls="evaluation-reason"),
            ),
            _compensation_card(item.job.compensation),
            Div(
                Small("Job evidence", cls="why-label"),
                Pre(item.job.description, cls="job-description", aria_label="Job description"),
                cls="description-block",
            ),
            cls="evidence-panel",
        ),
        Div(
            Small("Decision desk", cls="eyebrow"),
            H2("Make the call" if not item.reviewed else "Review the call"),
            _decision_form(item, csrf_token),
            _reevaluation_form(item, csrf_token),
            cls="decision-panel",
        ),
        cls="workbench audit-card" if second_look else "workbench",
    )


def _metadata_strip(item: ReviewItem) -> object:
    posted = item.job.date_posted.strftime("%b %-d, %Y") if item.job.date_posted else "Date unknown"
    profile = item.matched_profile.replace("-", " ") if item.matched_profile else "No profile match"
    return Div(
        Div(Small("Posted"), Span(posted)),
        Div(Small("Source"), Span(item.job.source.replace("_", " ").title())),
        Div(Small("Profile"), Span(profile)),
        cls="metadata-strip",
        aria_label="Job metadata",
    )


_CURRENCY_SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£"}
_PERIOD_LABELS = {
    "year": "per year",
    "month": "per month",
    "week": "per week",
    "day": "per day",
    "hour": "per hour",
}
_SOURCE_LABELS = {"ats": "from the ATS", "llm": "extracted from the posting"}


def _compensation_card(compensation: Compensation | None) -> object:
    if compensation is None:
        return None
    parts: list[str] = []
    currency = compensation.currency or ""
    symbol = _CURRENCY_SYMBOLS.get(currency, currency)
    minimum, maximum = compensation.minimum, compensation.maximum
    if minimum is not None and maximum is not None and minimum != maximum:
        parts.append(f"{symbol}{minimum:,.0f} – {symbol}{maximum:,.0f}")
    elif minimum is not None and maximum is not None:
        parts.append(f"{symbol}{minimum:,.0f}")
    elif minimum is not None:
        parts.append(f"from {symbol}{minimum:,.0f}")
    elif maximum is not None:
        parts.append(f"up to {symbol}{maximum:,.0f}")
    if compensation.period is not None:
        parts.append(_PERIOD_LABELS.get(compensation.period, compensation.period))
    if compensation.source is not None:
        parts.append(_SOURCE_LABELS.get(compensation.source, compensation.source))
    if not parts:
        return None
    return Div(
        Small("Compensation", cls="why-label"),
        P(" · ".join(parts), cls="compensation-value"),
        cls="compensation-card",
    )


def _decision_form(item: ReviewItem, csrf_token: str) -> object:
    return Form(
        Input(type="hidden", name="csrf_token", value=csrf_token),
        Input(type="hidden", name="evaluation_id", value=item.evaluation_id),
        Input(type="hidden", name="snapshot_id", value=item.snapshot_id),
        Label(
            "Notes, context, anything worth remembering (optional)",
            Textarea(
                item.note or "",
                name="note",
                rows="3",
                placeholder="Why this decision? Salary signals, location, language, anything.",
            ),
            cls="note-field",
        ),
        Label(
            Input(type="checkbox", name="block_company", checked=item.block_company),
            " Block this company from future results",
            cls="block-company",
        ),
        Fieldset(
            Legend("Revise the recorded decision" if item.reviewed else "Decision"),
            _decision_button("Pursue", "pursue", item.decision),
            _decision_button("Unsure", "unsure", item.decision),
            _decision_button("Reject", "reject", item.decision),
            cls="decision-row",
        ),
        action=f"/review/{item.id}",
        method="post",
    )


def _reevaluation_form(item: ReviewItem, csrf_token: str) -> object:
    return Form(
        Input(type="hidden", name="csrf_token", value=csrf_token),
        Input(
            type="hidden",
            name="expected_decision_id",
            value=item.evaluation_id,
        ),
        Input(
            type="hidden",
            name="expected_snapshot_id",
            value=item.snapshot_id,
        ),
        Input(
            type="hidden",
            name="idempotency_key",
            value=secrets.token_urlsafe(32),
        ),
        Button(
            "Re-evaluate this job",
            type="submit",
            cls="operation-button secondary",
        ),
        P(
            "Runs this saved snapshot against the current release without changing prior history.",
            cls="operation-help",
        ),
        action="/operations/reevaluation",
        method="post",
        cls="reevaluation-form",
    )


def _decision_button(label: str, value: str, current: str | None) -> object:
    return Button(
        label,
        name="decision",
        value=value,
        cls=f"decision {value}",
        aria_pressed="true" if current == value else "false",
    )


def _unavailable_response() -> HTMLResponse:
    return _state_response(
        "Review is unavailable",
        "The database could not load this review. Your previous decisions are unchanged.",
        action=A("Retry", href="/review", cls="retry"),
        status_code=503,
    )


def _operations_forbidden_response() -> HTMLResponse:
    return _state_response(
        "This operations form expired",
        "Reload operations and try again.",
        action=A("Reload operations", href="/", cls="retry"),
        status_code=403,
    )


def _malformed_operations_response(detail: str) -> HTMLResponse:
    return _state_response(
        "Malformed operations form",
        detail,
        action=A("Reload operations", href="/", cls="retry"),
        status_code=400,
    )


def _operations_conflict_response(detail: str) -> HTMLResponse:
    return _state_response(
        "Operations state changed",
        detail,
        action=A("Reload operations", href="/", cls="retry"),
        status_code=409,
    )


def _operations_unavailable_response(detail: str) -> HTMLResponse:
    return _state_response(
        "Dagster control is unavailable",
        detail,
        action=A("Reload operations", href="/", cls="retry"),
        status_code=503,
    )


def _work_recovery_unavailable_response() -> HTMLResponse:
    return _state_response(
        "Work recovery is unavailable",
        "The database could not apply this command. Reload operations and try again.",
        action=A("Reload operations", href="/", cls="retry"),
        status_code=503,
    )


def _work_recovery_not_found_response() -> HTMLResponse:
    return _state_response(
        "Work item was not found",
        "The requested work item no longer exists.",
        action=A("Reload operations", href="/", cls="retry"),
        status_code=404,
    )


def _reevaluation_not_found_response() -> HTMLResponse:
    return _state_response(
        "Source decision was not found",
        "The requested decision no longer exists.",
        action=A("Back to review", href="/review", cls="retry"),
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
    return _state_response(
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
    return _state_response(
        "Run launch result is uncertain",
        f"{detail}. Retry with the same request so Dagster can be reconciled without relaunching.",
        action=retry_form,
        status_code=503,
    )


def _configuration_unavailable_response() -> HTMLResponse:
    return _state_response(
        "Search setup is unavailable",
        "The database could not complete this request. Saved configuration was not overwritten.",
        action=A("Retry", href="/configuration", cls="retry"),
        status_code=503,
    )


def _configuration_forbidden_response() -> HTMLResponse:
    return _state_response(
        "This configuration form expired",
        "Reload search setup and try again.",
        action=A("Reload search setup", href="/configuration", cls="retry"),
        status_code=403,
    )


def _malformed_configuration_response(detail: str) -> HTMLResponse:
    return _state_response(
        "Malformed configuration form",
        detail,
        action=A("Reload search setup", href="/configuration", cls="retry"),
        status_code=400,
    )


def _configuration_conflict_page(
    service: ConfigurationEditorService,
    csrf_token: str,
    alert: str,
) -> HTMLResponse:
    try:
        state = service.inspect()
    except psycopg.Error:
        return _configuration_unavailable_response()
    return HTMLResponse(
        _document(
            configuration_page(
                state,
                RawConfigurationForm.from_draft(state.draft),
                csrf_token,
                publication_key=secrets.token_urlsafe(32),
                alert=alert,
            ),
            title="Search setup conflict",
        ),
        status_code=409,
    )


def _conflict_response(reason: str) -> HTMLResponse:
    return _state_response(
        "This review changed",
        reason,
        action=A("Back to the review", href="/review", cls="retry"),
        status_code=409,
    )


def _item_not_found_response() -> HTMLResponse:
    return _state_response(
        "Review item not found",
        "This job is not part of the review.",
        action=A("Back to the review", href="/review", cls="retry"),
        status_code=404,
    )


def _state_response(
    title: str,
    detail: str,
    *,
    action: object | None = None,
    status_code: int,
) -> HTMLResponse:
    content = Main(
        Small("Review queue", cls="eyebrow"),
        Div(H1(title), P(detail), action, cls="state"),
        cls="review-shell state-shell",
    )
    return HTMLResponse(_document(content), status_code=status_code)


def _document(content: object, *, title: str = "Daily job review") -> str:
    return str(
        to_xml(
            Html(
                Head(
                    Meta(charset="utf-8"),
                    Meta(name="viewport", content="width=device-width, initial-scale=1"),
                    Meta(name="color-scheme", content="light dark"),
                    Title(title),
                    Style(_CSS + CONFIGURATION_CSS),
                ),
                Body(content),
                lang="en",
            )
        )
    )


def _form_text(form: FormData, key: str) -> str:
    value = form.get(key)
    return value if isinstance(value, str) else ""


def _valid_csrf(request: Request, supplied: str) -> bool:
    expected = request.session.get("csrf_token")
    return isinstance(expected, str) and hmac.compare_digest(supplied, expected)


def _csrf_token(request: Request) -> str | None:
    value = request.session.get("csrf_token")
    return value if isinstance(value, str) else None


def _verified_csrf_token(request: Request, form: FormData) -> str | None:
    supplied = _form_text(form, "csrf_token")
    if not _valid_csrf(request, supplied):
        return None
    return _csrf_token(request)


def _verified_control_csrf_token(request: Request, form: FormData) -> str | None:
    values = form.getlist("csrf_token")
    if len(values) != 1 or not isinstance(values[0], str):
        return None
    if not _valid_csrf(request, values[0]):
        return None
    return _csrf_token(request)


def _required_control_form_text(form: FormData, key: str) -> str:
    values = form.getlist(key)
    if len(values) != 1 or not isinstance(values[0], str) or not values[0]:
        raise ValueError(f"Expected one {key} value")
    return values[0]


def _actionable_work_state(value: str) -> Literal["failed", "terminal_error"]:
    if value == "failed" or value == "terminal_error":
        return value
    raise ValueError("Expected failed or terminal_error state")


def _required_form_text(form: FormData, key: str) -> str:
    values = form.getlist(key)
    if len(values) != 1 or not isinstance(values[0], str):
        raise MalformedConfigurationForm(f"Expected one {key} value")
    return values[0]


def _form_integer(value: str, label: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise MalformedConfigurationForm(f"Invalid {label}") from error
    if parsed < 0:
        raise MalformedConfigurationForm(f"Invalid {label}")
    return parsed


def _affected_rows(action: str, raw: RawConfigurationForm) -> frozenset[tuple[str, int]]:
    if action == "criterion.add":
        return frozenset({("personal_criteria", len(raw.personal_criteria) - 1)})
    if action == "profile.add":
        return frozenset({("target_profiles", len(raw.target_profiles) - 1)})
    return frozenset()


def _safe_next(value: str) -> str:
    if value.startswith("/") and not value.startswith(("//", "/\\")):
        return value
    return "/"


def _successor_url(items: tuple[ReviewItem, ...], position: int) -> str:
    following = items[position + 1 :]
    if following:
        return _item_url(following[0].id)
    return "/review"


def _find_item(queue: ReviewQueue, item_id: UUID) -> ReviewItem | None:
    return next(
        (
            candidate
            for candidate in (*queue.items, *queue.reviewed_items)
            if candidate.id == item_id
        ),
        None,
    )


def _item_url(item_id: UUID) -> str:
    return f"/review/item/{item_id}"


def _day_title(review_day: date) -> str:
    return review_day.strftime("%A, %B %-d")


_CSS = """
:root {
  --ink: #151515;
  --paper: #f3f0e7;
  --panel: #fffdf5;
  --panel-muted: #ebe7dc;
  --panel-subtle: #f1eee5;
  --surface-raised: #f7f4eb;
  --muted: #5d5b54;
  --line: #151515;
  --grid-line: rgb(21 21 21 / 0.06);
  --grid-line-strong: rgb(21 21 21 / 0.08);
  --inverse-bg: #151515;
  --inverse-text: #fffdf5;
  --shadow: #151515;
  --focus: #315cff;
  --acid: #dfff00;
  --caution: #ffd86b;
  --accent-ink: #151515;
  --reviewed: #dedbd1;
  font-family: Arial, Helvetica, ui-sans-serif, system-ui, sans-serif;
  color: var(--ink);
  background: var(--paper);
  color-scheme: light dark;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  min-width: 320px;
  min-height: 100vh;
  background-color: var(--paper);
  background-image: linear-gradient(var(--grid-line) 1px, transparent 1px), linear-gradient(90deg, var(--grid-line) 1px, transparent 1px);
  background-size: 24px 24px;
}
a { color: inherit; text-underline-offset: 0.2em; }
button, select, textarea, input { font: inherit; }
h1, h2 { margin: 0; font-family: Georgia, 'Times New Roman', serif; letter-spacing: -0.04em; }
h1 { max-width: 14ch; font-size: clamp(2.4rem, 7vw, 5.2rem); line-height: 0.9; }
h2 { font-size: clamp(1.55rem, 3vw, 2.35rem); line-height: 1; }
.eyebrow, .status-kicker, .masthead-label, .why-label, .metadata-strip small {
  text-transform: uppercase;
  letter-spacing: 0.12em;
  font-size: 0.72rem;
  font-weight: 900;
}
.eyebrow { display: block; margin-bottom: 0.7rem; }
.review-shell { width: min(100% - 2rem, 1180px); margin: 0 auto; padding: 1.25rem 0 5rem; }
.masthead { display: flex; align-items: center; justify-content: space-between; min-height: 56px; border: 2px solid var(--line); background: var(--panel); }
.masthead > div { display: flex; align-items: center; }
.wordmark { display: grid; place-items: center; align-self: stretch; min-width: 58px; padding: 0.6rem; background: var(--inverse-bg); color: var(--acid); font-size: 1.35rem; }
.masthead-label { padding: 0 0.85rem; }
.logout { min-width: 88px; min-height: 52px; border: 0; border-left: 2px solid var(--line); background: var(--acid); color: var(--accent-ink); cursor: pointer; font-weight: 900; }
.review-header { padding: clamp(2rem, 6vw, 5rem) 0 1.5rem; }
.review-header .eyebrow { width: fit-content; padding: 0.25rem 0.4rem; background: var(--acid); color: var(--accent-ink); }
.day-section { margin-top: 2.4rem; }
.day-summary { margin: 0.55rem 0 0; color: var(--muted); font-weight: 700; }
.job-list { list-style: none; padding: 0; margin: 0.8rem 0 0; border: 2px solid var(--line); border-bottom: 0; }
.job-list-item { border-bottom: 2px solid var(--line); }
.job-row, .reviewed-row { min-height: 92px; background: var(--panel); color: inherit; }
.job-row { display: block; padding: 0.9rem 1rem; text-decoration: none; }
.job-row:hover, .job-row:focus-visible { background: var(--acid); color: var(--accent-ink); }
.reviewed-row { display: grid; grid-template-columns: auto 1fr auto; align-items: center; gap: 1rem; padding-left: 1rem; background: var(--reviewed); }
.row-copy { padding: 0.8rem 0; }
.change-link { display: inline-flex; align-items: center; justify-content: center; align-self: stretch; min-width: 84px; min-height: 44px; border-left: 2px solid var(--line); font-weight: 900; }
.chip { display: inline-block; width: fit-content; padding: 0.22rem 0.45rem; border: 2px solid var(--line); font-size: 0.7rem; font-weight: 900; letter-spacing: 0.08em; text-transform: uppercase; }
.lane-new { background: var(--acid); color: var(--accent-ink); }
.lane-second-look { background: var(--caution); color: var(--accent-ink); }
.decision-chip { background: var(--inverse-bg); color: var(--inverse-text); }
.job-title { display: block; margin-top: 0.35rem; font-size: 1.08rem; }
.job-subline { display: block; margin-top: 0.18rem; color: var(--muted); font-size: 0.92rem; }
.item-topbar { display: grid; grid-template-columns: 1fr auto 1fr; align-items: center; gap: 1rem; margin-top: 0.75rem; padding: 0.65rem 0; border-bottom: 2px solid var(--line); }
.back-link { min-height: 44px; display: inline-flex; align-items: center; font-weight: 900; text-decoration: none; }
.position-marker { font-weight: 900; text-transform: uppercase; letter-spacing: 0.06em; }
.item-nav { display: flex; justify-content: end; gap: 0.5rem; }
.item-nav-link { min-width: 72px; min-height: 44px; display: inline-flex; align-items: center; justify-content: center; padding: 0 0.6rem; border: 2px solid var(--line); background: var(--panel); font-weight: 900; text-decoration: none; }
.item-nav-off { opacity: 0.45; border-style: dashed; }
.workbench { display: grid; grid-template-columns: minmax(0, 1.55fr) minmax(320px, 0.85fr); margin-top: 1.25rem; border: 2px solid var(--line); background: var(--panel); box-shadow: 8px 8px 0 var(--shadow); }
.evidence-panel, .decision-panel { min-width: 0; padding: clamp(1rem, 3vw, 2rem); }
.decision-panel { border-left: 2px solid var(--line); background: var(--panel-muted); }
.audit-card { box-shadow: 8px 8px 0 var(--caution); }
.card-topline { display: flex; justify-content: space-between; align-items: center; gap: 1rem; margin-bottom: 1.4rem; }
.status-kicker { padding: 0.25rem 0.4rem; background: var(--acid); color: var(--accent-ink); }
.audit-card .status-kicker { background: var(--caution); }
.job-meta { margin: 0.75rem 0 1.1rem; color: var(--muted); font-size: 1.05rem; }
.company { color: var(--ink); font-weight: 900; }
.metadata-strip { display: grid; grid-template-columns: repeat(3, 1fr); margin-bottom: 2rem; border: 2px solid var(--line); }
.metadata-strip > div { min-width: 0; padding: 0.6rem; border-right: 2px solid var(--line); }
.metadata-strip > div:last-child { border-right: 0; }
.metadata-strip small, .metadata-strip span { display: block; }
.metadata-strip span { margin-top: 0.3rem; overflow-wrap: anywhere; font-size: 0.88rem; }
.why-label { display: block; margin-bottom: 0.4rem; color: var(--muted); }
.evaluation-reason { margin: 1.25rem 0; padding: 0.85rem 1rem; border-left: 6px solid var(--acid); background: var(--panel-subtle); font-weight: 750; }
.compensation-card { margin: 1.25rem 0; padding: 0.8rem 1rem; border: 2px solid var(--line); background: var(--acid); color: var(--accent-ink); }
.compensation-card .why-label { color: var(--accent-ink); }
.compensation-value { margin: 0.25rem 0 0; font-size: 1.1rem; font-weight: 900; }
.description-block { margin-top: 1.5rem; }
.job-description { max-height: 52vh; overflow: auto; white-space: pre-wrap; margin: 0; padding: 1rem; border: 2px solid var(--line); background: var(--surface-raised); color: var(--ink); font: 1rem/1.65 Arial, Helvetica, ui-sans-serif, system-ui, sans-serif; }
.decision-panel h2 { margin-bottom: 1.5rem; }
.note-field { display: grid; gap: 0.45rem; margin: 0 0 1rem; font-weight: 800; }
.note-field textarea { width: 100%; min-height: 112px; padding: 0.75rem; border: 2px solid var(--line); border-radius: 0; background: var(--panel); color: var(--ink); }
.block-company { display: flex; align-items: center; min-height: 56px; margin: 1rem 0; padding: 0.6rem; border: 2px solid var(--line); background: var(--caution); color: var(--accent-ink); font-weight: 900; }
.block-company input { width: 22px; height: 22px; margin-right: 0.65rem; accent-color: var(--accent-ink); }
.decision-row { display: grid; grid-template-columns: 1fr; gap: 0.65rem; padding: 0; border: 0; }
.decision-row legend { margin-bottom: 0.75rem; font-weight: 900; }
.decision { min-height: 52px; border: 2px solid var(--line); background: var(--panel); color: var(--ink); cursor: pointer; font-weight: 900; }
.decision:hover, .decision:focus-visible { background: var(--acid); color: var(--accent-ink); box-shadow: 4px 4px 0 var(--shadow); }
.decision[aria-pressed="true"] { background: var(--inverse-bg); color: var(--acid); box-shadow: 4px 4px 0 var(--acid); }
.decision.reject[aria-pressed="true"] { color: var(--caution); }
.decision:focus-visible, a:focus-visible, textarea:focus-visible, input:focus-visible { outline: 3px solid var(--focus); outline-offset: 3px; }
.state-shell { min-height: 100vh; display: grid; align-content: center; }
.state { margin-top: 1.25rem; padding: clamp(1.5rem, 5vw, 3rem); border: 2px solid var(--line); background-color: var(--panel); background-image: linear-gradient(var(--grid-line-strong) 1px, transparent 1px), linear-gradient(90deg, var(--grid-line-strong) 1px, transparent 1px); background-size: 20px 20px; box-shadow: 8px 8px 0 var(--shadow); }
.state h1, .state h2 { max-width: 14ch; }
.state p { max-width: 48ch; color: var(--muted); font-size: 1.08rem; line-height: 1.6; }
.retry { display: inline-flex; min-height: 48px; align-items: center; margin-top: 0.5rem; padding: 0 1.1rem; border: 2px solid var(--line); background: var(--acid); color: var(--accent-ink); font-weight: 900; }
.login-shell { display: grid; grid-template-columns: minmax(0, 1.25fr) minmax(320px, 0.75fr); min-height: 100vh; }
.login-editorial { display: grid; align-content: center; padding: clamp(2rem, 7vw, 7rem); background: var(--inverse-bg); color: var(--inverse-text); }
.login-editorial h1 { color: var(--acid); }
.route-line { display: flex; flex-wrap: wrap; gap: 0.55rem; margin: 1.8rem 0 0; font-weight: 900; text-transform: uppercase; letter-spacing: 0.06em; }
.route-line .route-divider { color: var(--acid); }
.login-intro { max-width: 40ch; line-height: 1.5; }
.login-card { align-self: center; width: min(100% - 2rem, 430px); margin: 2rem auto; padding: 2rem; border: 2px solid var(--line); background: var(--panel); box-shadow: 8px 8px 0 var(--acid); }
.login-card h2 { margin-bottom: 1.5rem; }
.login-card label { display: grid; gap: 0.5rem; font-weight: 800; }
.login-card input { width: 100%; min-height: 48px; padding: 0.75rem; border: 2px solid var(--line); border-radius: 0; background: var(--surface-raised); color: var(--ink); }
.login-card button { width: 100%; min-height: 48px; margin-top: 1rem; border: 2px solid var(--line); background: var(--acid); color: var(--accent-ink); cursor: pointer; font-weight: 900; }
.error { padding: 0.75rem; border: 2px solid var(--line); background: var(--caution); color: var(--accent-ink); font-weight: 800; }
.operations-header { margin-top: 1.25rem; padding: clamp(1.5rem, 5vw, 3rem); border: 2px solid var(--line); background: var(--panel); box-shadow: 8px 8px 0 var(--shadow); }
.operations-header.health-caught_up { box-shadow: 8px 8px 0 var(--acid); }
.operations-header.health-working, .operations-header.health-unknown { box-shadow: 8px 8px 0 var(--caution); }
.operations-header.health-action_required { background: var(--caution); color: var(--accent-ink); }
.operations-notice { margin: 1.25rem 0 0; padding: 0.8rem 1rem; border: 2px solid var(--line); background: var(--acid); color: var(--accent-ink); font-weight: 900; }
.operations-intro { max-width: 54ch; margin: 1rem 0 0; font-size: 1.08rem; line-height: 1.55; }
.operations-metrics { display: grid; grid-template-columns: repeat(5, 1fr); margin-top: 2rem; border: 2px solid var(--line); background: var(--panel); }
.operations-metrics > div { min-width: 0; padding: 0.8rem; border-right: 2px solid var(--line); }
.operations-metrics > div:last-child { border-right: 0; }
.operations-metrics small, .operations-metrics strong, .operations-metrics span { display: block; }
.operations-metrics small { text-transform: uppercase; letter-spacing: 0.08em; font-weight: 900; }
.operations-metrics strong { margin-top: 0.25rem; font: 700 2rem Georgia, 'Times New Roman', serif; }
.operations-metrics span { margin-top: 0.15rem; color: var(--muted); font-size: 0.8rem; }
.operations-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 1.25rem; margin-top: 1.25rem; }
.operations-panel, .operations-section { padding: 1.25rem; border: 2px solid var(--line); background: var(--panel); }
.spend-panel { align-self: start; }
.spend-value { display: block; font: 700 clamp(2rem, 6vw, 4rem) Georgia, 'Times New Roman', serif; letter-spacing: -0.04em; }
.operations-muted, .operations-empty { color: var(--muted); line-height: 1.5; }
.operations-section { margin-top: 1.25rem; }
.operations-list { list-style: none; margin: 1rem 0 0; padding: 0; border: 2px solid var(--line); border-bottom: 0; }
.operations-list li { padding: 0.8rem; border-bottom: 2px solid var(--line); background: var(--surface-raised); }
.operations-list li > div { display: flex; justify-content: space-between; gap: 1rem; }
.operations-list li > small, .failure-list p { display: block; margin-top: 0.4rem; color: var(--muted); }
.run-status { text-transform: uppercase; letter-spacing: 0.08em; font-size: 0.72rem; font-weight: 900; }
.failure-list p { margin-bottom: 0; overflow-wrap: anywhere; }
.schedule-list { list-style: none; margin: 1rem 0 0; padding: 0; border: 2px solid var(--line); border-bottom: 0; }
.schedule-list > li { padding: 0.8rem; border-bottom: 2px solid var(--line); background: var(--surface-raised); }
.schedule-list > li > div > div:first-child { display: flex; align-items: center; justify-content: space-between; gap: 0.75rem; }
.schedule-state { padding: 0.18rem 0.35rem; border: 2px solid var(--line); font-size: 0.68rem; font-weight: 900; letter-spacing: 0.08em; text-transform: uppercase; }
.schedule-state.running { background: var(--acid); color: var(--accent-ink); }
.schedule-state.stopped, .schedule-state.unavailable { background: var(--caution); color: var(--accent-ink); }
.schedule-cadence, .schedule-next { margin: 0.45rem 0 0; color: var(--muted); font-size: 0.86rem; }
.schedule-actions { display: grid !important; grid-template-columns: 1fr 1fr; gap: 0.5rem; margin-top: 0.75rem; }
.operation-button { width: 100%; min-height: 42px; padding: 0 0.6rem; border: 2px solid var(--line); background: var(--acid); color: var(--accent-ink); cursor: pointer; font-weight: 900; }
.operation-button.secondary { background: var(--panel); color: var(--ink); }
.operation-button:disabled { cursor: not-allowed; opacity: 0.5; }
.recovery-list { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0.8rem; list-style: none; margin: 1rem 0 0; padding: 0; }
.recovery-list > li { min-width: 0; padding: 1rem; border: 2px solid var(--line); background: var(--surface-raised); }
.recovery-list > li > div:first-child { display: flex; align-items: center; justify-content: space-between; gap: 0.75rem; }
.recovery-list p { margin: 0.65rem 0; color: var(--muted); overflow-wrap: anywhere; }
.recovery-list form { margin-top: 0.8rem; }
.recovery-id { display: block; margin-top: 0.35rem; overflow-wrap: anywhere; color: var(--muted); }
@media (prefers-color-scheme: dark) {
  :root {
    --ink: #f3f0e7;
    --paper: #11120f;
    --panel: #1b1c18;
    --panel-muted: #24251f;
    --panel-subtle: #272821;
    --surface-raised: #171814;
    --muted: #b9b7ae;
    --line: #e7e2d5;
    --grid-line: rgb(243 240 231 / 0.07);
    --grid-line-strong: rgb(243 240 231 / 0.1);
    --inverse-bg: #050604;
    --inverse-text: #f3f0e7;
    --shadow: #050604;
    --focus: #8ca9ff;
    --reviewed: #292a25;
  }
}
@media (max-width: 760px) {
  .review-shell { width: min(100% - 1rem, 1180px); padding-top: 0.5rem; }
  .login-shell { grid-template-columns: 1fr; }
  .login-editorial { min-height: 48vh; padding: 2rem 1rem; }
  .workbench { grid-template-columns: 1fr; box-shadow: 5px 5px 0 var(--shadow); }
  .decision-panel { border-top: 2px solid var(--line); border-left: 0; }
  .item-topbar { grid-template-columns: 1fr auto; }
  .position-marker { grid-column: 1 / -1; grid-row: 1; }
  .back-link, .item-nav { grid-row: 2; }
  .metadata-strip { grid-template-columns: 1fr; }
  .metadata-strip > div { border-right: 0; border-bottom: 2px solid var(--line); }
  .metadata-strip > div:last-child { border-bottom: 0; }
  .reviewed-row { grid-template-columns: 1fr auto; padding-left: 0.75rem; }
  .reviewed-row .decision-chip { grid-column: 1; margin-top: 0.7rem; }
  .row-copy { grid-column: 1; }
  .change-link { grid-column: 2; grid-row: 1 / 3; }
  .card-topline { align-items: flex-start; flex-direction: column; }
  .job-description { max-height: none; overflow: visible; }
  .operations-metrics { grid-template-columns: 1fr; }
  .operations-metrics > div { border-right: 0; border-bottom: 2px solid var(--line); }
  .operations-metrics > div:last-child { border-bottom: 0; }
  .operations-grid { grid-template-columns: 1fr; }
  .recovery-list { grid-template-columns: 1fr; }
  .schedule-actions { grid-template-columns: 1fr; }
}
@media (max-width: 360px) {
  .masthead-label { display: none; }
  .evidence-panel, .decision-panel, .login-card { padding: 1rem; }
  .item-nav-link { min-width: 64px; padding: 0 0.35rem; }
}
@media (prefers-reduced-motion: reduce) {
  .job-row:hover, .job-row:focus-visible, .decision:hover, .decision:focus-visible { transform: none; }
}
"""
