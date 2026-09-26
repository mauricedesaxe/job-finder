# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportMissingTypeStubs=false
from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
import logging
from uuid import UUID

import psycopg
from fasthtml.common import (
    A,
    Button,
    Div,
    FastHTML,
    Fieldset,
    Form,
    H1,
    H2,
    Input,
    Label,
    Legend,
    Li,
    P,
    Pre,
    Small,
    Span,
    Strong,
    Textarea,
    Ul,
)
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from job_finder.review.feedback import (
    ReviewConflict,
    ReviewSubmission,
    ReviewSubmitter,
)
from job_finder.review.queue import (
    Compensation,
    ReviewItem,
    ReviewJob,
    ReviewQueue,
    ReviewQueueLoader,
)
from job_finder.web.security import csrf_token, form_text, valid_csrf
from job_finder.web.shell import document, sidebar_page, state_response

_logger = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class ReviewWorkbench:
    load_queue: ReviewQueueLoader
    submit_review: ReviewSubmitter
    actor: str
    now: Callable[[], datetime]

    def register_queue_routes(self, app: FastHTML) -> None:
        app.route(
            "/",
            methods=["GET"],
            name="create_review_app_home",
        )(self._home)
        app.route(
            "/review",
            methods=["GET"],
            name="create_review_app_review_page",
        )(self._review_page_redirect)

    def register_item_routes(self, app: FastHTML) -> None:
        app.route(
            "/review/{review_item_id}",
            methods=["POST"],
            name="create_review_app_submit_review",
        )(self._submit_review)
        app.route(
            "/review/item/{review_item_id}",
            methods=["GET"],
            name="create_review_app_review_item_page",
        )(self._review_item_page)

    def _home(self, request: Request) -> HTMLResponse:
        try:
            queue = self.load_queue()
        except psycopg.Error as error:
            _log_database_failure("load review queue", error)
            return _unavailable_response()
        token = csrf_token(request)
        if token is None:
            return HTMLResponse(status_code=401)
        return HTMLResponse(document(sidebar_page("review", token, _review_page(queue))))

    def _review_page_redirect(self, request: Request) -> Response:
        _ = request
        return RedirectResponse("/", status_code=303)

    async def _submit_review(
        self, review_item_id: str, request: Request
    ) -> HTMLResponse | RedirectResponse:
        form = await request.form()
        if not valid_csrf(request, form_text(form, "csrf_token")):
            return _conflict_response("This review form expired. Reload the page.")
        try:
            item_id = UUID(review_item_id)
        except ValueError:
            return _item_not_found_response()
        try:
            queue = self.load_queue()
        except psycopg.Error as error:
            _log_database_failure("load review before submission", error)
            return _unavailable_response()
        item = _find_item(queue, item_id)
        if item is None:
            return _item_not_found_response()
        if (
            form_text(form, "evaluation_id") != item.evaluation_id
            or form_text(form, "snapshot_id") != item.snapshot_id
        ):
            return _item_not_found_response()
        try:
            submission = ReviewSubmission.model_validate(
                {
                    "review_item_id": review_item_id,
                    "evaluation_id": form_text(form, "evaluation_id"),
                    "snapshot_id": form_text(form, "snapshot_id"),
                    "decision": form_text(form, "decision"),
                    "note": form_text(form, "note"),
                    "block_company": form_text(form, "block_company") == "on",
                    "actor": self.actor,
                    "created_at": self.now(),
                }
            )
        except ValidationError:
            return _conflict_response("This review form is invalid. Reload the page and try again.")
        try:
            result = self.submit_review(submission)
        except psycopg.Error as error:
            _log_database_failure("submit review", error)
            return _uncertain_submission_response()
        if isinstance(result, ReviewConflict):
            return _conflict_response(result.reason)
        if item.reviewed:
            return RedirectResponse(_item_url(item.id), status_code=303)
        position = next(i for i, candidate in enumerate(queue.items) if candidate.id == item_id)
        return RedirectResponse(_successor_url(queue.items, position), status_code=303)

    def _review_item_page(self, review_item_id: str, request: Request) -> HTMLResponse:
        try:
            queue = self.load_queue()
        except psycopg.Error as error:
            _log_database_failure("load review item", error)
            return _unavailable_response()
        token = csrf_token(request)
        if token is None:
            return HTMLResponse(status_code=401)
        try:
            item_id = UUID(review_item_id)
        except ValueError:
            return _item_not_found_response()
        item = _find_item(queue, item_id)
        if item is None:
            return _item_not_found_response()
        if item.reviewed:
            return HTMLResponse(
                document(sidebar_page("review", token, _revision_page(item, token)))
            )
        return HTMLResponse(
            document(sidebar_page("review", token, _item_page(queue.items, item, token)))
        )


def _review_page(queue: ReviewQueue) -> object:
    return Div(
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
    return Div(
        Div(
            A("← All jobs", href="/", cls="back-link"),
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
    return Div(
        Div(
            A("← All jobs", href="/", cls="back-link"),
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
            cls="decision",
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
    return state_response(
        "Review is unavailable",
        "The database could not load this review. Your previous decisions are unchanged.",
        action=A("Retry", href="/", cls="retry"),
        status_code=503,
    )


def _uncertain_submission_response() -> HTMLResponse:
    return state_response(
        "Review result is unknown",
        "The database disconnected while saving. Check the review queue before trying again.",
        action=A("Check review queue", href="/", cls="retry"),
        status_code=503,
    )


def _log_database_failure(operation: str, error: psycopg.Error) -> None:
    _logger.warning("Review %s failed (%s)", operation, type(error).__name__)


def _conflict_response(reason: str) -> HTMLResponse:
    return state_response(
        "This review changed",
        reason,
        action=A("Back to the review", href="/", cls="retry"),
        status_code=409,
    )


def _item_not_found_response() -> HTMLResponse:
    return state_response(
        "Review item not found",
        "This job is not part of the review.",
        action=A("Back to the review", href="/", cls="retry"),
        status_code=404,
    )


def _successor_url(items: tuple[ReviewItem, ...], position: int) -> str:
    following = items[position + 1 :]
    if following:
        return _item_url(following[0].id)
    return "/"


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
