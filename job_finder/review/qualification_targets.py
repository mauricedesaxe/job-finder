# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUntypedFunctionDecorator=false, reportUnusedFunction=false, reportMissingTypeStubs=false
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import psycopg
from fasthtml.common import (
    A,
    Button,
    FastHTML,
    Form,
    H1,
    H2,
    Input,
    Li,
    P,
    Request,
    Section,
    Small,
    Ul,
)
from starlette.responses import HTMLResponse, RedirectResponse

from job_finder.benchmarks.qualification_activation import get_active_qualification_target
from job_finder.database import ConnectionFactory
from job_finder.qualification_target_service import (
    CreateCurrentQualificationCandidateCommand,
    create_current_qualification_candidate,
)
from job_finder.web.security import csrf_token, verified_csrf_token
from job_finder.web.shell import document, sidebar_page


def register_qualification_target_routes(
    app: FastHTML,
    *,
    connect: ConnectionFactory,
    artifact_path: Path,
    actor: str,
    now: Callable[[], datetime],
) -> None:
    @app.route("/configuration/qualification-targets", methods=["GET"])
    def show(request: Request) -> HTMLResponse:
        token = csrf_token(request)
        if token is None:
            return HTMLResponse(status_code=401)
        return _page(connect, token, request.query_params.get("notice"))

    @app.route("/configuration/qualification-targets/candidate", methods=["POST"])
    async def create_candidate(request: Request) -> HTMLResponse | RedirectResponse:
        form = await request.form()
        token = verified_csrf_token(request, form)
        if token is None:
            return HTMLResponse(status_code=403)
        try:
            with connect() as connection:
                candidate = create_current_qualification_candidate(
                    connection,
                    CreateCurrentQualificationCandidateCommand(actor=actor, timestamp=now()),
                    artifact_path,
                )
        except (ValueError, psycopg.Error) as error:
            message = (
                "Qualification candidate is unavailable. Reload and try again."
                if isinstance(error, psycopg.Error)
                else str(error)
            )
            return _page(connect, token, message, status_code=422)
        return RedirectResponse(
            f"/configuration/qualification-targets?notice=Candidate+{candidate.id}+created",
            status_code=303,
        )


def _page(
    connect: ConnectionFactory, token: str, notice: str | None, status_code: int = 200
) -> HTMLResponse:
    with connect() as connection:
        active = get_active_qualification_target(connection)
        rows = connection.execute(
            "SELECT id FROM qualification_targets ORDER BY created_at DESC, id DESC LIMIT 10"
        ).fetchall()
    body = sidebar_page(
        "configuration",
        token,
        Section(
            Small("Qualification targets"),
            H1("Prepare a qualification target"),
            P("A target pins the published definition and executing build for evaluation."),
            P(notice, role="status") if notice else None,
            P(f"Active target: {active.target_id or 'none'}. Generation {active.generation}."),
            Form(
                Input(type="hidden", name="csrf_token", value=token),
                Button("Create candidate from current setup", type="submit", cls="button primary"),
                action="/configuration/qualification-targets/candidate",
                method="post",
            ),
            H2("Recent candidates"),
            Ul(*(Li(str(row[0])) for row in rows)) if rows else P("No candidates yet."),
            A("Back to search setup", href="/configuration"),
            cls="review-shell configuration-shell",
        ),
    )
    return HTMLResponse(document(body, title="Qualification targets"), status_code=status_code)
