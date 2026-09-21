# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUntypedFunctionDecorator=false, reportUnusedFunction=false, reportMissingTypeStubs=false
from __future__ import annotations

import hmac
import secrets
import time
from collections import deque
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import cast
from urllib.parse import quote
from uuid import UUID

import psycopg
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
from pydantic import ValidationError
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
from job_finder.review.models import (
    Compensation,
    ReviewConflict,
    ReviewItem,
    ReviewJob,
    ReviewQueue,
    ReviewSubmission,
)
from job_finder.review.postgres import ReviewService
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


def create_review_app(
    service: ReviewService,
    configuration_service: ConfigurationEditorService,
    settings: ReviewAppSettings,
    *,
    readiness: ReadinessProbe = lambda: None,
    actor: str = "owner",
    now: DateTimeClock = lambda: datetime.now(UTC),
) -> FastHTML:
    app = FastHTML(
        before=Beforeware(
            _require_owner,
            skip=[r"/healthz", r"/readyz", r"/login", r"/favicon.ico"],
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

    @app.route("/login", methods=["GET"])
    def login_form(request: Request) -> HTMLResponse:
        return HTMLResponse(
            _document(_login_content(_safe_next(request.query_params.get("next", "/"))))
        )

    @app.route("/login", methods=["POST"])
    async def login_submit(request: Request) -> HTMLResponse | RedirectResponse:
        return await _submit_login(request, settings, login_failures)

    @app.route("/", methods=["GET"])
    def home() -> RedirectResponse:
        return RedirectResponse("/review", status_code=303)

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
            result = configuration_service.activate(command)
        except psycopg.Error:
            return _configuration_unavailable_response()
        if isinstance(result, ConfigurationActivated):
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
    settings: ReviewAppSettings,
    login_failures: dict[str, deque[float]],
) -> HTMLResponse | RedirectResponse:
    form = await request.form()
    password = _form_text(form, "password")
    next_url = _safe_next(_form_text(form, "next"))
    client_id = request.headers.get("x-real-ip")
    if client_id is None:
        client_id = request.client.host if request.client else "unknown"
    checked_at = time.monotonic()
    recent = login_failures.get(client_id, deque())
    while recent and checked_at - recent[0] >= LOGIN_WINDOW_SECONDS:
        recent.popleft()
    if len(recent) >= LOGIN_MAX_FAILURES:
        return HTMLResponse(
            _document(_login_content(next_url, "Too many attempts. Try again in a few minutes.")),
            status_code=429,
        )
    if hmac.compare_digest(password, settings.app_password):
        login_failures.pop(client_id, None)
        request.session.clear()
        request.session.update({"authenticated": True, "csrf_token": secrets.token_urlsafe(32)})
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


def _require_owner(request: Request) -> Response | None:
    if ".." in request.url.path.split("/"):
        return Response(status_code=404)
    if request.session.get("authenticated") is True:
        return None
    next_url = request.url.path
    if request.url.query:
        next_url = f"{next_url}?{request.url.query}"
    return RedirectResponse(f"/login?next={quote(next_url, safe='')}", status_code=303)


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
                maxlength="2000",
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
