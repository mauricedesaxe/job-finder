# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUntypedFunctionDecorator=false, reportUnusedFunction=false, reportMissingTypeStubs=false
from __future__ import annotations

import hmac
import secrets
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Literal, cast
from urllib.parse import quote

import psycopg
from fasthtml.common import (
    A,
    Beforeware,
    Body,
    Button,
    Details,
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
    Main,
    Meta,
    Option,
    P,
    Pre,
    Select,
    Small,
    Span,
    Style,
    Summary,
    Textarea,
    Time,
    Title,
    FastHTML,
    Request,
    to_xml,
)
from pydantic import ValidationError
from starlette.responses import PlainTextResponse
from starlette.datastructures import FormData
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from job_finder.config import ReviewAppSettings
from job_finder.review.models import (
    DailyReview,
    PrimaryReason,
    ReviewConflict,
    ReviewItem,
    ReviewSubmission,
    TargetProfile,
)
from job_finder.review.postgres import ReviewService

DateClock = Callable[[], date]
DateTimeClock = Callable[[], datetime]
ReadinessProbe = Callable[[], None]
PageKind = Literal["empty", "complete"]
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
TARGET_PROFILES: tuple[tuple[TargetProfile, str], ...] = (
    ("early-stage-product-engineer", "Early-stage product"),
    ("applied-ai-product-engineer", "Applied AI product"),
    ("neither", "Neither profile"),
)
PRIMARY_REASONS: tuple[tuple[PrimaryReason, str], ...] = (
    ("technology-fit", "Technology fit"),
    ("role-scope", "Role scope"),
    ("company-quality", "Company quality"),
    ("work-environment", "Work environment"),
    ("location", "Location"),
    ("compensation", "Compensation"),
    ("crypto-company", "Crypto company"),
    ("insufficient-information", "Insufficient information"),
    ("other", "Other"),
)


def create_review_app(
    service: ReviewService,
    settings: ReviewAppSettings,
    *,
    readiness: ReadinessProbe = lambda: None,
    actor: str = "owner",
    today: DateClock = lambda: datetime.now(UTC).date(),
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
    def review_page(request: Request, day: str = "") -> HTMLResponse:
        review_day = _parse_day(day, today())
        if review_day is None:
            return _state_response(
                "Choose a valid review date",
                "Use a date in YYYY-MM-DD format.",
                status_code=400,
            )
        try:
            daily_review = service.open_day(review_day)
        except psycopg.Error:
            return _unavailable_response(review_day)
        csrf_token = request.session.get("csrf_token")
        if not isinstance(csrf_token, str):
            return HTMLResponse(status_code=401)
        return HTMLResponse(_document(_review_content(daily_review, csrf_token)))

    @app.route("/review/{review_item_id}", methods=["POST"])
    async def submit_review(
        review_item_id: str, request: Request
    ) -> HTMLResponse | RedirectResponse:
        return await _submit_review(
            request,
            review_item_id,
            service,
            actor=actor,
            today=today,
            now=now,
        )

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
    today: DateClock,
    now: DateTimeClock,
) -> HTMLResponse | RedirectResponse:
    form = await request.form()
    review_day = _parse_day(_form_text(form, "review_day"), today())
    if review_day is None:
        return _conflict_response(today(), "This review form has an invalid date.")
    if not _valid_csrf(request, _form_text(form, "csrf_token")):
        return _conflict_response(review_day, "This review form expired. Reload the page.")
    try:
        submission = ReviewSubmission.model_validate(
            {
                "review_item_id": review_item_id,
                "evaluation_id": _form_text(form, "evaluation_id"),
                "snapshot_id": _form_text(form, "snapshot_id"),
                "decision": _form_text(form, "decision"),
                "target_profile": _form_text(form, "target_profile"),
                "primary_reason": _form_text(form, "primary_reason"),
                "note": _form_text(form, "note"),
                "block_company": _form_text(form, "block_company") == "on",
                "actor": actor,
                "created_at": now(),
            }
        )
    except ValidationError:
        return _conflict_response(review_day, "This review form is invalid or out of date.")
    try:
        result = service.submit(submission)
    except psycopg.Error:
        return _unavailable_response(review_day)
    if isinstance(result, ReviewConflict):
        return _conflict_response(review_day, result.reason)
    return RedirectResponse(f"/review?day={review_day.isoformat()}", status_code=303)


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
            Small("Private access", cls="eyebrow"),
            H1("Daily job review"),
            P("Enter the review password to continue."),
            P(error, cls="error") if error else None,
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


def _review_content(review: DailyReview, csrf_token: str) -> object:
    current = review.current
    if current is None:
        kind: PageKind = "empty" if review.total == 0 else "complete"
        return _finished_state(review, kind, csrf_token)
    return Main(
        _review_header(review, csrf_token),
        _job_card(review, current, csrf_token),
        cls="review-shell",
    )


def _review_header(review: DailyReview, csrf_token: str) -> object:
    current = review.current
    lane_label = "Qualified" if current is not None and current.lane == "qualified" else "Audit"
    return (
        Div(
            Div(
                Small("Daily review", cls="eyebrow"),
                H1("Choose the next move"),
            ),
            Div(
                Time(review.day.strftime("%A, %B %-d"), datetime=review.day.isoformat()),
                P(f"{review.completed} of {review.total} reviewed", cls="progress-copy"),
                _logout_form(csrf_token),
                cls="review-meta",
            ),
            cls="review-header",
        ),
        Div(
            Span(lane_label, cls="lane-label"),
            Span(
                f"Qualified {review.qualified.completed}/{review.qualified.total}",
                cls="lane-progress",
            ),
            Span(
                f"Audit {review.rejected_audit.completed}/{review.rejected_audit.total}",
                cls="lane-progress audit-progress",
            ),
            cls="progress-line",
            role="status",
            aria_live="polite",
        ),
    )


def _job_card(review: DailyReview, item: ReviewItem, csrf_token: str) -> object:
    audit = item.lane == "rejected_audit"
    default_profile = _default_profile(item)
    return Div(
        Div(
            Span("Rejected audit" if audit else "Qualified match", cls="status-kicker"),
            A("Open original listing", href=item.job.url, target="_blank", rel="noreferrer"),
            cls="card-topline",
        ),
        H2(item.job.title),
        P(
            Span(item.job.company, cls="company"),
            Span(" · "),
            Span(item.job.location or "Location not specified"),
            cls="job-meta",
        ),
        P(item.evaluation_reason, cls="evaluation-reason"),
        Pre(item.job.description, cls="job-description", aria_label="Job description"),
        Form(
            Input(type="hidden", name="review_day", value=review.day.isoformat()),
            Input(type="hidden", name="csrf_token", value=csrf_token),
            Input(type="hidden", name="evaluation_id", value=item.evaluation_id),
            Input(type="hidden", name="snapshot_id", value=item.snapshot_id),
            Details(
                Summary("Review context and note"),
                Div(
                    Label(
                        "Target profile",
                        Select(
                            *(
                                Option(label, value=value, selected=value == default_profile)
                                for value, label in TARGET_PROFILES
                            ),
                            name="target_profile",
                            required=True,
                        ),
                    ),
                    Label(
                        "Primary reason",
                        Select(
                            *(
                                Option(label, value=value, selected=value == "other")
                                for value, label in PRIMARY_REASONS
                            ),
                            name="primary_reason",
                            required=True,
                        ),
                    ),
                    Label(
                        "Optional note",
                        Textarea(
                            name="note",
                            maxlength="2000",
                            rows="3",
                            placeholder="What made this decision clear?",
                        ),
                        cls="note-field",
                    ),
                    Label(
                        Input(type="checkbox", name="block_company"),
                        " Block this company from future results",
                        cls="block-company",
                    ),
                    cls="context-fields",
                ),
            ),
            Fieldset(
                Legend("Decision"),
                Button("Pursue", name="decision", value="pursue", cls="decision pursue"),
                Button("Unsure", name="decision", value="unsure", cls="decision unsure"),
                Button("Reject", name="decision", value="reject", cls="decision reject"),
                cls="decision-row",
            ),
            action=f"/review/{item.id}",
            method="post",
        ),
        cls="job-card audit-card" if audit else "job-card",
    )


def _finished_state(review: DailyReview, kind: PageKind, csrf_token: str) -> object:
    if kind == "empty":
        title = "Nothing to review"
        detail = "No qualified jobs or rejected audit cases were added for this date."
    else:
        title = "Review complete"
        detail = f"You reviewed all {review.total} jobs for this date."
    return Main(
        Small("Daily review", cls="eyebrow"),
        Time(review.day.strftime("%A, %B %-d"), datetime=review.day.isoformat()),
        _logout_form(csrf_token),
        Div(
            H1(title),
            P(detail),
            A("Check again", href=_day_url(review.day), cls="retry"),
            cls="state",
        ),
        cls="review-shell state-shell",
    )


def _logout_form(csrf_token: str) -> object:
    return Form(
        Input(type="hidden", name="csrf_token", value=csrf_token),
        Button("Sign out", type="submit", cls="logout"),
        action="/logout",
        method="post",
    )


def _unavailable_response(review_day: date) -> HTMLResponse:
    return _state_response(
        "Review is unavailable",
        "The database could not load this review. Your previous decisions are unchanged.",
        action=A("Retry", href=_day_url(review_day), cls="retry"),
        status_code=503,
    )


def _conflict_response(review_day: date, reason: str) -> HTMLResponse:
    return _state_response(
        "This review changed",
        reason,
        action=A("Load the current job", href=_day_url(review_day), cls="retry"),
        status_code=409,
    )


def _state_response(
    title: str,
    detail: str,
    *,
    action: object | None = None,
    status_code: int,
) -> HTMLResponse:
    content = Main(
        Small("Daily review", cls="eyebrow"),
        Div(H1(title), P(detail), action, cls="state"),
        cls="review-shell state-shell",
    )
    return HTMLResponse(_document(content), status_code=status_code)


def _document(content: object) -> str:
    return str(
        to_xml(
            Html(
                Head(
                    Meta(charset="utf-8"),
                    Meta(name="viewport", content="width=device-width, initial-scale=1"),
                    Title("Daily job review"),
                    Style(_CSS),
                ),
                Body(content),
                lang="en",
            )
        )
    )


def _parse_day(value: str, fallback: date) -> date | None:
    if not value:
        return fallback
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _form_text(form: FormData, key: str) -> str:
    value = form.get(key)
    return value if isinstance(value, str) else ""


def _valid_csrf(request: Request, supplied: str) -> bool:
    expected = request.session.get("csrf_token")
    return isinstance(expected, str) and hmac.compare_digest(supplied, expected)


def _safe_next(value: str) -> str:
    if value.startswith("/") and not value.startswith(("//", "/\\")):
        return value
    return "/"


def _default_profile(item: ReviewItem) -> TargetProfile:
    if item.matched_profile == "early-stage-product-engineer":
        return "early-stage-product-engineer"
    if item.matched_profile == "applied-ai-product-engineer":
        return "applied-ai-product-engineer"
    return "neither"


def _day_url(review_day: date) -> str:
    return f"/review?day={review_day.isoformat()}"


_CSS = """
:root {
  --ink: #20201d;
  --muted: #6f6c64;
  --paper: #f4f0e8;
  --panel: #fffdf8;
  --line: #d9d2c6;
  --accent: #bf4b36;
  --accent-dark: #913522;
  font-family: Inter, ui-sans-serif, system-ui, sans-serif;
  color: var(--ink);
  background: var(--paper);
}
* { box-sizing: border-box; }
body { margin: 0; min-height: 100vh; background: var(--paper); }
a { color: var(--accent-dark); text-underline-offset: 0.2em; }
button, select, textarea { font: inherit; }
.review-shell { width: min(100% - 2rem, 860px); margin: 0 auto; padding: 3.5rem 0 5rem; }
.review-header { display: flex; justify-content: space-between; gap: 2rem; align-items: end; }
h1, h2 { font-family: Georgia, 'Times New Roman', serif; letter-spacing: -0.025em; margin: 0; }
h1 { font-size: clamp(2.2rem, 6vw, 4.4rem); line-height: 0.98; max-width: 10ch; }
h2 { font-size: clamp(2rem, 5vw, 3.5rem); line-height: 1.04; }
.eyebrow, .status-kicker, .lane-label { text-transform: uppercase; letter-spacing: 0.14em; font-weight: 800; }
.eyebrow { display: block; color: var(--accent-dark); margin-bottom: 0.75rem; }
.review-meta { text-align: right; color: var(--muted); }
.review-meta time { color: var(--ink); font-weight: 750; }
.progress-copy { margin: 0.35rem 0 0; }
.logout { border: 0; background: transparent; color: var(--accent-dark); cursor: pointer; padding: 0.5rem 0; }
.progress-line { display: flex; gap: 1rem; align-items: center; border-bottom: 1px solid var(--line); padding: 1.5rem 0 0.9rem; color: var(--muted); }
.lane-label { color: var(--accent-dark); margin-right: auto; }
.audit-progress { opacity: 0.7; }
.job-card { margin-top: 2rem; background: var(--panel); border: 1px solid var(--line); border-top: 5px solid var(--accent); padding: clamp(1.25rem, 4vw, 2.5rem); box-shadow: 0 1.2rem 3rem rgb(54 45 32 / 0.08); }
.audit-card { border-top-color: var(--line); box-shadow: none; }
.card-topline { display: flex; justify-content: space-between; gap: 1rem; align-items: center; margin-bottom: 1.5rem; }
.status-kicker { color: var(--accent-dark); font-size: 0.75rem; }
.audit-card .status-kicker { color: var(--muted); }
.job-meta { color: var(--muted); font-size: 1.05rem; }
.company { color: var(--ink); font-weight: 800; }
.evaluation-reason { border-left: 3px solid var(--accent); padding-left: 1rem; margin: 1.5rem 0; font-weight: 650; }
.audit-card .evaluation-reason { border-left-color: var(--line); color: var(--muted); }
.job-description { max-height: 45vh; overflow: auto; white-space: pre-wrap; font: 1rem/1.7 Inter, ui-sans-serif, system-ui, sans-serif; background: #f8f5ee; border: 0; border-radius: 0; padding: 1.25rem; color: var(--ink); }
details { margin: 1.5rem 0; border-top: 1px solid var(--line); padding-top: 1rem; }
summary { cursor: pointer; font-weight: 800; color: var(--accent-dark); min-height: 44px; display: flex; align-items: center; }
.context-fields { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; padding-top: 1rem; }
.context-fields label { display: grid; gap: 0.45rem; font-weight: 700; }
.context-fields select, .context-fields textarea { width: 100%; border: 1px solid var(--line); background: white; padding: 0.8rem; color: var(--ink); }
.note-field, .block-company { grid-column: 1 / -1; }
.block-company { display: flex !important; grid-template-columns: auto 1fr !important; align-items: center; min-height: 44px; }
.block-company input { width: 1.2rem; height: 1.2rem; accent-color: var(--accent); }
.decision-row { display: grid; grid-template-columns: 1.3fr 1fr 1fr; gap: 0.75rem; padding: 0; border: 0; }
.decision-row legend { font-weight: 800; margin-bottom: 0.75rem; }
.decision { min-height: 58px; border: 2px solid var(--ink); background: transparent; color: var(--ink); font-weight: 850; cursor: pointer; }
.decision:hover, .decision:focus-visible { transform: translateY(-1px); box-shadow: 0 0.35rem 0 var(--ink); }
.decision:focus-visible, a:focus-visible, summary:focus-visible, select:focus-visible, textarea:focus-visible { outline: 3px solid var(--accent); outline-offset: 3px; }
.pursue { background: var(--accent); border-color: var(--accent); color: white; }
.pursue:hover, .pursue:focus-visible { background: var(--accent-dark); border-color: var(--accent-dark); }
.reject { color: var(--muted); border-color: var(--muted); }
.state-shell { min-height: 100vh; display: grid; align-content: center; }
.state { margin-top: 1.25rem; background: var(--panel); border-left: 5px solid var(--accent); padding: clamp(1.5rem, 5vw, 3rem); }
.state h1 { max-width: 14ch; }
.state p { color: var(--muted); font-size: 1.08rem; max-width: 48ch; line-height: 1.6; }
.retry { display: inline-flex; min-height: 48px; align-items: center; padding: 0 1.1rem; margin-top: 0.5rem; border: 2px solid var(--accent); font-weight: 800; }
.login-shell { display: grid; min-height: 100vh; place-items: center; padding: 1rem; }
.login-card { width: min(100%, 440px); background: var(--panel); border: 1px solid var(--line); padding: 2rem; }
.login-card label { display: grid; gap: 0.5rem; font-weight: 700; }
.login-card input { width: 100%; border: 1px solid var(--line); padding: 0.8rem; }
.login-card button { width: 100%; min-height: 48px; margin-top: 1rem; border: 0; background: var(--accent); color: white; font-weight: 800; cursor: pointer; }
.error { color: var(--accent-dark); font-weight: 700; }
@media (max-width: 640px) {
  .review-shell { width: min(100% - 1.25rem, 860px); padding-top: 1.5rem; }
  .review-header { display: block; }
  .review-meta { text-align: left; margin-top: 1.25rem; }
  .progress-line { flex-wrap: wrap; gap: 0.5rem 1rem; }
  .lane-label { width: 100%; }
  .card-topline { align-items: start; }
  .context-fields { grid-template-columns: 1fr; }
  .note-field, .block-company { grid-column: auto; }
  .decision-row { grid-template-columns: 1fr; }
  .pursue { order: -2; }
  .unsure { order: -1; }
  .job-description { max-height: 50vh; }
}
@media (prefers-reduced-motion: reduce) {
  .decision:hover, .decision:focus-visible { transform: none; }
}
"""
