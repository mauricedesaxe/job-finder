# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUntypedFunctionDecorator=false, reportUnusedFunction=false, reportMissingTypeStubs=false
from __future__ import annotations

import secrets
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Literal

import psycopg
from fasthtml.common import (
    A,
    Button,
    Div,
    FastHTML,
    Form,
    H1,
    H2,
    Input,
    Label,
    Li,
    P,
    Request,
    Section,
    Select,
    Small,
    Ul,
    Option,
)
from pydantic import ValidationError
from starlette.datastructures import FormData
from starlette.responses import HTMLResponse, RedirectResponse

from job_finder.benchmarks.qualification_activation import (
    ActivateQualificationTargetCommand,
    QualificationActivationError,
    activate_qualification_target,
    get_active_qualification_target,
)
from job_finder.benchmarks.qualification_promotions import (
    PromotionEvidenceSelection,
    preview_qualification_promotion,
    record_qualification_promotion_decision,
)
from job_finder.database import ConnectionFactory
from job_finder.evaluation.qualification_components import QualificationTargetId
from job_finder.web.security import csrf_token, verified_csrf_token
from job_finder.web.shell import document, sidebar_page

_EVIDENCE_FIELDS = (
    ("input_preparation_evidence_id", "Input preparation evidence"),
    ("relevance_evidence_id", "Relevance evidence"),
    ("enrichment_evidence_id", "Enrichment evidence"),
    ("deduplication_evidence_id", "Deduplication evidence"),
    ("composition_evidence_id", "Composition evidence"),
    ("relevance_comparison_id", "Relevance comparison"),
)


def register_qualification_promotion_routes(
    app: FastHTML,
    *,
    connect: ConnectionFactory,
    artifact_path: Path,
    actor: str,
    now: Callable[[], datetime],
) -> None:
    @app.route("/configuration/qualification-promotion", methods=["GET"])
    def show(request: Request) -> HTMLResponse:
        token = csrf_token(request)
        if token is None:
            return HTMLResponse(status_code=401)
        return _page(connect, token, request.query_params.get("notice"))

    @app.route("/configuration/qualification-promotion/preview", methods=["POST"])
    async def preview(request: Request) -> HTMLResponse:
        form = await request.form()
        token = verified_csrf_token(request, form)
        if token is None:
            return HTMLResponse(status_code=403)
        try:
            baseline, candidate = _targets(form)
            with connect() as connection:
                result = preview_qualification_promotion(
                    connection, baseline, candidate, _evidence(form), artifact_path
                )
            message = (
                "Evidence is eligible for approval."
                if result.eligible
                else "Evidence is incomplete: " + "; ".join(result.failures)
            )
            return _page(connect, token, message, submitted=form)
        except (ValueError, ValidationError, psycopg.Error) as error:
            return _page(connect, token, _error(error), 422, submitted=form)

    @app.route("/configuration/qualification-promotion/decide", methods=["POST"])
    async def decide(request: Request) -> HTMLResponse | RedirectResponse:
        form = await request.form()
        token = verified_csrf_token(request, form)
        if token is None:
            return HTMLResponse(status_code=403)
        try:
            baseline, candidate = _targets(form)
            decision = str(form.get("decision", ""))
            if decision not in ("approved", "rejected"):
                raise ValueError("Choose approve or reject.")
            approved_or_rejected: Literal["approved", "rejected"] = (
                "approved" if decision == "approved" else "rejected"
            )
            with connect() as connection:
                result = record_qualification_promotion_decision(
                    connection,
                    baseline_target_id=baseline,
                    candidate_target_id=candidate,
                    evidence=_evidence(form),
                    artifact_path=artifact_path,
                    decision=approved_or_rejected,
                    reason=str(form.get("reason", "")),
                    actor=actor,
                    created_at=now(),
                    idempotency_key=str(form.get("idempotency_key", "")),
                )
        except (ValueError, ValidationError, psycopg.Error) as error:
            return _page(connect, token, _error(error), 422, submitted=form)
        return RedirectResponse(
            f"/configuration/qualification-promotion?notice=Decision+{result.id}+recorded",
            status_code=303,
        )

    @app.route("/configuration/qualification-promotion/activate", methods=["POST"])
    async def activate(request: Request) -> HTMLResponse | RedirectResponse:
        form = await request.form()
        token = verified_csrf_token(request, form)
        if token is None:
            return HTMLResponse(status_code=403)
        try:
            observed = str(form.get("expected_target_id", ""))
            with connect() as connection:
                receipt = activate_qualification_target(
                    connection,
                    ActivateQualificationTargetCommand(
                        idempotency_key=str(form.get("idempotency_key", "")),
                        promotion_decision_id=str(form.get("promotion_decision_id", "")),
                        expected_target_id=QualificationTargetId(observed) if observed else None,
                        expected_generation=int(str(form.get("expected_generation", ""))),
                        actor=actor,
                        timestamp=now(),
                    ),
                    artifact_path,
                )
            if receipt.outcome == "active_changed":
                return _page(connect, token, "Active target changed. Reload and retry.", 409)
        except (ValueError, ValidationError, psycopg.Error, QualificationActivationError) as error:
            return _page(connect, token, _error(error), 422)
        return RedirectResponse(
            "/configuration/qualification-promotion?notice=Qualification+activated",
            status_code=303,
        )


def _targets(form: FormData) -> tuple[QualificationTargetId | None, QualificationTargetId]:
    baseline = str(form.get("baseline_target_id", ""))
    candidate = str(form.get("candidate_target_id", ""))
    if (baseline and len(baseline) != 64) or len(candidate) != 64:
        raise ValueError("Choose a candidate and a valid baseline target.")
    return QualificationTargetId(baseline) if baseline else None, QualificationTargetId(candidate)


def _evidence(form: FormData) -> PromotionEvidenceSelection:
    return PromotionEvidenceSelection.model_validate(
        {name: str(form.get(name, "")) or None for name, _label in _EVIDENCE_FIELDS}
    )


def _error(error: ValueError | psycopg.Error) -> str:
    if isinstance(error, psycopg.Error):
        return "Qualification promotion is unavailable. Reload and try again."
    if isinstance(error, ValidationError):
        return str(error.errors()[0]["msg"]).removeprefix("Value error, ")
    return str(error)


def _page(
    connect: ConnectionFactory,
    token: str,
    notice: str | None,
    status_code: int = 200,
    *,
    submitted: FormData | None = None,
) -> HTMLResponse:
    with connect() as connection:
        active = get_active_qualification_target(connection)
        targets = connection.execute(
            "SELECT id FROM qualification_targets ORDER BY created_at DESC, id DESC LIMIT 20"
        ).fetchall()
        evidence = connection.execute(
            "SELECT id, target_id, phase FROM qualification_phase_evidence WHERE origin = 'canonical' AND outcome = 'passed' ORDER BY created_at DESC LIMIT 25"
        ).fetchall()
        decisions = connection.execute(
            "SELECT id, candidate_target_id, decision FROM qualification_promotion_decisions ORDER BY created_at DESC LIMIT 10"
        ).fetchall()
    target_ids = tuple(str(row[0]) for row in targets)
    body = sidebar_page(
        "configuration",
        token,
        Section(
            Small("Qualification promotion"),
            H1("Review evidence and activate"),
            P(
                "Use canonical evidence to approve a complete candidate. For the first activation, choose No active baseline and provide passed evidence for every phase."
            ),
            P(notice, role="status") if notice else None,
            P(f"Active target: {active.target_id or 'none'}. Generation {active.generation}."),
            _decision_form(token, target_ids, submitted),
            H2("Recent canonical evidence"),
            Ul(*(Li(f"{row[2]} · target {str(row[1])[:12]} · {row[0]}") for row in evidence))
            if evidence
            else P("No canonical evidence recorded yet."),
            H2("Recent decisions"),
            Ul(*(Li(f"{row[2]} · target {str(row[1])[:12]} · {row[0]}") for row in decisions))
            if decisions
            else P("No decisions recorded yet."),
            _activation_form(token, active.target_id, active.generation, decisions),
            A("Qualification candidates", href="/configuration/qualification-targets"),
            cls="review-shell configuration-shell",
        ),
    )
    return HTMLResponse(document(body, title="Qualification promotion"), status_code=status_code)


def _decision_form(token: str, targets: tuple[str, ...], submitted: FormData | None) -> object:
    def selected(name: str) -> str:
        return "" if submitted is None else str(submitted.get(name, ""))

    return Form(
        Input(type="hidden", name="csrf_token", value=token),
        Input(type="hidden", name="idempotency_key", value=secrets.token_hex(16)),
        Label(
            "Baseline target",
            Select(
                Option(
                    "No active baseline (first activation)",
                    value="",
                    selected=selected("baseline_target_id") == "",
                ),
                *(
                    Option(value, value=value, selected=value == selected("baseline_target_id"))
                    for value in targets
                ),
                name="baseline_target_id",
            ),
        ),
        Label(
            "Candidate target",
            Select(
                *(
                    Option(value, value=value, selected=value == selected("candidate_target_id"))
                    for value in targets
                ),
                name="candidate_target_id",
            ),
        ),
        *(
            Label(
                label,
                Input(name=name, value=selected(name), placeholder="64-character evidence ID"),
            )
            for name, label in _EVIDENCE_FIELDS
        ),
        Label("Reason for a decision", Input(name="reason", value=selected("reason"))),
        Label(
            "Decision",
            Select(
                Option("Approve", value="approved"),
                Option("Reject", value="rejected"),
                name="decision",
            ),
        ),
        Button(
            "Preview evidence",
            type="submit",
            formaction="/configuration/qualification-promotion/preview",
        ),
        Button(
            "Record decision",
            type="submit",
            formaction="/configuration/qualification-promotion/decide",
        ),
        action="/configuration/qualification-promotion/preview",
        method="post",
    )


def _activation_form(
    token: str,
    target_id: QualificationTargetId | None,
    generation: int,
    decisions: list[tuple[object, ...]],
) -> object:
    approved = tuple(str(row[0]) for row in decisions if row[2] == "approved")
    return Div(
        H2("Activate an approved target"),
        Form(
            Input(type="hidden", name="csrf_token", value=token),
            Input(type="hidden", name="idempotency_key", value=secrets.token_hex(16)),
            Input(type="hidden", name="expected_target_id", value=target_id or ""),
            Input(type="hidden", name="expected_generation", value=str(generation)),
            Label(
                "Approved decision",
                Select(
                    *(Option(value, value=value) for value in approved),
                    name="promotion_decision_id",
                ),
            ),
            Button("Activate qualification", type="submit", disabled=not approved),
            action="/configuration/qualification-promotion/activate",
            method="post",
        ),
    )
