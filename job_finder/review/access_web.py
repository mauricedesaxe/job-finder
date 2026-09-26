# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUntypedFunctionDecorator=false, reportUnusedFunction=false, reportMissingTypeStubs=false
from __future__ import annotations

import hmac
import logging
import secrets
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

import psycopg
from anyio import Lock, to_thread
from fasthtml.common import (
    H1,
    H2,
    A,
    Button,
    Div,
    FastHTML,
    Form,
    Input,
    Label,
    Li,
    Main,
    P,
    Request,
    Section,
    Small,
    Span,
    Strong,
    Ul,
)
from pydantic import SecretStr
from starlette.responses import HTMLResponse, RedirectResponse, Response

from job_finder.config import ReviewAppSettings
from job_finder.execution_budget import (
    BudgetChanged,
    BudgetSetupService,
    BudgetSetupState,
    ExecutionBlocked,
)
from job_finder.evaluation.relevance_releases import RelevanceReleaseError
from job_finder.onboarding_test_search import OnboardingTestSearchAccepted
from job_finder.provider_credentials import (
    ProviderCredentialChanged,
    ProviderCredentialRejected,
    ProviderKind,
    ProviderSetupService,
    ProviderSetupSnapshot,
    ProviderStageBlocked,
)
from job_finder.review.onboarding import (
    OnboardingSearchProgress,
    OnboardingSearchService,
)
from job_finder.review.owner_access import (
    MAXIMUM_PASSWORD_INPUT_LENGTH,
    MAXIMUM_PASSWORD_LENGTH,
    MINIMUM_PASSWORD_LENGTH,
    OnboardingStage,
    OwnerAccessService,
    OwnerBootstrapConflict,
)
from job_finder.web.security import (
    authenticate_session,
    ensure_csrf_token,
    form_text,
    valid_csrf,
)
from job_finder.web.shell import (
    document,
    state_response,
)

DateTimeClock = Callable[[], datetime]
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_CLIENTS = 1024
logger = logging.getLogger(__name__)


def _budget_inspection_failure(error: psycopg.Error | RuntimeError) -> HTMLResponse:
    if isinstance(error, RelevanceReleaseError):
        logger.exception("Budget inspection failed because the relevance release is invalid")
        message = (
            "The active relevance release could not be validated. "
            "Activate a release compatible with this deployment, then reload."
        )
    elif isinstance(error, psycopg.Error):
        logger.error("Budget database inspection failed: %s", type(error).__name__)
        message = "Budget database state could not be read. Check the server logs and retry."
    else:
        logger.error("Budget inspection failed: %s", type(error).__name__)
        message = "Budget state could not be loaded. Check the server logs."
    return state_response("Budget setup is unavailable", message, status_code=503)


def register_access_routes(
    app: FastHTML,
    *,
    settings: ReviewAppSettings,
    owner_access_service: OwnerAccessService,
    provider_setup_service: ProviderSetupService | None,
    budget_setup_service: BudgetSetupService | None,
    test_search_service: OnboardingSearchService | None,
    actor: str,
    now: DateTimeClock,
) -> None:
    login_failures: dict[str, deque[float]] = {}
    login_attempt_lock = Lock()

    @app.route("/setup", methods=["GET"], name="create_review_app_setup_form")
    def setup_form(request: Request) -> HTMLResponse:
        csrf_token = request.session.get("csrf_token")
        if not isinstance(csrf_token, str):
            csrf_token = secrets.token_urlsafe(32)
            request.session["csrf_token"] = csrf_token
        return HTMLResponse(document(_setup_content(csrf_token)))

    @app.route("/setup", methods=["POST"], name="create_review_app_setup_submit")
    async def setup_submit(request: Request) -> HTMLResponse | RedirectResponse:
        form = await request.form()
        if not valid_csrf(request, form_text(form, "csrf_token")):
            return HTMLResponse(
                document(_setup_content("", "This setup form expired. Reload and try again.")),
                status_code=403,
            )
        csrf_token = request.session.get("csrf_token")
        assert isinstance(csrf_token, str)
        password = form_text(form, "password")
        configured_token = settings.bootstrap_token
        supplied_token = form_text(form, "bootstrap_token")
        if configured_token is None or not hmac.compare_digest(
            supplied_token, configured_token.get_secret_value()
        ):
            return HTMLResponse(
                document(
                    _setup_content(
                        csrf_token,
                        "The bootstrap token is incorrect.",
                    )
                ),
                status_code=401,
            )
        if password != form_text(form, "password_confirmation"):
            return HTMLResponse(
                document(_setup_content(csrf_token, "Passwords differ.")),
                status_code=400,
            )
        try:
            result = await to_thread.run_sync(owner_access_service.bootstrap, password)
        except ValueError as error:
            return HTMLResponse(
                document(_setup_content(csrf_token, str(error))),
                status_code=400,
            )
        except psycopg.Error:
            return state_response(
                "Owner setup is unavailable",
                "The password was not confirmed. Reload this page and try again.",
                status_code=503,
            )
        if isinstance(result, OwnerBootstrapConflict):
            return state_response(
                "Owner setup is already complete",
                "Sign in with the owner password that was created first.",
                action=A("Go to sign in", href="/login", cls="retry"),
                status_code=409,
            )
        authenticate_session(request)
        return RedirectResponse("/setup/providers", status_code=303)

    @app.route("/setup/providers", methods=["GET"], name="create_review_app_provider_setup_form")
    def provider_setup_form(request: Request) -> HTMLResponse:
        if provider_setup_service is None:
            return state_response(
                "Provider setup is unavailable",
                "Provider credential storage is not configured for this deployment.",
                status_code=503,
            )
        try:
            snapshot = provider_setup_service.inspect()
        except (psycopg.Error, RuntimeError):
            return state_response(
                "Provider setup is unavailable",
                "Provider state could not be loaded. Try again after the database recovers.",
                status_code=503,
            )
        return HTMLResponse(document(_provider_setup_content(ensure_csrf_token(request), snapshot)))

    @app.route("/setup/providers", methods=["POST"], name="create_review_app_provider_setup_submit")
    async def provider_setup_submit(request: Request) -> HTMLResponse | RedirectResponse:
        form = await request.form()
        if not valid_csrf(request, form_text(form, "csrf_token")):
            return state_response(
                "Provider setup failed",
                "This setup form expired. Reload and try again.",
                status_code=403,
            )
        if provider_setup_service is None:
            return state_response(
                "Provider setup is unavailable",
                "Provider credential storage is not configured for this deployment.",
                status_code=503,
            )
        try:
            provider = ProviderKind(form_text(form, "provider"))
            expected_generation = int(form_text(form, "expected_generation"))
        except (ValueError, TypeError):
            return state_response(
                "Provider setup failed",
                "The provider request was invalid. Reload and try again.",
                status_code=400,
            )
        credential = form_text(form, "credential")
        if not credential:
            return state_response(
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
            return state_response(
                "Provider setup is unavailable",
                "The credential was not stored. Reload and try again.",
                status_code=503,
            )
        try:
            snapshot = provider_setup_service.inspect()
        except (psycopg.Error, RuntimeError):
            return state_response(
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
                document(_provider_setup_content(ensure_csrf_token(request), snapshot, message)),
                status_code=422,
            )
        if isinstance(result, ProviderCredentialChanged):
            return HTMLResponse(
                document(
                    _provider_setup_content(
                        ensure_csrf_token(request),
                        snapshot,
                        "This credential changed in another session. Review the current state.",
                    )
                ),
                status_code=409,
            )
        return RedirectResponse("/setup/providers", status_code=303)

    @app.route(
        "/setup/providers/continue",
        methods=["POST"],
        name="create_review_app_provider_setup_continue",
    )
    async def provider_setup_continue(request: Request) -> HTMLResponse | RedirectResponse:
        form = await request.form()
        if not valid_csrf(request, form_text(form, "csrf_token")):
            return state_response(
                "Provider setup failed",
                "This setup form expired. Reload and try again.",
                status_code=403,
            )
        if provider_setup_service is None:
            return state_response(
                "Provider setup is unavailable",
                "Provider credential storage is not configured for this deployment.",
                status_code=503,
            )
        try:
            result = provider_setup_service.advance()
            snapshot = provider_setup_service.inspect()
        except (psycopg.Error, RuntimeError):
            return state_response(
                "Provider setup is unavailable",
                "Provider state could not be confirmed. Reload and try again.",
                status_code=503,
            )
        if isinstance(result, ProviderStageBlocked):
            if result.state.stage is OnboardingStage.PREFERENCES:
                return RedirectResponse("/configuration", status_code=303)
            return HTMLResponse(
                document(
                    _provider_setup_content(
                        ensure_csrf_token(request),
                        snapshot,
                        "Validate every required provider before continuing.",
                    )
                ),
                status_code=409,
            )
        return RedirectResponse("/configuration", status_code=303)

    @app.route("/login", methods=["GET"], name="create_review_app_login_form")
    def login_form(request: Request) -> HTMLResponse:
        return HTMLResponse(
            document(_login_content(_safe_next(request.query_params.get("next", "/"))))
        )

    @app.route("/setup/budget", methods=["GET"], name="create_review_app_budget_setup_form")
    def budget_setup_form(request: Request) -> HTMLResponse:
        if budget_setup_service is None:
            return state_response(
                "Budget setup is unavailable",
                "Execution budget storage is not configured for this deployment.",
                status_code=503,
            )
        try:
            initial = budget_setup_service.inspect(25)
        except (psycopg.Error, RuntimeError) as error:
            return _budget_inspection_failure(error)
        return HTMLResponse(document(_budget_setup_content(ensure_csrf_token(request), initial)))

    @app.route("/setup/budget", methods=["POST"], name="create_review_app_budget_setup_submit")
    async def budget_setup_submit(request: Request) -> HTMLResponse | RedirectResponse:
        form = await request.form()
        if not valid_csrf(request, form_text(form, "csrf_token")):
            return state_response(
                "Budget setup failed",
                "This setup form expired. Reload and try again.",
                status_code=403,
            )
        if budget_setup_service is None:
            return state_response(
                "Budget setup is unavailable",
                "Execution budget storage is not configured for this deployment.",
                status_code=503,
            )
        try:
            expected_version = int(form_text(form, "expected_version"))
            monthly_limit = Decimal(form_text(form, "monthly_limit_usd"))
            run_limit = Decimal(form_text(form, "run_allowance_usd"))
            max_jobs = int(form_text(form, "max_jobs_per_run"))
            result = budget_setup_service.save(
                expected_version,
                monthly_limit,
                run_limit,
                max_jobs,
                actor,
                now(),
            )
        except (InvalidOperation, ValueError):
            return state_response(
                "Budget setup failed",
                "Enter positive amounts; the run allowance cannot exceed the monthly budget.",
                status_code=400,
            )
        except (psycopg.Error, RuntimeError):
            return state_response(
                "Budget setup is unavailable",
                "The budget was not confirmed. Reload and try again.",
                status_code=503,
            )
        if isinstance(result, BudgetChanged):
            try:
                current = budget_setup_service.inspect(max_jobs)
            except (psycopg.Error, RuntimeError):
                return state_response(
                    "Budget setup is unavailable",
                    "Current budget state could not be reloaded.",
                    status_code=503,
                )
            return HTMLResponse(
                document(
                    _budget_setup_content(
                        ensure_csrf_token(request),
                        current,
                        "The budget changed in another session. Review the current limits.",
                    )
                ),
                status_code=409,
            )
        return RedirectResponse("/setup/test-search", status_code=303)

    @app.route("/setup/test-search", methods=["GET"], name="create_review_app_test_search_setup")
    def test_search_setup(request: Request) -> HTMLResponse:
        if test_search_service is None:
            return state_response(
                "Test search is unavailable", "Reload after the service recovers.", status_code=503
            )
        try:
            progress = test_search_service.inspect()
        except (psycopg.Error, RuntimeError):
            return state_response(
                "Test search is unavailable", "Reload after the database recovers.", status_code=503
            )
        refresh = (
            (5, "/setup/test-search")
            if progress.request is not None and progress.request.state in {"pending", "leased"}
            else None
        )
        return HTMLResponse(
            document(
                _test_search_content(progress, ensure_csrf_token(request)),
                title="Test search",
                refresh=refresh,
            )
        )

    @app.route("/setup/test-search", methods=["POST"], name="create_review_app_test_search_submit")
    async def test_search_submit(request: Request) -> HTMLResponse | RedirectResponse:
        if test_search_service is None:
            return state_response(
                "Test search is unavailable", "Reload after the service recovers.", status_code=503
            )
        form = await request.form()
        if not valid_csrf(request, form_text(form, "csrf_token")):
            return state_response(
                "Test search was not started", "Reload the page and try again.", status_code=403
            )
        try:
            result = test_search_service.launch(actor, now())
        except (psycopg.Error, RuntimeError):
            return state_response(
                "Test search is unavailable",
                "No new search was started. Reload and try again.",
                status_code=503,
            )
        if isinstance(result, ExecutionBlocked):
            return state_response(
                "Test search could not start",
                _TEST_SEARCH_BLOCKED_REASONS[result.reason],
                status_code=409,
            )
        assert isinstance(result, OnboardingTestSearchAccepted)
        return RedirectResponse("/setup/test-search", status_code=303)

    @app.route("/login", methods=["POST"], name="create_review_app_login_submit")
    async def login_submit(request: Request) -> HTMLResponse | RedirectResponse:
        return await _submit_login(
            request, owner_access_service, login_failures, login_attempt_lock
        )

    @app.route("/logout", methods=["POST"], name="create_review_app_logout")
    async def logout(request: Request) -> HTMLResponse | RedirectResponse:
        return await _submit_logout(request)


async def _submit_login(
    request: Request,
    owner_access: OwnerAccessService,
    login_failures: dict[str, deque[float]],
    login_attempt_lock: Lock,
) -> HTMLResponse | RedirectResponse:
    form = await request.form()
    password = form_text(form, "password")
    next_url = _safe_next(form_text(form, "next"))
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
            document(_login_content(next_url, "Too many attempts. Try again in a few minutes.")),
            status_code=429,
        )
    try:
        authenticated = await to_thread.run_sync(owner_access.authenticate, password)
    except psycopg.Error:
        return state_response(
            "Sign in is unavailable",
            "The password could not be checked. Reload this page and try again.",
            status_code=503,
        )
    if authenticated:
        login_failures.pop(client_id, None)
        authenticate_session(request)
        return RedirectResponse(next_url, status_code=303)
    if client_id not in login_failures and len(login_failures) >= LOGIN_MAX_CLIENTS:
        login_failures.pop(next(iter(login_failures)))
    recent.append(checked_at)
    login_failures[client_id] = recent
    return HTMLResponse(
        document(_login_content(next_url, "The password is incorrect.")), status_code=401
    )


async def _submit_logout(request: Request) -> HTMLResponse | RedirectResponse:
    form = await request.form()
    if not valid_csrf(request, form_text(form, "csrf_token")):
        return state_response(
            "Sign out failed",
            "Reload the page and try again.",
            status_code=403,
        )
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


def require_owner(
    request: Request,
    owner_access: OwnerAccessService,
    budget_setup: BudgetSetupService | None,
) -> Response | None:
    if ".." in request.url.path.split("/"):
        return Response(status_code=404)
    try:
        state = owner_access.load_state()
    except (psycopg.Error, RuntimeError):
        return state_response(
            "Owner access is unavailable",
            "The installation state could not be loaded. Try again after the database recovers.",
            status_code=503,
        )
    if state.stage is OnboardingStage.LEGACY_OWNER_IMPORT:
        return state_response(
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
    budget_response = _require_completed_budget(request, budget_setup)
    if budget_response is not None:
        return budget_response
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


def _require_completed_budget(
    request: Request,
    budget_setup: BudgetSetupService | None,
) -> Response | None:
    if (
        budget_setup is None
        or request.session.get("authenticated") is not True
        or request.url.path in ("/logout", "/setup/budget")
    ):
        return None
    try:
        policy = budget_setup.inspect(25).policy
    except (psycopg.Error, RuntimeError) as error:
        return _budget_inspection_failure(error)
    if policy is None:
        return RedirectResponse("/setup/budget", status_code=303)
    return None


def _provider_setup_content(
    csrf_token: str,
    snapshot: ProviderSetupSnapshot,
    error: str | None = None,
) -> object:
    labels = {
        ProviderKind.JINA: (
            "Jina",
            "Searches job boards and reads job pages. Validation sends one search and one reader request.",
        ),
        ProviderKind.OPENROUTER: (
            "OpenRouter",
            "Enriches and deduplicates jobs. Validation makes one small paid model request.",
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


def _budget_setup_content(
    csrf_token: str,
    state: BudgetSetupState,
    error: str | None = None,
) -> object:
    policy = state.policy
    estimate = state.estimate
    return Main(
        Div(
            Small("JF / FIRST RUN", cls="eyebrow"),
            H1("Put a hard admission limit on scheduled work."),
            P(
                "Each run that starts provider work consumes its full USD allowance. "
                + "Idle queue checks consume nothing. Set provider account limits to cap the bill.",
                cls="login-intro",
            ),
            cls="review-header",
        ),
        P(error, cls="error", role="alert") if error else None,
        Section(
            Small("CURRENT EXECUTION BOUND", cls="eyebrow"),
            H2(f"{estimate.search_queries} searches per discovery run"),
            P(
                f"At {estimate.jobs_per_run} jobs, the current prompts allow up to "
                + f"{estimate.maximum_provider_attempts} provider attempts including retries."
            ),
            cls="editor-section",
        ),
        Form(
            Input(
                type="hidden",
                name="csrf_token",
                value=csrf_token,
            ),
            Input(
                type="hidden",
                name="expected_version",
                value=str(0 if policy is None else policy.version),
            ),
            Label(
                "Monthly admission budget (USD)",
                Input(
                    type="number",
                    name="monthly_limit_usd",
                    value="20.00" if policy is None else str(policy.monthly_limit_usd),
                    min="0.01",
                    step="0.01",
                    required=True,
                ),
            ),
            Label(
                "USD allowance consumed per admitted run",
                Input(
                    type="number",
                    name="run_allowance_usd",
                    value="2.00" if policy is None else str(policy.run_allowance_usd),
                    min="0.01",
                    step="0.01",
                    required=True,
                ),
            ),
            Label(
                "Maximum jobs processed per run",
                Input(
                    type="number",
                    name="max_jobs_per_run",
                    value=str(estimate.jobs_per_run),
                    min="1",
                    max="1000",
                    required=True,
                ),
            ),
            Button("Save budget and prepare test", type="submit", cls="primary-action"),
            action="/setup/budget",
            method="post",
            cls="editor-section",
        ),
        cls="configuration-shell",
    )


_TEST_SEARCH_BLOCKED_REASONS = {
    "onboarding_incomplete": "Finish the earlier setup steps, then try again.",
    "budget_not_configured": "Set a monthly budget before starting the test search.",
    "configuration_exceeds_policy": "The current search exceeds its budget limits. Review the budget and search settings.",
    "monthly_budget_exhausted": "The monthly budget is exhausted. Increase it before retrying.",
    "already_consumed": "This test search has already used its allowance. Reload to see its result.",
}


def _test_search_content(progress: OnboardingSearchProgress, csrf_token: str) -> object:
    request = progress.request
    if request is None:
        title = "Ready for a bounded test search"
        explanation = "Run a small search with the preferences and budget you just set."
    elif request.state == "pending":
        title = "Test search queued"
        explanation = "Your search is waiting to start. This page updates automatically."
    elif request.state == "leased":
        title = "Test search running"
        explanation = "Jobs are being found and checked. You can leave this page and return."
    elif request.state == "failed":
        title = "Test search stopped"
        explanation = "The search did not finish. Review the reason below, then retry when ready."
    else:
        title = "Test search complete"
        explanation = "Setup is complete. You can review the jobs found so far."
    start_form = (
        Form(
            Input(type="hidden", name="csrf_token", value=csrf_token),
            Button(
                "Retry test search" if request is not None else "Start test search", type="submit"
            ),
            action="/setup/test-search",
            method="post",
        )
        if request is None or request.state == "failed"
        else None
    )
    return Main(
        Div(
            Small("JF / FIRST RUN", cls="eyebrow"),
            H1(title),
            P(explanation, cls="login-intro"),
            cls="review-header",
        ),
        Section(
            H2("Progress"),
            P(
                f"{progress.queries_completed} of {request.limits.max_queries} searches completed · "
                + f"{progress.urls_checked} of {request.limits.max_urls} URLs checked · "
                + f"{progress.jobs_found} of {request.limits.max_jobs} jobs added"
            )
            if request is not None
            else P("The search will stay within the budget and job limits you set."),
            P(request.error_reason[:500], role="alert")
            if request is not None and request.state == "failed" and request.error_reason
            else None,
            start_form,
            A("Open review queue →", href="/review")
            if request is not None and request.state == "completed"
            else None,
            cls="editor-section",
        ),
        Section(
            H2("Jobs found"),
            Ul(*(Li(Strong(job.title), P(job.company), Small(job.url)) for job in progress.jobs))
            if progress.jobs
            else P("No jobs to show yet."),
            cls="editor-section",
        )
        if request is not None
        else None,
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


def _safe_next(value: str) -> str:
    if value.startswith("/") and not value.startswith(("//", "/\\")):
        return value
    return "/"
