# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportMissingTypeStubs=false
from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from typing import Literal

from fasthtml.common import (
    A,
    Button,
    Details,
    Div,
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
    Section,
    Small,
    Span,
    Strong,
    Summary,
    Textarea,
    Ul,
)
from starlette.datastructures import FormData

from job_finder.configuration_service import (
    ActivateConfigurationCommand,
    ActivateConfigurationResult,
    ConfigurationInvalid,
    ConfigurationValidationIssue,
    ConfigurationRevisionDetails,
    ConfigurationRevisionNotFound,
    ConfigurationValidationResult,
    DetailedConfigurationPreview,
    DraftSaveResult,
    PublishConfigurationCommand,
    PublishConfigurationResult,
    PublishedActiveSearchConfiguration,
    SaveDraftCommand,
    activate_search_configuration,
    get_active_search_configuration,
    get_search_configuration_draft,
    get_search_configuration_revision,
    preview_search_configuration_detailed,
    publish_search_configuration,
    save_search_configuration_draft,
    validate_search_configuration,
)
from job_finder.search_configuration import (
    Connection,
    SearchConfiguration,
    SearchConfigurationDraft,
    SearchConfigurationPublication,
    SearchConfigurationRevisionId,
    search_configuration_revision_id,
)

ConnectionFactory = Callable[[], AbstractContextManager[Connection]]
_SUPPORTED_SOURCES = ("ashby", "lever", "greenhouse", "workable")


@dataclass(frozen=True)
class RawNamedRow:
    key: str
    name: str
    instructions: str


@dataclass(frozen=True)
class RawConfigurationForm:
    search_keywords: tuple[str, ...]
    enabled_sources: tuple[str, ...]
    personal_criteria: tuple[RawNamedRow, ...]
    target_profiles: tuple[RawNamedRow, ...]
    expected_draft_version: str

    @classmethod
    def from_draft(cls, draft: SearchConfigurationDraft) -> RawConfigurationForm:
        configuration = draft.configuration
        return cls(
            search_keywords=configuration.search_keywords,
            enabled_sources=tuple(source.value for source in configuration.enabled_sources),
            personal_criteria=tuple(
                RawNamedRow(item.key, item.name, item.instructions)
                for item in configuration.personal_criteria
            ),
            target_profiles=tuple(
                RawNamedRow(item.key, item.name, item.instructions)
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


@dataclass(frozen=True)
class ConfigurationEditorState:
    draft: SearchConfigurationDraft
    active: PublishedActiveSearchConfiguration
    content_revision_id: SearchConfigurationRevisionId
    saved_revision: ConfigurationRevisionDetails | None

    @property
    def publication(self) -> SearchConfigurationPublication | None:
        return None if self.saved_revision is None else self.saved_revision.publication


@dataclass(frozen=True)
class ConfigurationEditorService:
    inspect: Callable[[], ConfigurationEditorState]
    validate: Callable[[object], ConfigurationValidationResult]
    preview: Callable[[SearchConfiguration], DetailedConfigurationPreview]
    save: Callable[[SaveDraftCommand], DraftSaveResult]
    publish: Callable[[PublishConfigurationCommand], PublishConfigurationResult]
    activate: Callable[[ActivateConfigurationCommand], ActivateConfigurationResult]


class MalformedConfigurationForm(ValueError):
    pass


def postgres_configuration_editor_service(connect: ConnectionFactory) -> ConfigurationEditorService:
    def inspect() -> ConfigurationEditorState:
        with connect() as connection:
            draft = get_search_configuration_draft(connection)
            active = get_active_search_configuration(connection)
            revision_id = search_configuration_revision_id(draft.configuration)
            try:
                revision = get_search_configuration_revision(connection, revision_id)
            except ConfigurationRevisionNotFound:
                revision = None
            return ConfigurationEditorState(
                draft=draft,
                active=active,
                content_revision_id=revision_id,
                saved_revision=revision,
            )

    def save(command: SaveDraftCommand) -> DraftSaveResult:
        with connect() as connection:
            return save_search_configuration_draft(connection, command)

    def publish(command: PublishConfigurationCommand) -> PublishConfigurationResult:
        with connect() as connection:
            return publish_search_configuration(connection, command)

    def activate(command: ActivateConfigurationCommand) -> ActivateConfigurationResult:
        with connect() as connection:
            return activate_search_configuration(connection, command)

    return ConfigurationEditorService(
        inspect=inspect,
        validate=validate_search_configuration,
        preview=preview_search_configuration_detailed,
        save=save,
        publish=publish,
        activate=activate,
    )


def parse_configuration_form(
    form: FormData, *, source_remove_index: int | None = None
) -> RawConfigurationForm:
    keyword_text = _single_text(form, "search_keywords")
    return RawConfigurationForm(
        search_keywords=tuple(keyword_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")),
        enabled_sources=_source_values(form, remove_index=source_remove_index),
        personal_criteria=_named_rows(form, "personal_criteria", "criterion_count"),
        target_profiles=_named_rows(form, "target_profiles", "profile_count"),
        expected_draft_version=_single_text(form, "expected_draft_version"),
    )


def apply_configuration_edit(form: FormData, action: str) -> RawConfigurationForm:
    parts = action.split(".")
    if parts[:2] == ["source", "remove"]:
        if len(parts) != 3:
            raise MalformedConfigurationForm("Unknown editor action")
        try:
            index = int(parts[2])
        except ValueError as error:
            raise MalformedConfigurationForm("Invalid editor row") from error
        return parse_configuration_form(form, source_remove_index=index)
    return transform_rows(parse_configuration_form(form), action)


def transform_rows(raw: RawConfigurationForm, action: str) -> RawConfigurationForm:
    parts = action.split(".")
    if action in {"criterion.add", "profile.add"}:
        kind = action.removesuffix(".add")
        empty = RawNamedRow("", "", "")
        if kind == "criterion":
            return replace(raw, personal_criteria=(*raw.personal_criteria, empty))
        return replace(raw, target_profiles=(*raw.target_profiles, empty))
    if len(parts) != 3 or parts[0] not in {"criterion", "profile"}:
        raise MalformedConfigurationForm("Unknown editor action")
    kind = parts[0]
    direction = parts[1]
    if direction not in {"up", "down", "remove"}:
        raise MalformedConfigurationForm("Unknown editor action")
    try:
        index = int(parts[2])
    except ValueError as error:
        raise MalformedConfigurationForm("Invalid editor row") from error
    if kind == "criterion":
        return replace(raw, personal_criteria=_transform(raw.personal_criteria, index, direction))
    return replace(raw, target_profiles=_transform(raw.target_profiles, index, direction))


def authenticated_masthead(
    csrf_token: str, *, current: Literal["review", "configuration"]
) -> object:
    return Div(
        Div(Strong("JF", cls="wordmark"), Small("Owner workbench", cls="masthead-label")),
        Nav(
            A("Review", href="/review", aria_current="page" if current == "review" else None),
            A(
                "Search setup",
                href="/configuration",
                aria_current="page" if current == "configuration" else None,
            ),
            aria_label="Owner workbench",
            cls="masthead-nav",
        ),
        Form(
            Input(type="hidden", name="csrf_token", value=csrf_token),
            Button("Sign out", type="submit", cls="logout"),
            action="/logout",
            method="post",
        ),
        cls="masthead",
    )


def configuration_page(
    state: ConfigurationEditorState,
    raw: RawConfigurationForm,
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
    dirty = raw != RawConfigurationForm.from_draft(state.draft)
    return Section(
        authenticated_masthead(csrf_token, current="configuration"),
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
    )


def publication_retry_page(
    csrf_token: str,
    *,
    publication_key: str,
    expected_draft_version: str,
    expected_revision_id: str,
) -> object:
    return Section(
        authenticated_masthead(csrf_token, current="configuration"),
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
    raw: RawConfigurationForm,
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
    raw: RawConfigurationForm,
    issues: tuple[ConfigurationValidationIssue, ...],
) -> object:
    labels = {
        "ashby": "Ashby",
        "lever": "Lever",
        "greenhouse": "Greenhouse",
        "workable": "Workable",
    }
    issue_ids = _issue_ids(issues, ("enabled_sources",))
    selected = set(raw.enabled_sources)
    seen: set[str] = set()
    invalid_rows: list[tuple[int, str]] = []
    order_controls: list[object] = []
    for index, value in enumerate(raw.enabled_sources):
        if value in labels and value not in seen:
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
                    labels[value],
                    cls="source-choice",
                )
                for value in labels
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
    rows: tuple[RawNamedRow, ...],
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
    row: RawNamedRow,
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


CONFIGURATION_CSS = """
.configuration-header { padding: clamp(2rem, 6vw, 5rem) 0 1.5rem; }
.configuration-header h1 { max-width: 17ch; }
.configuration-intro { max-width: 64ch; font-size: 1.08rem; line-height: 1.6; }
.configuration-layout { display: grid; grid-template-columns: 210px minmax(0, 1fr); gap: 1.5rem; align-items: start; }
.section-index { position: sticky; top: 1rem; display: grid; border: 2px solid var(--line); background: var(--panel); }
.section-index strong, .section-index a { min-height: 44px; display: flex; align-items: center; padding: 0.6rem 0.75rem; border-bottom: 2px solid var(--line); }
.section-index a:last-child { border-bottom: 0; }
.section-index a:hover, .section-index a:focus-visible { background: var(--acid); color: var(--accent-ink); }
.configuration-state, .preview-counts { display: grid; grid-template-columns: repeat(3, 1fr); margin-bottom: 1.5rem; border: 2px solid var(--line); background: var(--panel); }
.configuration-state > div, .preview-counts > div { min-width: 0; padding: 0.75rem; border-right: 2px solid var(--line); border-bottom: 2px solid var(--line); }
.configuration-state > div:nth-child(3n), .preview-counts > div:last-child { border-right: 0; }
.configuration-state > div:nth-last-child(-n + 3), .preview-counts > div { border-bottom: 0; }
.configuration-state small, .configuration-state strong, .configuration-state span, .preview-counts small, .preview-counts strong, .preview-counts span { display: block; }
.configuration-state small, .preview-counts small { margin-bottom: 0.35rem; text-transform: uppercase; letter-spacing: 0.08em; font-weight: 900; }
.mono { overflow-wrap: anywhere; font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 0.76rem; }
.configuration-form { display: grid; gap: 1.5rem; }
.editor-section { min-width: 0; margin: 0; padding: clamp(1rem, 3vw, 1.6rem); border: 2px solid var(--line); background: var(--panel); box-shadow: 6px 6px 0 var(--shadow); }
.editor-section legend { padding: 0 0.45rem; font: 700 1.55rem Georgia, 'Times New Roman', serif; }
.field-help { margin-top: 0; color: var(--muted); }
.keyword-list-field, .named-card-body label { display: grid; gap: 0.35rem; font-weight: 800; }
.keyword-list-field textarea, .named-card input, .named-card textarea { width: 100%; min-height: 48px; padding: 0.7rem; border: 2px solid var(--line); border-radius: 0; background: var(--surface-raised); color: var(--ink); }
.keyword-list-field textarea, .named-card textarea { line-height: 1.5; resize: vertical; }
.row-actions { display: flex; flex-wrap: wrap; gap: 0.4rem; }
.row-actions button { min-width: 48px; min-height: 48px; border: 2px solid var(--line); background: var(--panel-muted); color: var(--ink); cursor: pointer; font-weight: 900; }
.row-actions button:disabled { opacity: 0.35; cursor: not-allowed; }
.row-actions .remove { background: var(--caution); color: var(--accent-ink); }
.source-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 0.65rem; }
.source-choice { min-height: 52px; display: flex; align-items: center; padding: 0.65rem; border: 2px solid var(--line); background: var(--surface-raised); font-weight: 900; }
.source-choice input { width: 22px; height: 22px; margin-right: 0.65rem; accent-color: var(--accent-ink); }
.invalid-sources { margin-top: 0.75rem; padding: 0.75rem; border: 2px solid var(--line); background: var(--caution); color: var(--accent-ink); }
.invalid-source-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 0.75rem; align-items: end; margin-top: 0.75rem; }
.invalid-source-row label { display: grid; gap: 0.35rem; font-weight: 800; }
.invalid-source-row input { width: 100%; min-height: 48px; padding: 0.7rem; border: 2px solid var(--line); border-radius: 0; }
.invalid-source-row button { min-height: 48px; border: 2px solid var(--line); background: var(--panel); font-weight: 900; }
.named-card { margin-top: 0.75rem; border: 2px solid var(--line); background: var(--surface-raised); }
.named-card summary { min-height: 58px; display: grid; grid-template-columns: auto minmax(0, 1fr) auto; gap: 0.75rem; align-items: center; padding: 0.65rem; cursor: pointer; }
.row-number { display: grid; place-items: center; width: 36px; height: 36px; background: var(--inverse-bg); color: var(--acid); font-weight: 900; }
.row-key { overflow-wrap: anywhere; color: var(--muted); font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 0.78rem; }
.named-card-body { display: grid; grid-template-columns: 0.7fr 1.3fr; gap: 1rem; padding: 1rem; border-top: 2px solid var(--line); }
.instructions-field, .named-card-body .row-actions { grid-column: 1 / -1; }
.affected { box-shadow: inset 6px 0 0 var(--caution); }
.button { min-height: 48px; padding: 0.65rem 1rem; border: 2px solid var(--line); color: var(--ink); cursor: pointer; font-weight: 900; }
.button.primary { background: var(--acid); color: var(--accent-ink); }
.button.secondary, .button.add { background: var(--panel); }
.button.add { margin-top: 0.9rem; }
.editor-actions { display: flex; justify-content: end; gap: 0.75rem; }
.validation-alert, .configuration-alert, .notice { margin: 0 0 1.5rem; padding: 1rem; border: 2px solid var(--line); background: var(--caution); color: var(--accent-ink); font-weight: 800; }
.notice { background: var(--acid); }
.field-error { color: #b22121; font-weight: 800; }
.configuration-preview, .release-panel { margin-top: 2rem; padding: clamp(1rem, 3vw, 1.6rem); border: 2px solid var(--line); background: var(--panel); box-shadow: 6px 6px 0 var(--shadow); }
.sample-list { padding-left: 1.3rem; line-height: 1.7; }
.prompt-summary { list-style: none; padding: 0; border: 2px solid var(--line); }
.prompt-summary li { display: flex; justify-content: space-between; gap: 1rem; padding: 0.7rem; border-bottom: 2px solid var(--line); }
.prompt-summary li:last-child { border-bottom: 0; }
.advanced-preview { margin-top: 1rem; border: 2px solid var(--line); }
.advanced-preview > summary { min-height: 48px; padding: 0.75rem; cursor: pointer; font-weight: 900; }
.compiled-prompt { padding: 1rem; border-top: 2px solid var(--line); }
.compiled-prompt pre { max-height: 360px; overflow: auto; white-space: pre-wrap; padding: 0.75rem; background: var(--surface-raised); color: var(--ink); }
.published-state, .active-state { padding: 0.8rem; border-left: 6px solid var(--acid); background: var(--panel-subtle); font-weight: 900; }
.masthead-nav { display: flex; align-self: stretch; margin-left: auto; }
.masthead-nav a { min-height: 52px; display: flex; align-items: center; padding: 0 0.8rem; border-left: 2px solid var(--line); text-decoration: none; font-weight: 900; }
.masthead-nav a[aria-current="page"] { background: var(--inverse-bg); color: var(--acid); }
@media (max-width: 760px) {
  .configuration-layout { grid-template-columns: 1fr; }
  .section-index { position: static; grid-template-columns: repeat(2, 1fr); }
  .section-index strong { grid-column: 1 / -1; }
  .section-index a { border-right: 2px solid var(--line); }
  .configuration-state { grid-template-columns: 1fr; }
  .configuration-state > div, .configuration-state > div:nth-child(3n), .configuration-state > div:nth-last-child(-n + 3) { border-right: 0; border-bottom: 2px solid var(--line); }
  .configuration-state > div:last-child { border-bottom: 0; }
  .named-card-body { grid-template-columns: 1fr; }
  .invalid-source-row { grid-template-columns: 1fr; }
  .instructions-field, .named-card-body .row-actions { grid-column: 1; }
  .named-card summary { grid-template-columns: auto minmax(0, 1fr); }
  .row-key { grid-column: 2; }
  .editor-actions { flex-direction: column; }
  .masthead { flex-wrap: wrap; }
  .masthead-nav { order: 3; width: 100%; border-top: 2px solid var(--line); }
  .masthead-nav a { flex: 1; justify-content: center; }
}
@media (max-width: 420px) {
  .source-grid, .preview-counts { grid-template-columns: 1fr; }
  .preview-counts > div { border-right: 0; border-bottom: 2px solid var(--line); }
  .preview-counts > div:last-child { border-bottom: 0; }
  .row-actions button { flex: 1; }
}
"""


def _transform[T](rows: tuple[T, ...], index: int, direction: str) -> tuple[T, ...]:
    if index < 0 or index >= len(rows):
        raise MalformedConfigurationForm("Invalid editor row")
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


def _named_rows(form: FormData, prefix: str, count_key: str) -> tuple[RawNamedRow, ...]:
    count = _count(form, count_key)
    return tuple(
        RawNamedRow(
            key=_single_text(form, f"{prefix}.{index}.key"),
            name=_single_text(form, f"{prefix}.{index}.name"),
            instructions=_single_text(form, f"{prefix}.{index}.instructions"),
        )
        for index in range(count)
    )


def _source_values(form: FormData, *, remove_index: int | None = None) -> tuple[str, ...]:
    ordered = _indexed_values(form, "source_order", "source_count")
    if remove_index is not None and (remove_index < 0 or remove_index >= len(ordered)):
        raise MalformedConfigurationForm("Invalid editor row")
    selected = tuple(_text_values(form, "source_selected"))
    preserved_indexes: set[int] = set()
    for raw_index in _text_values(form, "source_preserve"):
        try:
            index = int(raw_index)
        except ValueError as error:
            raise MalformedConfigurationForm("Invalid source_preserve value") from error
        if index < 0 or index >= len(ordered):
            raise MalformedConfigurationForm("Invalid source_preserve value")
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
        raise MalformedConfigurationForm(f"Invalid {key}") from error
    if count < 0 or count > 100:
        raise MalformedConfigurationForm(f"Invalid {key}")
    return count


def _single_text(form: FormData, key: str) -> str:
    values = _text_values(form, key)
    if len(values) != 1:
        raise MalformedConfigurationForm(f"Expected one {key} value")
    return values[0]


def _text_values(form: FormData, key: str) -> list[str]:
    values = form.getlist(key)
    if any(not isinstance(value, str) for value in values):
        raise MalformedConfigurationForm(f"Invalid {key} value")
    return [value for value in values if isinstance(value, str)]
