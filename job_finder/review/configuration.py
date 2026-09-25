# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUntypedFunctionDecorator=false, reportUnusedFunction=false, reportMissingTypeStubs=false
from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime

import psycopg
from fasthtml.common import (
    A,
    Button,
    Details,
    Div,
    FastHTML,
    Fieldset,
    Form,
    H1,
    H2,
    H3,
    Input,
    Label,
    Legend,
    Li,
    Nav,
    P,
    Pre,
    Request,
    Section,
    Small,
    Span,
    Strong,
    Summary,
    Textarea,
    Ul,
)
from pydantic import ValidationError
from starlette.datastructures import FormData
from starlette.responses import HTMLResponse, RedirectResponse

from job_finder.configuration_service import (
    ActivateConfigurationCommand,
    ActiveConfigurationChanged,
    ConfigurationActivated,
    ConfigurationInvalid,
    ConfigurationPublished,
    ConfigurationValidationIssue,
    DetailedConfigurationPreview,
    DraftSaved,
    PublishConfigurationCommand,
    PublishDraftChanged,
    SaveDraftCommand,
)
from job_finder.discovery.catalog import SupportedSearchSource
from job_finder.review.configuration_editor import (
    ConfigurationEditorService,
    ConfigurationEditorState,
)
from job_finder.review.onboarding import OnboardingProgressService
from job_finder.review.owner_access import OnboardingStage, OwnerAccessService
from job_finder.search_configuration import (
    SearchConfigurationDraft,
    SearchConfigurationRevisionId,
)
from job_finder.web.security import csrf_token, verified_csrf_token
from job_finder.web.shell import document, sidebar_page, state_response

_CONFIGURATION_NOTICES = {
    "draft-saved": "Draft saved. It is not published or active yet.",
    "published": "Saved draft published. Active search configuration is unchanged.",
    "publication-replayed": "Publication confirmed from the original request. Active search configuration is unchanged.",
    "activated": "Published draft activated.",
}
_SOURCE_LABELS = {source.value: source.value.title() for source in SupportedSearchSource}
_SUPPORTED_SOURCES = tuple(_SOURCE_LABELS)


@dataclass(frozen=True)
class _RawNamedRow:
    key: str
    name: str
    instructions: str


@dataclass(frozen=True)
class _RawConfigurationForm:
    search_keywords: tuple[str, ...]
    enabled_sources: tuple[str, ...]
    personal_criteria: tuple[_RawNamedRow, ...]
    target_profiles: tuple[_RawNamedRow, ...]
    expected_draft_version: str

    @classmethod
    def from_draft(cls, draft: SearchConfigurationDraft) -> _RawConfigurationForm:
        configuration = draft.configuration
        return cls(
            search_keywords=configuration.search_keywords,
            enabled_sources=tuple(source.value for source in configuration.enabled_sources),
            personal_criteria=tuple(
                _RawNamedRow(item.key, item.name, item.instructions)
                for item in configuration.personal_criteria
            ),
            target_profiles=tuple(
                _RawNamedRow(item.key, item.name, item.instructions)
                for item in configuration.target_profiles
            ),
            expected_draft_version=str(draft.version),
        )

    def candidate(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "search_keywords": self.search_keywords,
            "enabled_sources": self.enabled_sources,
            "personal_criteria": tuple(
                {"key": row.key, "name": row.name, "instructions": row.instructions}
                for row in self.personal_criteria
            ),
            "target_profiles": tuple(
                {"key": row.key, "name": row.name, "instructions": row.instructions}
                for row in self.target_profiles
            ),
        }


class _MalformedConfigurationForm(ValueError):
    pass


def register_configuration_routes(
    app: FastHTML,
    *,
    configuration_service: ConfigurationEditorService,
    owner_access_service: OwnerAccessService,
    onboarding_progress_service: OnboardingProgressService | None,
    actor: str,
    now: Callable[[], datetime],
) -> None:
    @app.route(
        "/configuration",
        methods=["GET"],
        name="create_review_app_configuration",
    )
    def _configuration(request: Request) -> HTMLResponse:
        token = csrf_token(request)
        if token is None:
            return HTMLResponse(status_code=401)
        try:
            state = configuration_service.inspect()
        except psycopg.Error:
            return _configuration_unavailable_response()
        notice = _CONFIGURATION_NOTICES.get(request.query_params.get("notice", ""))
        return HTMLResponse(
            document(
                _configuration_page(
                    state,
                    _RawConfigurationForm.from_draft(state.draft),
                    token,
                    publication_key=secrets.token_urlsafe(32),
                    notice=notice,
                ),
                title="Search setup",
            )
        )

    @app.route(
        "/configuration/edit",
        methods=["POST"],
        name="create_review_app_edit_configuration",
    )
    async def _edit_configuration(request: Request) -> HTMLResponse:
        form = await request.form()
        token = verified_csrf_token(request, form)
        if token is None:
            return _configuration_forbidden_response()
        try:
            action = _required_form_text(form, "action")
            changed = _apply_configuration_edit(form, action)
            state = configuration_service.inspect()
        except _MalformedConfigurationForm as error:
            return _malformed_configuration_response(str(error))
        except psycopg.Error:
            return _configuration_unavailable_response()
        return HTMLResponse(
            document(
                _configuration_page(
                    state,
                    changed,
                    token,
                    publication_key=secrets.token_urlsafe(32),
                    expanded=_affected_rows(action, changed),
                ),
                title="Search setup",
            )
        )

    @app.route(
        "/configuration/preview",
        methods=["POST"],
        name="create_review_app_preview_configuration",
    )
    async def _preview_configuration(request: Request) -> HTMLResponse:
        form = await request.form()
        token = verified_csrf_token(request, form)
        if token is None:
            return _configuration_forbidden_response()
        try:
            raw = _parse_configuration_form(form)
            validation = configuration_service.validate(raw.candidate())
            state = configuration_service.inspect()
        except _MalformedConfigurationForm as error:
            return _malformed_configuration_response(str(error))
        except psycopg.Error:
            return _configuration_unavailable_response()
        if isinstance(validation, ConfigurationInvalid):
            return HTMLResponse(
                document(
                    _configuration_page(
                        state,
                        raw,
                        token,
                        publication_key=secrets.token_urlsafe(32),
                        validation=validation,
                    ),
                    title="Search setup",
                ),
                status_code=422,
            )
        detailed = configuration_service.preview(validation.configuration)
        return HTMLResponse(
            document(
                _configuration_page(
                    state,
                    raw,
                    token,
                    publication_key=secrets.token_urlsafe(32),
                    preview=detailed,
                ),
                title="Search setup preview",
            )
        )

    @app.route(
        "/configuration/draft",
        methods=["POST"],
        name="create_review_app_save_configuration",
    )
    async def _save_configuration(request: Request) -> HTMLResponse | RedirectResponse:
        form = await request.form()
        token = verified_csrf_token(request, form)
        if token is None:
            return _configuration_forbidden_response()
        try:
            raw = _parse_configuration_form(form)
            expected_version = _form_integer(raw.expected_draft_version, "draft version")
            validation = configuration_service.validate(raw.candidate())
        except _MalformedConfigurationForm as error:
            return _malformed_configuration_response(str(error))
        if isinstance(validation, ConfigurationInvalid):
            try:
                state = configuration_service.inspect()
            except psycopg.Error:
                return _configuration_unavailable_response()
            return HTMLResponse(
                document(
                    _configuration_page(
                        state,
                        raw,
                        token,
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
            document(
                _configuration_page(
                    state,
                    rebound,
                    token,
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

    @app.route(
        "/configuration/publish",
        methods=["POST"],
        name="create_review_app_publish_configuration",
    )
    async def _publish_configuration(request: Request) -> HTMLResponse | RedirectResponse:
        form = await request.form()
        token = verified_csrf_token(request, form)
        if token is None:
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
        except (_MalformedConfigurationForm, ValidationError) as error:
            return _malformed_configuration_response(str(error))
        try:
            result = configuration_service.publish(command)
        except psycopg.Error:
            return HTMLResponse(
                document(
                    _publication_retry_page(
                        token,
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
        return _configuration_conflict_page(configuration_service, token, message)

    @app.route(
        "/configuration/activate",
        methods=["POST"],
        name="create_review_app_activate_configuration",
    )
    async def _activate_configuration(request: Request) -> HTMLResponse | RedirectResponse:
        form = await request.form()
        token = verified_csrf_token(request, form)
        if token is None:
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
        except (_MalformedConfigurationForm, ValidationError) as error:
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
        return _configuration_conflict_page(configuration_service, token, message)


def _parse_configuration_form(
    form: FormData, *, source_remove_index: int | None = None
) -> _RawConfigurationForm:
    keyword_text = _single_text(form, "search_keywords")
    return _RawConfigurationForm(
        search_keywords=tuple(keyword_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")),
        enabled_sources=_source_values(form, remove_index=source_remove_index),
        personal_criteria=_named_rows(form, "personal_criteria", "criterion_count"),
        target_profiles=_named_rows(form, "target_profiles", "profile_count"),
        expected_draft_version=_single_text(form, "expected_draft_version"),
    )


def _apply_configuration_edit(form: FormData, action: str) -> _RawConfigurationForm:
    parts = action.split(".")
    if parts[:2] == ["source", "remove"]:
        if len(parts) != 3:
            raise _MalformedConfigurationForm("Unknown editor action")
        try:
            index = int(parts[2])
        except ValueError as error:
            raise _MalformedConfigurationForm("Invalid editor row") from error
        return _parse_configuration_form(form, source_remove_index=index)
    return _transform_rows(_parse_configuration_form(form), action)


def _transform_rows(raw: _RawConfigurationForm, action: str) -> _RawConfigurationForm:
    parts = action.split(".")
    if action in {"criterion.add", "profile.add"}:
        kind = action.removesuffix(".add")
        empty = _RawNamedRow("", "", "")
        if kind == "criterion":
            return replace(raw, personal_criteria=(*raw.personal_criteria, empty))
        return replace(raw, target_profiles=(*raw.target_profiles, empty))
    if len(parts) != 3 or parts[0] not in {"criterion", "profile"}:
        raise _MalformedConfigurationForm("Unknown editor action")
    kind = parts[0]
    direction = parts[1]
    if direction not in {"up", "down", "remove"}:
        raise _MalformedConfigurationForm("Unknown editor action")
    try:
        index = int(parts[2])
    except ValueError as error:
        raise _MalformedConfigurationForm("Invalid editor row") from error
    if kind == "criterion":
        return replace(raw, personal_criteria=_transform(raw.personal_criteria, index, direction))
    return replace(raw, target_profiles=_transform(raw.target_profiles, index, direction))


def _configuration_page(
    state: ConfigurationEditorState,
    raw: _RawConfigurationForm,
    csrf_token: str,
    *,
    publication_key: str,
    validation: ConfigurationInvalid | None = None,
    preview: DetailedConfigurationPreview | None = None,
    notice: str | None = None,
    alert: str | None = None,
    expanded: frozenset[tuple[str, int]] | None = None,
) -> object:
    expanded_rows = expanded or frozenset()
    issues = () if validation is None else validation.issues
    dirty = raw != _RawConfigurationForm.from_draft(state.draft)
    return sidebar_page(
        "configuration",
        csrf_token,
        Section(
            Div(
                Small("Search setup", cls="eyebrow"),
                H1("Shape the search, then publish on purpose."),
                P(
                    "Edit and preview freely. Saving updates the shared draft. "
                    + "Publishing freezes that saved draft, and activation is a separate choice.",
                    cls="configuration-intro",
                ),
                cls="configuration-header",
            ),
            _notice(notice) if notice else None,
            P(alert, cls="configuration-alert", role="alert") if alert else None,
            _validation_alert(validation) if validation else None,
            P(
                "Unsaved browser changes are shown below. Release actions still apply to the saved draft.",
                cls="configuration-alert unsaved-state",
                role="status",
            )
            if dirty
            else None,
            _state_strip(state, dirty=dirty),
            Div(
                _section_index(),
                Form(
                    Input(type="hidden", name="csrf_token", value=csrf_token),
                    Input(
                        type="hidden",
                        name="expected_draft_version",
                        value=raw.expected_draft_version,
                    ),
                    _keywords_editor(raw, issues),
                    _sources_editor(raw, issues),
                    _named_editor(
                        "Personal criteria",
                        "criteria",
                        "personal_criteria",
                        "criterion",
                        raw.personal_criteria,
                        issues,
                        expanded_rows,
                    ),
                    _named_editor(
                        "Target profiles",
                        "profiles",
                        "target_profiles",
                        "profile",
                        raw.target_profiles,
                        issues,
                        expanded_rows,
                    ),
                    Div(
                        Button(
                            "Preview unsaved values",
                            type="submit",
                            formaction="/configuration/preview",
                            cls="button secondary",
                        ),
                        Button(
                            "Save draft",
                            type="submit",
                            formaction="/configuration/draft",
                            cls="button primary",
                        ),
                        cls="editor-actions",
                    ),
                    action="/configuration/edit",
                    method="post",
                    cls="configuration-form",
                ),
                cls="configuration-layout",
            ),
            _preview(preview) if preview else None,
            _release_actions(state, csrf_token, publication_key, dirty=dirty),
            id="configuration",
            cls="review-shell configuration-shell",
        ),
    )


def _publication_retry_page(
    csrf_token: str,
    *,
    publication_key: str,
    expected_draft_version: str,
    expected_revision_id: str,
) -> object:
    return sidebar_page(
        "configuration",
        csrf_token,
        Section(
            Div(
                Small("Search setup", cls="eyebrow"),
                H1("Publication result unknown"),
                P(
                    "The database did not confirm whether it received this publication. "
                    + "Retry this exact request. Its private idempotency key is unchanged, so a completed "
                    + "publication will be replayed rather than duplicated.",
                    role="alert",
                ),
                Form(
                    Input(type="hidden", name="csrf_token", value=csrf_token),
                    Input(type="hidden", name="idempotency_key", value=publication_key),
                    Input(
                        type="hidden",
                        name="expected_draft_version",
                        value=expected_draft_version,
                    ),
                    Input(
                        type="hidden",
                        name="expected_configuration_revision_id",
                        value=expected_revision_id,
                    ),
                    Button("Retry exact publication", type="submit", cls="button primary"),
                    action="/configuration/publish",
                    method="post",
                ),
                A("Return to search setup", href="/configuration", cls="retry"),
                cls="state",
            ),
            cls="review-shell state-shell",
        ),
    )


def _state_strip(state: ConfigurationEditorState, *, dirty: bool) -> object:
    publication = state.publication
    active = state.active.active
    return Div(
        Div(
            Small("Draft version"),
            Strong(
                f"{state.draft.version} · unsaved browser edits"
                if dirty
                else str(state.draft.version)
            ),
        ),
        Div(Small("Draft base"), Span(state.draft.base_revision_id, cls="mono")),
        Div(Small("Current content"), Span(state.content_revision_id, cls="mono")),
        Div(
            Small("Saved draft publication"),
            Strong("Published" if publication else "Not published"),
        ),
        Div(Small("Active generation"), Strong(str(active.generation))),
        Div(
            Small("Active revision"),
            Span(active.revision.id, cls="mono"),
        ),
        cls="configuration-state",
        aria_label="Saved configuration state",
    )


def _section_index() -> object:
    return Nav(
        Strong("On this page"),
        A("Search keywords", href="#keywords"),
        A("Sources", href="#sources"),
        A("Personal criteria", href="#criteria"),
        A("Target profiles", href="#profiles"),
        A("Release", href="#release"),
        aria_label="Configuration sections",
        cls="section-index",
    )


def _keywords_editor(
    raw: _RawConfigurationForm,
    issues: tuple[ConfigurationValidationIssue, ...],
) -> object:
    issue_ids = _issue_ids(issues, ("search_keywords",))
    return Fieldset(
        Legend("Search keywords"),
        P(
            "Enter one keyword per line. Queries run in this exact top-to-bottom order.",
            cls="field-help",
        ),
        Label(
            "Ordered keywords",
            Textarea(
                "\n".join(raw.search_keywords),
                id="search_keywords",
                name="search_keywords",
                rows=str(min(max(len(raw.search_keywords), 6), 14)),
                aria_invalid="true" if issue_ids else None,
                aria_describedby=issue_ids or None,
            ),
            cls="keyword-list-field",
        ),
        *_field_issues(issues, "search_keywords"),
        id="keywords",
        cls="editor-section",
    )


def _sources_editor(
    raw: _RawConfigurationForm,
    issues: tuple[ConfigurationValidationIssue, ...],
) -> object:
    issue_ids = _issue_ids(issues, ("enabled_sources",))
    selected = set(raw.enabled_sources)
    seen: set[str] = set()
    invalid_rows: list[tuple[int, str]] = []
    order_controls: list[object] = []
    for index, value in enumerate(raw.enabled_sources):
        if value in _SOURCE_LABELS and value not in seen:
            order_controls.append(Input(type="hidden", name=f"source_order.{index}", value=value))
            seen.add(value)
        else:
            invalid_rows.append((index, value))
    return Fieldset(
        Legend("Supported sources"),
        P("Choose at least one supported applicant tracking system.", cls="field-help"),
        Input(type="hidden", name="source_count", value=str(len(raw.enabled_sources))),
        *order_controls,
        Div(
            *(
                Label(
                    Input(
                        type="checkbox",
                        name="source_selected",
                        value=value,
                        checked=value in selected,
                        aria_invalid="true" if issue_ids else None,
                        aria_describedby=issue_ids or None,
                    ),
                    _SOURCE_LABELS[value],
                    cls="source-choice",
                )
                for value in _SOURCE_LABELS
            ),
            cls="source-grid",
        ),
        Div(
            Strong("Submitted source values that need correction"),
            *(
                Div(
                    Input(type="hidden", name="source_preserve", value=str(index)),
                    Label(
                        f"Source value {index + 1}",
                        Input(
                            type="text",
                            name=f"source_order.{index}",
                            value=value,
                        ),
                    ),
                    Button(
                        "Remove",
                        name="action",
                        value=f"source.remove.{index}",
                        type="submit",
                        cls="remove",
                    ),
                    cls="invalid-source-row",
                )
                for index, value in invalid_rows
            ),
            cls="invalid-sources",
        )
        if invalid_rows
        else None,
        *_field_issues(issues, "enabled_sources"),
        id="sources",
        cls="editor-section",
    )


def _named_editor(
    title: str,
    section_id: str,
    prefix: str,
    action_prefix: str,
    rows: tuple[_RawNamedRow, ...],
    issues: tuple[ConfigurationValidationIssue, ...],
    expanded: frozenset[tuple[str, int]],
) -> object:
    count_key = "criterion_count" if action_prefix == "criterion" else "profile_count"
    has_collection_issue = any(tuple(issue.location) == (prefix,) for issue in issues)
    duplicate_indexes = {
        index for index, row in enumerate(rows) if sum(other.key == row.key for other in rows) > 1
    }
    collection_issue_indexes = (
        duplicate_indexes or set(range(len(rows))) if has_collection_issue else set()
    )
    collection_issue_ids = " ".join(
        f"configuration-issue-{index}"
        for index, issue in enumerate(issues)
        if tuple(issue.location) == (prefix,)
    )
    return Fieldset(
        Legend(title),
        P(
            "Order matters. Open a card to edit its identity and full instructions.",
            cls="field-help",
        ),
        Input(type="hidden", name=count_key, value=str(len(rows))),
        *(
            _named_card(
                prefix,
                action_prefix,
                index,
                row,
                len(rows),
                issues,
                open_card=(prefix, index) in expanded
                or _row_has_issue(issues, prefix, index)
                or index in collection_issue_indexes,
                collection_issue_ids=collection_issue_ids
                if index in collection_issue_indexes
                else "",
            )
            for index, row in enumerate(rows)
        ),
        Button(
            f"Add {action_prefix}",
            name="action",
            value=f"{action_prefix}.add",
            type="submit",
            cls="button add",
        ),
        *_field_issues(issues, prefix),
        id=section_id,
        cls="editor-section",
    )


def _named_card(
    prefix: str,
    action_prefix: str,
    index: int,
    row: _RawNamedRow,
    count: int,
    issues: tuple[ConfigurationValidationIssue, ...],
    *,
    open_card: bool,
    collection_issue_ids: str,
) -> object:
    label = row.name if row.name else f"New {action_prefix}"
    key_issues = " ".join(
        value
        for value in (
            _issue_ids(issues, (prefix, index, "key")),
            collection_issue_ids,
        )
        if value
    )
    name_issues = _issue_ids(issues, (prefix, index, "name"))
    instruction_issues = _issue_ids(issues, (prefix, index, "instructions"))
    return Details(
        Summary(
            Span(f"{index + 1:02d}", cls="row-number"),
            Strong(label),
            Span(row.key or "Key needed", cls="row-key"),
        ),
        Div(
            Label(
                "Key",
                Input(
                    type="text",
                    id=f"{prefix}-{index}-key",
                    name=f"{prefix}.{index}.key",
                    value=row.key,
                    aria_invalid="true" if key_issues else None,
                    aria_describedby=key_issues or None,
                ),
            ),
            Label(
                "Name",
                Input(
                    type="text",
                    id=f"{prefix}-{index}-name",
                    name=f"{prefix}.{index}.name",
                    value=row.name,
                    aria_invalid="true" if name_issues else None,
                    aria_describedby=name_issues or None,
                ),
            ),
            Label(
                "Instructions",
                Textarea(
                    row.instructions,
                    id=f"{prefix}-{index}-instructions",
                    name=f"{prefix}.{index}.instructions",
                    rows="12",
                    aria_invalid="true" if instruction_issues else None,
                    aria_describedby=instruction_issues or None,
                ),
                cls="instructions-field",
            ),
            _row_buttons(action_prefix, index, count),
            cls="named-card-body",
        ),
        open=open_card,
        cls="named-card affected" if open_card else "named-card",
    )


def _row_buttons(kind: str, index: int, count: int) -> object:
    return Div(
        Button(
            "Up",
            name="action",
            value=f"{kind}.up.{index}",
            type="submit",
            disabled=index == 0,
            aria_label=f"Move {kind} {index + 1} up",
        ),
        Button(
            "Down",
            name="action",
            value=f"{kind}.down.{index}",
            type="submit",
            disabled=index + 1 == count,
            aria_label=f"Move {kind} {index + 1} down",
        ),
        Button(
            "Remove",
            name="action",
            value=f"{kind}.remove.{index}",
            type="submit",
            aria_label=f"Remove {kind} {index + 1}",
            cls="remove",
        ),
        cls="row-actions",
    )


def _validation_alert(validation: ConfigurationInvalid) -> object:
    return Div(
        Strong(f"Fix {validation.total_issue_count} validation issue(s)."),
        Ul(*(Li(A(issue.message, href=_issue_href(issue))) for issue in validation.issues)),
        P(f"{validation.omitted_issue_count} more issue(s) are not shown.")
        if validation.omitted_issue_count
        else None,
        role="alert",
        cls="validation-alert",
    )


def _field_issues(
    issues: tuple[ConfigurationValidationIssue, ...], field: str
) -> tuple[object, ...]:
    return tuple(
        P(issue.message, id=f"configuration-issue-{index}", cls="field-error")
        for index, issue in enumerate(issues)
        if issue.location and issue.location[0] == field
    )


def _issue_ids(
    issues: tuple[ConfigurationValidationIssue, ...],
    location: tuple[str | int, ...],
) -> str:
    return " ".join(
        f"configuration-issue-{index}"
        for index, issue in enumerate(issues)
        if tuple(issue.location) == location
        or (len(location) == 1 and tuple(issue.location[:1]) == location)
    )


def _issue_href(issue: ConfigurationValidationIssue) -> str:
    if not issue.location:
        return "#configuration"
    field = issue.location[0]
    section = {
        "search_keywords": "keywords",
        "enabled_sources": "sources",
        "personal_criteria": "criteria",
        "target_profiles": "profiles",
    }.get(str(field), "configuration")
    if len(issue.location) >= 3 and isinstance(issue.location[1], int):
        return f"#{field}-{issue.location[1]}-{issue.location[2]}"
    return f"#{section}"


def _row_has_issue(
    issues: tuple[ConfigurationValidationIssue, ...], prefix: str, index: int
) -> bool:
    return any(
        len(issue.location) > 1 and issue.location[0] == prefix and issue.location[1] == index
        for issue in issues
    )


def _preview(preview: DetailedConfigurationPreview) -> object:
    summary = preview.summary
    return Section(
        Small("Unsaved preview", cls="eyebrow"),
        H2("What this configuration will produce"),
        Div(
            Div(Small("Searches"), Strong(str(summary.total_generated_search_count))),
            Div(Small("Compiled prompts"), Strong(str(summary.total_compiled_prompt_count))),
            Div(Small("Content revision"), Span(summary.configuration_revision_id, cls="mono")),
            cls="preview-counts",
        ),
        H3("Query samples"),
        Ul(*(Li(sample) for sample in summary.search_samples), cls="sample-list"),
        H3("Prompt summary"),
        Ul(
            *(
                Li(
                    Strong(item.name),
                    Span(f"{item.phase} / {item.criterion}"),
                )
                for item in summary.prompt_summaries
            ),
            cls="prompt-summary",
        ),
        Details(
            Summary("Advanced: compiled system messages"),
            *(
                Div(
                    H3(version.definition.name),
                    Pre(_system_message(version.messages)),
                    cls="compiled-prompt",
                )
                for version in preview.prompt_release.versions
            ),
            cls="advanced-preview",
        ),
        id="preview",
        cls="configuration-preview",
    )


def _system_message(messages: tuple[dict[str, str], ...]) -> str:
    return next((message["content"] for message in messages if message["role"] == "system"), "")


def _release_actions(
    state: ConfigurationEditorState,
    csrf_token: str,
    publication_key: str,
    *,
    dirty: bool,
) -> object:
    publication = state.publication
    active = state.active.active
    is_active = publication is not None and publication.revision_id == active.revision.id
    if dirty:
        controls: object = P(
            "Save or discard the browser entries before using release actions.",
            cls="configuration-alert",
            role="status",
        )
    elif publication is None:
        controls = Form(
            Input(type="hidden", name="csrf_token", value=csrf_token),
            Input(type="hidden", name="idempotency_key", value=publication_key),
            Input(
                type="hidden",
                name="expected_draft_version",
                value=str(state.draft.version),
            ),
            Input(
                type="hidden",
                name="expected_configuration_revision_id",
                value=state.content_revision_id,
            ),
            Button("Publish saved draft", type="submit", cls="button primary"),
            action="/configuration/publish",
            method="post",
        )
    else:
        controls = Div(
            P("This exact saved draft is published.", cls="published-state"),
            Form(
                Input(type="hidden", name="csrf_token", value=csrf_token),
                Input(
                    type="hidden",
                    name="target_revision_id",
                    value=publication.revision_id,
                ),
                Input(
                    type="hidden",
                    name="expected_active_revision_id",
                    value=active.revision.id,
                ),
                Input(
                    type="hidden",
                    name="expected_generation",
                    value=str(active.generation),
                ),
                Button("Activate published draft", type="submit", cls="button primary"),
                action="/configuration/activate",
                method="post",
            )
            if not is_active
            else P("This published draft is active.", cls="active-state"),
        )
    return Section(
        Small("Release", cls="eyebrow"),
        H2("Publish saved work, then activate it"),
        P(
            "Publish freezes the exact saved draft, not unsaved browser entries. "
            + "It never changes the active search configuration."
        ),
        controls,
        id="release",
        cls="release-panel",
    )


def _notice(message: str) -> object:
    return P(message, cls="notice", role="status")


def _transform[T](rows: tuple[T, ...], index: int, direction: str) -> tuple[T, ...]:
    if index < 0 or index >= len(rows):
        raise _MalformedConfigurationForm("Invalid editor row")
    values = list(rows)
    if direction == "remove":
        del values[index]
    elif direction == "up" and index > 0:
        values[index - 1], values[index] = values[index], values[index - 1]
    elif direction == "down" and index + 1 < len(values):
        values[index + 1], values[index] = values[index], values[index + 1]
    return tuple(values)


def _indexed_values(form: FormData, prefix: str, count_key: str) -> tuple[str, ...]:
    count = _count(form, count_key)
    return tuple(_single_text(form, f"{prefix}.{index}") for index in range(count))


def _named_rows(form: FormData, prefix: str, count_key: str) -> tuple[_RawNamedRow, ...]:
    count = _count(form, count_key)
    return tuple(
        _RawNamedRow(
            key=_single_text(form, f"{prefix}.{index}.key"),
            name=_single_text(form, f"{prefix}.{index}.name"),
            instructions=_single_text(form, f"{prefix}.{index}.instructions"),
        )
        for index in range(count)
    )


def _source_values(form: FormData, *, remove_index: int | None = None) -> tuple[str, ...]:
    ordered = _indexed_values(form, "source_order", "source_count")
    if remove_index is not None and (remove_index < 0 or remove_index >= len(ordered)):
        raise _MalformedConfigurationForm("Invalid editor row")
    selected = tuple(_text_values(form, "source_selected"))
    preserved_indexes: set[int] = set()
    for raw_index in _text_values(form, "source_preserve"):
        try:
            index = int(raw_index)
        except ValueError as error:
            raise _MalformedConfigurationForm("Invalid source_preserve value") from error
        if index < 0 or index >= len(ordered):
            raise _MalformedConfigurationForm("Invalid source_preserve value")
        preserved_indexes.add(index)
    values = tuple(
        value
        for index, value in enumerate(ordered)
        if index != remove_index
        and (index in preserved_indexes or value not in _SUPPORTED_SOURCES or value in selected)
    )
    return (*values, *(value for value in selected if value not in ordered))


def _count(form: FormData, key: str) -> int:
    raw = _single_text(form, key)
    try:
        count = int(raw)
    except ValueError as error:
        raise _MalformedConfigurationForm(f"Invalid {key}") from error
    if count < 0 or count > 100:
        raise _MalformedConfigurationForm(f"Invalid {key}")
    return count


def _single_text(form: FormData, key: str) -> str:
    values = _text_values(form, key)
    if len(values) != 1:
        raise _MalformedConfigurationForm(f"Expected one {key} value")
    return values[0]


def _text_values(form: FormData, key: str) -> list[str]:
    values = form.getlist(key)
    if any(not isinstance(value, str) for value in values):
        raise _MalformedConfigurationForm(f"Invalid {key} value")
    return [value for value in values if isinstance(value, str)]


def _configuration_unavailable_response() -> HTMLResponse:
    return state_response(
        "Search setup is unavailable",
        "The database could not complete this request. Saved configuration was not overwritten.",
        action=A("Retry", href="/configuration", cls="retry"),
        status_code=503,
    )


def _configuration_forbidden_response() -> HTMLResponse:
    return state_response(
        "This configuration form expired",
        "Reload search setup and try again.",
        action=A("Reload search setup", href="/configuration", cls="retry"),
        status_code=403,
    )


def _malformed_configuration_response(detail: str) -> HTMLResponse:
    return state_response(
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
        document(
            _configuration_page(
                state,
                _RawConfigurationForm.from_draft(state.draft),
                csrf_token,
                publication_key=secrets.token_urlsafe(32),
                alert=alert,
            ),
            title="Search setup conflict",
        ),
        status_code=409,
    )


def _required_form_text(form: FormData, key: str) -> str:
    values = form.getlist(key)
    if len(values) != 1 or not isinstance(values[0], str):
        raise _MalformedConfigurationForm(f"Expected one {key} value")
    return values[0]


def _form_integer(value: str, label: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise _MalformedConfigurationForm(f"Invalid {label}") from error
    if parsed < 0:
        raise _MalformedConfigurationForm(f"Invalid {label}")
    return parsed


def _affected_rows(action: str, raw: _RawConfigurationForm) -> frozenset[tuple[str, int]]:
    if action == "criterion.add":
        return frozenset({("personal_criteria", len(raw.personal_criteria) - 1)})
    if action == "profile.add":
        return frozenset({("target_profiles", len(raw.target_profiles) - 1)})
    return frozenset()
