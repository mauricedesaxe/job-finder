# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUntypedFunctionDecorator=false, reportUnusedFunction=false, reportMissingTypeStubs=false
from __future__ import annotations

import json
import secrets
from collections.abc import Callable
from datetime import datetime

import psycopg
from fasthtml.common import (
    A,
    Button,
    Details,
    Div,
    FastHTML,
    Form,
    H1,
    H2,
    Input,
    Label,
    P,
    Request,
    Section,
    Small,
    Summary,
    Textarea,
)
from pydantic import ValidationError
from starlette.responses import HTMLResponse, RedirectResponse

from job_finder.acquisition_policy import (
    AcquisitionPolicy,
    AcquisitionPolicyRevisionId,
    acquisition_policy_revision_id,
)
from job_finder.acquisition_policy_activation import (
    ActivateAcquisitionPolicyCommand,
    activate_acquisition_policy,
)
from job_finder.acquisition_policy_service import (
    AcquisitionPolicyDraft,
    ActiveAcquisitionPolicy,
    PublishAcquisitionPolicyCommand,
    ReplaceAcquisitionPolicyDraftCommand,
    get_acquisition_policy_draft,
    get_active_acquisition_policy,
    publish_acquisition_policy,
    replace_acquisition_policy_draft,
)
from job_finder.database import ConnectionFactory
from job_finder.discovery.catalog import SupportedSearchSource
from job_finder.qualification_definition import (
    QualificationDefinition,
    qualification_definition_revision_id,
)
from job_finder.qualification_definition_service import (
    PublishQualificationDefinitionCommand,
    QualificationDefinitionDraft,
    ReplaceQualificationDefinitionDraftCommand,
    get_qualification_definition_draft,
    publish_qualification_definition,
    replace_qualification_definition_draft,
)
from job_finder.web.security import csrf_token, verified_csrf_token
from job_finder.web.shell import document, sidebar_page, state_response


_SPLIT_NOTICES = {
    "acquisition-draft-saved": "Acquisition draft saved.",
    "acquisition-published": "Acquisition published.",
    "acquisition-activated": "Acquisition activated.",
    "qualification-draft-saved": "Qualification draft saved.",
    "qualification-published": "Qualification published.",
}


def register_split_configuration_routes(
    app: FastHTML,
    *,
    connect: ConnectionFactory,
    actor: str,
    now: Callable[[], datetime],
) -> None:
    @app.route("/configuration", methods=["GET"], name="create_review_app_configuration")
    def show(request: Request) -> HTMLResponse:
        token = csrf_token(request)
        if token is None:
            return HTMLResponse(status_code=401)
        return _page(connect, token, _SPLIT_NOTICES.get(request.query_params.get("notice", "")))

    @app.route("/configuration/acquisition/draft", methods=["POST"])
    async def save_acquisition(request: Request) -> HTMLResponse | RedirectResponse:
        form = await request.form()
        token = verified_csrf_token(request, form)
        if token is None:
            return HTMLResponse(status_code=403)
        raw_keywords = str(form.get("search_keywords", ""))
        raw_sources = tuple(str(value) for value in form.getlist("enabled_sources"))
        try:
            keywords = tuple(line.strip() for line in raw_keywords.splitlines() if line.strip())
            if not keywords:
                raise ValueError("Enter at least one search keyword.")
            policy = AcquisitionPolicy.model_validate(
                {
                    "search_keywords": keywords,
                    "enabled_sources": raw_sources,
                }
            )
            with connect() as connection:
                draft = get_acquisition_policy_draft(connection)
                result = replace_acquisition_policy_draft(
                    connection,
                    ReplaceAcquisitionPolicyDraftCommand(
                        expected_base_revision_id=draft.base_revision_id,
                        expected_version=int(str(form.get("draft_version", ""))),
                        policy=policy,
                        actor=actor,
                        timestamp=now(),
                    ),
                )
            if result.kind == "draft_changed":
                return _page(
                    connect,
                    token,
                    "Acquisition draft changed. Reload and retry.",
                    409,
                    acquisition_input=(raw_keywords, raw_sources),
                )
        except psycopg.Error:
            return _unavailable_response()
        except (ValueError, ValidationError) as error:
            return _page(
                connect,
                token,
                _user_error(error),
                422,
                acquisition_input=(raw_keywords, raw_sources),
            )
        return RedirectResponse("/configuration?notice=acquisition-draft-saved", status_code=303)

    @app.route("/configuration/acquisition/publish", methods=["POST"])
    async def publish_acquisition(request: Request) -> HTMLResponse | RedirectResponse:
        form = await request.form()
        token = verified_csrf_token(request, form)
        if token is None:
            return HTMLResponse(status_code=403)
        try:
            with connect() as connection:
                draft = get_acquisition_policy_draft(connection)
                result = publish_acquisition_policy(
                    connection,
                    PublishAcquisitionPolicyCommand(
                        idempotency_key=str(form.get("idempotency_key", "")),
                        expected_draft_version=int(str(form.get("draft_version", ""))),
                        expected_revision_id=acquisition_policy_revision_id(draft.policy),
                        actor=actor,
                        timestamp=now(),
                    ),
                )
            if result.outcome == "draft_changed":
                return _page(connect, token, "Acquisition draft changed. Reload and retry.", 409)
        except psycopg.Error:
            return _uncertain_action_response(
                "/configuration/acquisition/publish",
                token,
                (
                    ("draft_version", str(form.get("draft_version", ""))),
                    ("idempotency_key", str(form.get("idempotency_key", ""))),
                ),
            )
        except (ValueError, ValidationError) as error:
            return _page(connect, token, _user_error(error), 422)
        return RedirectResponse("/configuration?notice=acquisition-published", status_code=303)

    @app.route("/configuration/acquisition/activate", methods=["POST"])
    async def activate_acquisition(request: Request) -> HTMLResponse | RedirectResponse:
        form = await request.form()
        token = verified_csrf_token(request, form)
        if token is None:
            return HTMLResponse(status_code=403)
        try:
            with connect() as connection:
                active = get_active_acquisition_policy(connection)
                receipt = activate_acquisition_policy(
                    connection,
                    ActivateAcquisitionPolicyCommand(
                        idempotency_key=str(form.get("idempotency_key", "")),
                        candidate_revision_id=AcquisitionPolicyRevisionId(
                            str(form.get("candidate_revision_id", ""))
                        ),
                        expected_revision_id=active.revision.id,
                        expected_generation=int(str(form.get("active_generation", ""))),
                        actor=actor,
                        timestamp=now(),
                    ),
                )
            if receipt.outcome == "active_changed":
                return _page(connect, token, "Active acquisition changed. Reload and retry.", 409)
        except psycopg.Error:
            return _uncertain_action_response(
                "/configuration/acquisition/activate",
                token,
                (
                    ("active_generation", str(form.get("active_generation", ""))),
                    ("candidate_revision_id", str(form.get("candidate_revision_id", ""))),
                    ("idempotency_key", str(form.get("idempotency_key", ""))),
                ),
            )
        except (ValueError, ValidationError) as error:
            return _page(connect, token, _user_error(error), 422)
        return RedirectResponse("/configuration?notice=acquisition-activated", status_code=303)

    @app.route("/configuration/qualification/draft", methods=["POST"])
    async def save_qualification(request: Request) -> HTMLResponse | RedirectResponse:
        form = await request.form()
        token = verified_csrf_token(request, form)
        if token is None:
            return HTMLResponse(status_code=403)
        raw_criteria = str(form.get("personal_criteria", ""))
        raw_profiles = str(form.get("target_profiles", ""))
        try:
            definition = QualificationDefinition.model_validate(
                {
                    "personal_criteria": json.loads(raw_criteria),
                    "target_profiles": json.loads(raw_profiles),
                }
            )
            with connect() as connection:
                draft = get_qualification_definition_draft(connection)
                result = replace_qualification_definition_draft(
                    connection,
                    ReplaceQualificationDefinitionDraftCommand(
                        expected_base_revision_id=draft.base_revision_id,
                        expected_version=int(str(form.get("draft_version", ""))),
                        definition=definition,
                        actor=actor,
                        timestamp=now(),
                    ),
                )
            if result.kind == "draft_changed":
                return _page(
                    connect,
                    token,
                    "Qualification draft changed. Reload and retry.",
                    409,
                    qualification_input=(raw_criteria, raw_profiles),
                )
        except psycopg.Error:
            return _unavailable_response()
        except (ValueError, ValidationError) as error:
            return _page(
                connect,
                token,
                _user_error(error),
                422,
                qualification_input=(raw_criteria, raw_profiles),
            )
        return RedirectResponse("/configuration?notice=qualification-draft-saved", status_code=303)

    @app.route("/configuration/qualification/publish", methods=["POST"])
    async def publish_qualification(request: Request) -> HTMLResponse | RedirectResponse:
        form = await request.form()
        token = verified_csrf_token(request, form)
        if token is None:
            return HTMLResponse(status_code=403)
        try:
            with connect() as connection:
                draft = get_qualification_definition_draft(connection)
                receipt = publish_qualification_definition(
                    connection,
                    PublishQualificationDefinitionCommand(
                        idempotency_key=str(form.get("idempotency_key", "")),
                        expected_draft_version=int(str(form.get("draft_version", ""))),
                        expected_revision_id=qualification_definition_revision_id(draft.definition),
                        actor=actor,
                        timestamp=now(),
                    ),
                )
            if receipt.outcome == "draft_changed":
                return _page(connect, token, "Qualification draft changed. Reload and retry.", 409)
        except psycopg.Error:
            return _uncertain_action_response(
                "/configuration/qualification/publish",
                token,
                (
                    ("draft_version", str(form.get("draft_version", ""))),
                    ("idempotency_key", str(form.get("idempotency_key", ""))),
                ),
            )
        except (ValueError, ValidationError) as error:
            return _page(connect, token, _user_error(error), 422)
        return RedirectResponse("/configuration?notice=qualification-published", status_code=303)

    @app.route("/configuration/continue", methods=["POST"])
    async def continue_setup(request: Request) -> HTMLResponse | RedirectResponse:
        form = await request.form()
        token = verified_csrf_token(request, form)
        if token is None:
            return HTMLResponse(status_code=403)
        try:
            with connect() as connection, connection.transaction():
                policy = get_acquisition_policy_draft(connection)
                active = get_active_acquisition_policy(connection)
                definition = get_qualification_definition_draft(connection)
                if (
                    acquisition_policy_revision_id(policy.policy) != active.revision.id
                    or qualification_definition_revision_id(definition.definition)
                    != definition.base_revision_id
                ):
                    return _page(
                        connect, token, "Publish and activate both saved sections first.", 409
                    )
                advanced = (
                    connection.execute(
                        """
                        UPDATE owner_onboarding SET stage = 'budget', updated_at = %s
                        WHERE singleton_id = 1 AND stage = 'preferences'
                        """,
                        (now(),),
                    ).rowcount
                    == 1
                )
        except psycopg.Error:
            return _unavailable_response()
        except (ValueError, ValidationError) as error:
            return _page(connect, token, _user_error(error), 422)
        if not advanced:
            return _page(connect, token, "Setup stage changed. Reload to continue.", 409)
        return RedirectResponse("/setup/budget", status_code=303)


def _page(
    connect: ConnectionFactory,
    token: str,
    notice: str | None,
    status_code: int = 200,
    *,
    acquisition_input: tuple[str, tuple[str, ...]] | None = None,
    qualification_input: tuple[str, str] | None = None,
) -> HTMLResponse:
    try:
        with connect() as connection:
            policy = get_acquisition_policy_draft(connection)
            active = get_active_acquisition_policy(connection)
            definition = get_qualification_definition_draft(connection)
    except psycopg.Error:
        return _unavailable_response()
    body = sidebar_page(
        "configuration",
        token,
        Section(
            Small("Search setup"),
            H1("Shape the search"),
            P(
                "Save and publish each section separately. Run a bounded test search before qualification becomes active."
            ),
            P(notice, role="status") if notice else None,
            _acquisition_panel(policy, active, token, acquisition_input),
            _qualification_panel(definition, token, qualification_input),
            Form(
                Input(type="hidden", name="csrf_token", value=token),
                Button("Continue to budget", type="submit", cls="button primary"),
                action="/configuration/continue",
                method="post",
            ),
            A("Operations", href="/operations"),
            A("Qualification targets", href="/configuration/qualification-targets"),
            cls="review-shell configuration-shell split-setup",
        ),
    )
    return HTMLResponse(document(body, title="Search setup"), status_code=status_code)


def _unavailable_response() -> HTMLResponse:
    return state_response(
        "Search setup is unavailable",
        "Reload after the database recovers, then check saved values before retrying.",
        action=A("Retry", href="/configuration", cls="retry"),
        status_code=503,
    )


def _uncertain_action_response(
    action: str, token: str, fields: tuple[tuple[str, str], ...]
) -> HTMLResponse:
    return state_response(
        "Search setup result is unknown",
        "The database disconnected. Retry this exact request after it recovers.",
        action=Form(
            Input(type="hidden", name="csrf_token", value=token),
            *(Input(type="hidden", name=name, value=value) for name, value in fields),
            Button("Retry this request", type="submit", cls="button primary"),
            action=action,
            method="post",
        ),
        status_code=503,
    )


def _user_error(error: ValueError) -> str:
    if isinstance(error, json.JSONDecodeError):
        return "Enter valid JSON for the criteria and profiles."
    if isinstance(error, ValidationError):
        first = error.errors()[0]
        return str(first["msg"]).removeprefix("Value error, ")
    return str(error)


def _acquisition_panel(
    draft: AcquisitionPolicyDraft,
    active: ActiveAcquisitionPolicy,
    token: str,
    submitted: tuple[str, tuple[str, ...]] | None,
) -> object:
    keywords = "\n".join(draft.policy.search_keywords) if submitted is None else submitted[0]
    sources = (
        tuple(source.value for source in draft.policy.enabled_sources)
        if submitted is None
        else submitted[1]
    )
    return Div(
        H2("Find jobs"),
        P("Keywords run in the order shown, once per selected source."),
        Form(
            Input(type="hidden", name="csrf_token", value=token),
            Input(type="hidden", name="draft_version", value=str(draft.version)),
            Label(
                "Keywords, one per line",
                Textarea(keywords, name="search_keywords", rows="7"),
                cls="keyword-list-field",
            ),
            Div(
                *(
                    Label(
                        Input(
                            type="checkbox",
                            name="enabled_sources",
                            value=source.value,
                            checked=source.value in sources,
                        ),
                        source.value.title(),
                        cls="source-choice",
                    )
                    for source in SupportedSearchSource
                ),
                cls="source-grid",
            ),
            Button("Save acquisition draft", type="submit", cls="button secondary"),
            action="/configuration/acquisition/draft",
            method="post",
            cls="split-config-form",
        ),
        _action_form(
            "/configuration/acquisition/publish", "Publish acquisition", draft.version, token
        ),
        _action_form(
            "/configuration/acquisition/activate",
            "Activate acquisition",
            active.generation,
            token,
            candidate_revision_id=draft.base_revision_id,
        ),
        P(f"Active generation {active.generation}. Draft version {draft.version}."),
        cls="editor-section split-config-panel",
    )


def _qualification_panel(
    draft: QualificationDefinitionDraft, token: str, submitted: tuple[str, str] | None
) -> object:
    definition = draft.definition
    criteria_json = (
        json.dumps([item.model_dump() for item in definition.personal_criteria], indent=2)
        if submitted is None
        else submitted[0]
    )
    profiles_json = (
        json.dumps([item.model_dump() for item in definition.target_profiles], indent=2)
        if submitted is None
        else submitted[1]
    )
    return Div(
        H2("Choose relevant jobs"),
        P("Review each rule before the first search. Open a section to edit its full definition."),
        Form(
            Input(type="hidden", name="csrf_token", value=token),
            Input(type="hidden", name="draft_version", value=str(draft.version)),
            Details(
                Summary(
                    "Personal criteria: "
                    + ", ".join(item.name for item in definition.personal_criteria)
                ),
                Label(
                    "Criteria JSON",
                    Textarea(
                        criteria_json,
                        name="personal_criteria",
                        rows="18",
                    ),
                ),
                cls="split-definition-editor",
                open=submitted is not None,
            ),
            Details(
                Summary(
                    "Target profiles: "
                    + ", ".join(item.name for item in definition.target_profiles)
                ),
                Label(
                    "Profiles JSON",
                    Textarea(
                        profiles_json,
                        name="target_profiles",
                        rows="18",
                    ),
                ),
                cls="split-definition-editor",
                open=submitted is not None,
            ),
            Button("Save qualification draft", type="submit", cls="button secondary"),
            action="/configuration/qualification/draft",
            method="post",
            cls="split-config-form",
        ),
        _action_form(
            "/configuration/qualification/publish", "Publish qualification", draft.version, token
        ),
        P(f"Published base {draft.base_revision_id[:12]}. Draft version {draft.version}."),
        cls="editor-section split-config-panel",
    )


def _action_form(
    action: str,
    label: str,
    version: int,
    token: str,
    *,
    candidate_revision_id: AcquisitionPolicyRevisionId | None = None,
) -> object:
    key = "active_generation" if action.endswith("activate") else "draft_version"
    return Form(
        Input(type="hidden", name="csrf_token", value=token),
        Input(type="hidden", name="idempotency_key", value=secrets.token_urlsafe(24)),
        Input(type="hidden", name=key, value=str(version)),
        Input(type="hidden", name="candidate_revision_id", value=candidate_revision_id)
        if candidate_revision_id is not None
        else None,
        Button(label, type="submit", cls="button secondary"),
        action=action,
        method="post",
        cls="split-config-action",
    )
