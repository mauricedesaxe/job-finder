# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUntypedFunctionDecorator=false, reportUnusedFunction=false, reportMissingTypeStubs=false
from __future__ import annotations

import hmac
import secrets
import time
from collections import deque
from collections.abc import Callable
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
from job_finder.review.models import (
    DailyReview,
    ReviewConflict,
    ReviewItem,
    ReviewJob,
    ReviewSubmission,
)
from job_finder.review.postgres import ReviewService

DateClock = Callable[[], date]
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
    def review_page(request: Request, day: str = "", tab: str = "") -> HTMLResponse:
        review_day = _parse_day(day, today())
        if review_day is None:
            return _state_response(
                "Choose a valid review date",
                "Use a date in YYYY-MM-DD format.",
                status_code=400,
            )
        try:
            daily_review = service.open_day(review_day)
            adjacent = service.adjacent_days(review_day)
        except psycopg.Error:
            return _unavailable_response(review_day)
        csrf_token = request.session.get("csrf_token")
        if not isinstance(csrf_token, str):
            return HTMLResponse(status_code=401)
        return HTMLResponse(_document(_review_page(daily_review, adjacent, tab, csrf_token)))

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

    @app.route("/review/item/{review_item_id}", methods=["GET"])
    def review_item_page(
        review_item_id: str, request: Request, day: str = "", edit: str = ""
    ) -> HTMLResponse:
        review_day = _parse_day(day, today())
        if review_day is None:
            return _item_not_found_response(today())
        try:
            daily_review = service.open_day(review_day)
        except psycopg.Error:
            return _unavailable_response(review_day)
        csrf_token = request.session.get("csrf_token")
        if not isinstance(csrf_token, str):
            return HTMLResponse(status_code=401)
        try:
            item_id = UUID(review_item_id)
        except ValueError:
            return _item_not_found_response(review_day)
        items = daily_review.ordered_items
        item = next((candidate for candidate in items if candidate.id == item_id), None)
        if item is None:
            return _item_not_found_response(review_day)
        show_form = not item.reviewed or edit == "1"
        return HTMLResponse(_document(_item_page(review_day, items, item, show_form, csrf_token)))

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
    return RedirectResponse(
        _next_review_url(service, review_day, submission.review_item_id), status_code=303
    )


def _next_review_url(service: ReviewService, review_day: date, saved_item_id: UUID) -> str:
    try:
        review = service.open_day(review_day)
    except psycopg.Error:
        return _day_url(review_day)
    items = review.ordered_items
    index = next((i for i, item in enumerate(items) if item.id == saved_item_id), None)
    if index is not None:
        for item in items[index + 1 :]:
            if not item.reviewed:
                return _item_url(item.id, review_day)
    return _day_url(review_day)


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


def _review_page(
    review: DailyReview,
    adjacent: tuple[date | None, date | None],
    tab: str,
    csrf_token: str,
) -> object:
    active_tab = "reviewed" if tab == "reviewed" else "to_review"
    return Main(
        _review_header(review, adjacent, csrf_token),
        _review_tabs(review, active_tab),
        _review_tab_content(review, active_tab),
        cls="review-shell",
    )


def _review_header(
    review: DailyReview,
    adjacent: tuple[date | None, date | None],
    csrf_token: str,
) -> object:
    previous_day, next_day = adjacent
    return Div(
        Div(
            Small("Daily review", cls="eyebrow"),
            Div(
                _day_arrow("←", previous_day, "Previous review day"),
                A("Today", href="/review", cls="day-link day-today"),
                _day_arrow("→", next_day, "Next review day"),
                cls="day-nav",
            ),
            cls="header-row",
        ),
        Div(
            H1(_day_title(review.day), cls="day-title"),
            _logout_form(csrf_token),
            cls="title-row",
        ),
        cls="review-header",
    )


def _day_arrow(glyph: str, target_day: date | None, label: str) -> object:
    if target_day is None:
        return Span(glyph, cls="day-arrow day-arrow-off", aria_hidden="true")
    return A(glyph, href=_day_url(target_day), cls="day-arrow", aria_label=label)


def _review_tabs(review: DailyReview, active_tab: str) -> object:
    pending = len(review.qualified.pending) + len(review.rejected_audit.pending)
    return Div(
        _tab("To review", pending, _day_url(review.day), active_tab == "to_review"),
        _tab(
            "Reviewed",
            review.completed,
            _day_url(review.day, "reviewed"),
            active_tab == "reviewed",
        ),
        cls="review-tabs",
    )


def _tab(name: str, count: int, href: str, active: bool) -> object:
    attributes: dict[str, str] = {}
    if active:
        attributes["aria_current"] = "page"
    return A(f"{name} · {count}", href=href, cls="review-tab", **attributes)


def _review_tab_content(review: DailyReview, active_tab: str) -> object:
    if active_tab == "reviewed":
        return _reviewed_tab_content(review)
    return _to_review_tab_content(review)


def _to_review_tab_content(review: DailyReview) -> object:
    pending = (*review.qualified.pending, *review.rejected_audit.pending)
    if pending:
        return Ul(
            *(_pending_row(item, review.day) for item in pending),
            cls="job-list",
        )
    if review.total == 0:
        return _state_block(
            "Nothing to review",
            "No qualified jobs or second-look cases were added for this date.",
        )
    return _state_block(
        "All done for this day",
        f"You reviewed all {review.total} jobs for this date.",
        action=A("See the reviewed list", href=_day_url(review.day, "reviewed"), cls="retry"),
    )


def _reviewed_tab_content(review: DailyReview) -> object:
    reviewed = (*review.qualified.reviewed_items, *review.rejected_audit.reviewed_items)
    if not reviewed:
        return _state_block(
            "Nothing reviewed yet",
            "Decisions you record on this day are listed here.",
        )
    return Ul(
        *(_reviewed_row(item, review.day) for item in reviewed),
        cls="job-list",
    )


def _pending_row(item: ReviewItem, review_day: date) -> object:
    lane_chip = "chip lane-new" if item.lane == "qualified" else "chip lane-second-look"
    lane_name = "New result" if item.lane == "qualified" else "Second look"
    return Li(
        A(
            Span(lane_name, cls=lane_chip),
            Strong(item.job.title, cls="job-title"),
            Span(_job_subline(item.job), cls="job-subline"),
            href=_item_url(item.id, review_day),
            cls="job-row",
        ),
        cls="job-list-item",
    )


def _reviewed_row(item: ReviewItem, review_day: date) -> object:
    decision = item.decision or ""
    return Li(
        A(
            Span(
                decision.capitalize(),
                cls=f"chip decision-chip decision-{decision}",
            ),
            Strong(item.job.title, cls="job-title"),
            Span(_job_subline(item.job), cls="job-subline"),
            href=_item_url(item.id, review_day),
            cls="job-row job-row-reviewed",
        ),
        cls="job-list-item",
    )


def _job_subline(job: ReviewJob) -> str:
    location = job.location or "Location not specified"
    return f"{job.company} · {location}"


def _state_block(title: str, detail: str, *, action: object | None = None) -> object:
    return Div(
        H2(title),
        P(detail),
        action,
        cls="state",
    )


def _item_page(
    review_day: date,
    items: tuple[ReviewItem, ...],
    item: ReviewItem,
    show_form: bool,
    csrf_token: str,
) -> object:
    position = next(i for i, candidate in enumerate(items) if candidate.id == item.id)
    previous_item = items[position - 1] if position > 0 else None
    next_item = items[position + 1] if position + 1 < len(items) else None
    return Main(
        Div(
            A(
                f"← All jobs, {_day_title(review_day)}",
                href=_day_url(review_day),
                cls="back-link",
            ),
            Span(f"{position + 1} of {len(items)}", cls="position-marker"),
            Div(
                _item_arrow("← Prev", previous_item, review_day),
                _item_arrow("Next →", next_item, review_day),
                cls="item-nav",
            ),
            cls="item-topbar",
        ),
        None if show_form else _recorded_banner(review_day, item),
        _job_card(review_day, item, show_form, csrf_token),
        cls="review-shell",
    )


def _item_arrow(glyph: str, target: ReviewItem | None, review_day: date) -> object:
    if target is None:
        return Span(glyph, cls="item-nav-link item-nav-off", aria_hidden="true")
    return A(glyph, href=_item_url(target.id, review_day), cls="item-nav-link")


def _recorded_banner(review_day: date, item: ReviewItem) -> object:
    return Div(
        Strong(f"You decided: {item.decision}", cls="recorded-decision"),
        P(f"“{item.note}”", cls="recorded-note") if item.note else None,
        A(
            "Change",
            href=f"{_item_url(item.id, review_day)}&edit=1",
            cls="change-link",
        ),
        cls="recorded-banner",
    )


def _job_card(review_day: date, item: ReviewItem, show_form: bool, csrf_token: str) -> object:
    second_look = item.lane == "rejected_audit"
    return Div(
        Div(
            Span("Second look" if second_look else "New result", cls="status-kicker"),
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
        Div(
            Small("Why it's here", cls="why-label"),
            P(item.evaluation_reason, cls="evaluation-reason"),
        ),
        Pre(item.job.description, cls="job-description", aria_label="Job description"),
        _decision_form(review_day, item, csrf_token) if show_form else None,
        cls="job-card audit-card" if second_look else "job-card",
    )


def _decision_form(review_day: date, item: ReviewItem, csrf_token: str) -> object:
    return Form(
        Input(type="hidden", name="review_day", value=review_day.isoformat()),
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
            Input(
                type="checkbox",
                name="block_company",
                checked=item.reviewed and item.decision == "reject",
            ),
            " Block this company from future results",
            cls="block-company",
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


def _item_not_found_response(review_day: date) -> HTMLResponse:
    return _state_response(
        "Review item not found",
        "This job is not part of the review for this date.",
        action=A("Back to the review", href=_day_url(review_day), cls="retry"),
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


def _day_url(review_day: date, tab: str | None = None) -> str:
    url = f"/review?day={review_day.isoformat()}"
    if tab:
        return f"{url}&tab={tab}"
    return url


def _item_url(item_id: UUID, review_day: date) -> str:
    return f"/review/item/{item_id}?day={review_day.isoformat()}"


def _day_title(review_day: date) -> str:
    return review_day.strftime("%A, %B %-d")


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
.review-header { margin-bottom: 0.5rem; }
.header-row { display: flex; justify-content: space-between; align-items: center; gap: 1rem; }
.day-nav { display: flex; gap: 0.5rem; align-items: center; }
.day-arrow, .item-nav-link { min-width: 44px; min-height: 44px; display: inline-flex; align-items: center; justify-content: center; padding: 0 0.6rem; border: 2px solid var(--line); color: var(--ink); font-weight: 800; text-decoration: none; }
.day-arrow-off, .item-nav-off { opacity: 0.35; border-style: dashed; }
.day-today { padding: 0 1rem; border-color: var(--ink); }
h1, h2 { font-family: Georgia, 'Times New Roman', serif; letter-spacing: -0.025em; margin: 0; }
h1 { font-size: clamp(2.2rem, 6vw, 4.4rem); line-height: 0.98; max-width: 14ch; }
h2 { font-size: clamp(1.6rem, 4vw, 2.4rem); line-height: 1.04; }
.eyebrow, .status-kicker { text-transform: uppercase; letter-spacing: 0.14em; font-weight: 800; }
.eyebrow { display: block; color: var(--accent-dark); }
.title-row { display: flex; justify-content: space-between; align-items: end; gap: 2rem; margin-top: 0.75rem; }
.logout { border: 0; background: transparent; color: var(--accent-dark); cursor: pointer; padding: 0.5rem 0; }
.review-tabs { display: flex; gap: 0.5rem; border-bottom: 1px solid var(--line); margin-top: 1.5rem; }
.review-tab { padding: 0.7rem 1.1rem; border: 2px solid transparent; border-bottom: 0; color: var(--muted); font-weight: 800; text-decoration: none; }
.review-tab[aria-current="page"] { color: var(--ink); border-color: var(--line); background: var(--panel); position: relative; top: 1px; }
.job-list { list-style: none; padding: 0; margin: 1.5rem 0 0; display: grid; gap: 0.75rem; }
.job-row { display: block; background: var(--panel); border: 1px solid var(--line); border-left: 5px solid var(--accent); padding: 1rem 1.25rem; text-decoration: none; color: inherit; }
.job-row:hover, .job-row:focus-visible { transform: translateY(-1px); box-shadow: 0 0.35rem 1rem rgb(54 45 32 / 0.12); }
.job-row.lane-second-look, .job-row.job-row-reviewed { border-left-color: var(--line); }
.chip { display: inline-block; font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.12em; font-weight: 800; padding: 0.25rem 0.6rem; border-radius: 999px; border: 1px solid var(--line); color: var(--muted); }
.lane-new { color: var(--accent-dark); border-color: var(--accent); }
.decision-pursue { color: #1d6b40; border-color: #1d6b40; }
.decision-unsure { color: #8a6d1a; border-color: #8a6d1a; }
.job-title { display: block; margin-top: 0.4rem; font-size: 1.15rem; }
.job-subline { display: block; margin-top: 0.2rem; color: var(--muted); font-size: 0.95rem; }
.item-topbar { display: flex; justify-content: space-between; align-items: center; gap: 1rem; flex-wrap: wrap; border-bottom: 1px solid var(--line); padding-bottom: 1rem; margin-bottom: 0.5rem; }
.back-link { color: var(--accent-dark); font-weight: 800; text-decoration: none; }
.position-marker { color: var(--muted); font-weight: 700; }
.item-nav { display: flex; gap: 0.5rem; }
.job-card { margin-top: 2rem; background: var(--panel); border: 1px solid var(--line); border-top: 5px solid var(--accent); padding: clamp(1.25rem, 4vw, 2.5rem); box-shadow: 0 1.2rem 3rem rgb(54 45 32 / 0.08); }
.audit-card { border-top-color: var(--line); box-shadow: none; }
.card-topline { display: flex; justify-content: space-between; gap: 1rem; align-items: center; margin-bottom: 1.5rem; }
.status-kicker { color: var(--accent-dark); font-size: 0.75rem; }
.audit-card .status-kicker { color: var(--muted); }
.job-meta { color: var(--muted); font-size: 1.05rem; }
.company { color: var(--ink); font-weight: 800; }
.why-label { display: block; text-transform: uppercase; letter-spacing: 0.12em; font-size: 0.72rem; font-weight: 800; color: var(--muted); margin-bottom: 0.35rem; }
.evaluation-reason { border-left: 3px solid var(--accent); padding-left: 1rem; margin: 0; font-weight: 650; }
.audit-card .evaluation-reason { border-left-color: var(--line); color: var(--muted); }
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
.note-field { display: grid; gap: 0.45rem; font-weight: 700; margin: 1.5rem 0 1rem; }
.note-field textarea { width: 100%; border: 1px solid var(--line); background: white; padding: 0.8rem; color: var(--ink); }
.block-company { display: flex; align-items: center; min-height: 44px; font-weight: 700; }
.block-company input { width: 1.2rem; height: 1.2rem; accent-color: var(--accent); margin-right: 0.6rem; }
.recorded-banner { background: #f8f5ee; border: 1px solid var(--line); border-left: 3px solid var(--accent); border-radius: 6px; padding: 0.9rem 1.1rem; margin: 0 0 1.5rem; display: flex; justify-content: space-between; align-items: baseline; gap: 1rem; flex-wrap: wrap; }
.recorded-decision { color: var(--ink); text-transform: capitalize; }
.recorded-note { margin: 0; color: var(--muted); }
.change-link { color: var(--accent-dark); font-weight: 800; }
.block-company input { width: 1.2rem; height: 1.2rem; accent-color: var(--accent); }
.decision-row { display: grid; grid-template-columns: 1.3fr 1fr 1fr; gap: 0.75rem; padding: 0; border: 0; }
.decision-row legend { font-weight: 800; margin-bottom: 0.75rem; }
.decision { min-height: 58px; border: 2px solid var(--ink); background: transparent; color: var(--ink); font-weight: 850; cursor: pointer; }
.decision:hover, .decision:focus-visible { transform: translateY(-1px); box-shadow: 0 0.35rem 0 var(--ink); }
.decision:focus-visible, a:focus-visible, select:focus-visible, textarea:focus-visible { outline: 3px solid var(--accent); outline-offset: 3px; }
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
  .title-row { display: block; }
  .day-nav { margin-top: 1rem; }
  .item-topbar { align-items: start; flex-direction: column; }
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
