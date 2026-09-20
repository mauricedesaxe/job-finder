from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from collections.abc import Generator
from typing import cast

import pytest
from starlette.datastructures import FormData, UploadFile

import job_finder.review.configuration_editor as editor_module
from job_finder.configuration_service import (
    ConfigurationRevisionDetails,
    PublishedActiveSearchConfiguration,
)
from job_finder.evaluation.prompt_releases import build_prompt_release
from job_finder.review.configuration_editor import (
    MalformedConfigurationForm,
    RawConfigurationForm,
    RawNamedRow,
    apply_configuration_edit,
    parse_configuration_form,
    postgres_configuration_editor_service,
    transform_rows,
)
from job_finder.search_configuration import (
    ActiveSearchConfiguration,
    Connection,
    DEFAULT_SEARCH_CONFIGURATION,
    SearchConfigurationDraft,
    SearchConfigurationPublication,
    SearchConfigurationRevision,
    search_configuration_revision_id,
)

NOW = datetime(2026, 9, 20, 12, tzinfo=UTC)


def test_form_parser_preserves_order_whitespace_blanks_and_duplicates() -> None:
    values: list[tuple[str, str | UploadFile]] = [
        ("search_keywords", " first \r\n\r\n first "),
        ("source_count", "2"),
        ("source_order.0", "lever"),
        ("source_order.1", "unknown"),
        ("source_selected", "lever"),
        ("criterion_count", "1"),
        ("personal_criteria.0.key", " key "),
        ("personal_criteria.0.name", " name "),
        ("personal_criteria.0.instructions", " instructions\n"),
        ("profile_count", "1"),
        ("target_profiles.0.key", "profile"),
        ("target_profiles.0.name", "Profile"),
        ("target_profiles.0.instructions", "Do work"),
        ("expected_draft_version", "7"),
    ]
    form = FormData(values)

    raw = parse_configuration_form(form)

    assert raw.search_keywords == (" first ", "", " first ")
    assert raw.enabled_sources == ("lever", "unknown")
    assert raw.personal_criteria == (RawNamedRow(" key ", " name ", " instructions\n"),)
    assert raw.expected_draft_version == "7"


def test_corrected_invalid_source_is_not_dropped_when_its_checkbox_was_absent() -> None:
    form = FormData(
        [
            ("search_keywords", "keyword"),
            ("source_count", "1"),
            ("source_order.0", "ashby"),
            ("source_preserve", "0"),
            ("criterion_count", "0"),
            ("profile_count", "0"),
            ("expected_draft_version", "7"),
        ]
    )

    assert parse_configuration_form(form).enabled_sources == ("ashby",)


def test_source_removal_uses_submitted_position_before_filtering_unchecked_sources() -> None:
    form = FormData(
        [
            ("search_keywords", "keyword"),
            ("source_count", "3"),
            ("source_order.0", "ashby"),
            ("source_order.1", "lever"),
            ("source_order.2", "unknown"),
            ("source_selected", "lever"),
            ("source_preserve", "2"),
            ("criterion_count", "0"),
            ("profile_count", "0"),
            ("expected_draft_version", "7"),
        ]
    )

    raw = apply_configuration_edit(form, "source.remove.2")

    assert raw.enabled_sources == ("lever",)


def test_named_and_source_row_actions_preserve_exact_values() -> None:
    raw = RawConfigurationForm(
        search_keywords=(" one ", "two"),
        enabled_sources=("ashby",),
        personal_criteria=(
            RawNamedRow("a", "A", "first"),
            RawNamedRow("b", "B", "second"),
        ),
        target_profiles=(RawNamedRow("p", "P", "profile"),),
        expected_draft_version="4",
    )

    assert [row.key for row in transform_rows(raw, "criterion.up.1").personal_criteria] == [
        "b",
        "a",
    ]
    assert transform_rows(raw, "profile.add").target_profiles[-1] == RawNamedRow("", "", "")


@pytest.mark.parametrize(
    "form",
    [
        FormData(),
        FormData({"search_keywords": "a", "source_count": "one"}),
        FormData(
            {
                "search_keywords": "a",
                "source_count": "0",
            }
        ),
        FormData(
            {
                "search_keywords": "a",
                "source_count": "0",
                "source_preserve": "0",
            }
        ),
    ],
)
def test_form_parser_rejects_malformed_transport(form: FormData) -> None:
    with pytest.raises(MalformedConfigurationForm):
        parse_configuration_form(form)


def test_postgres_adapter_opens_one_connection_for_inspection_and_has_no_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = cast(Connection, object())
    opens = 0
    draft = _draft()
    active = _active()
    revision = ConfigurationRevisionDetails(
        revision=active.active.revision,
        publication=active.publication,
    )

    @contextmanager
    def connect() -> Generator[Connection, None, None]:
        nonlocal opens
        opens += 1
        yield connection

    def get_draft(_connection: Connection) -> SearchConfigurationDraft:
        return draft

    def get_active(_connection: Connection) -> PublishedActiveSearchConfiguration:
        return active

    def get_revision(_connection: Connection, _revision_id: object) -> ConfigurationRevisionDetails:
        return revision

    monkeypatch.setattr(editor_module, "get_search_configuration_draft", get_draft)
    monkeypatch.setattr(editor_module, "get_active_search_configuration", get_active)
    monkeypatch.setattr(
        editor_module,
        "get_search_configuration_revision",
        get_revision,
    )

    state = postgres_configuration_editor_service(connect).inspect()

    assert opens == 1
    assert state.draft == draft
    assert state.active == active
    assert state.saved_revision == revision


def _draft() -> SearchConfigurationDraft:
    revision_id = search_configuration_revision_id(DEFAULT_SEARCH_CONFIGURATION)
    return SearchConfigurationDraft(
        base_revision_id=revision_id,
        version=4,
        configuration=DEFAULT_SEARCH_CONFIGURATION,
        updated_at=NOW,
        updated_by="owner",
    )


def _active() -> PublishedActiveSearchConfiguration:
    revision_id = search_configuration_revision_id(DEFAULT_SEARCH_CONFIGURATION)
    release = build_prompt_release(DEFAULT_SEARCH_CONFIGURATION)
    revision = SearchConfigurationRevision(
        id=revision_id,
        configuration=DEFAULT_SEARCH_CONFIGURATION,
        created_at=NOW,
        created_by="owner",
    )
    publication = SearchConfigurationPublication(
        revision_id=revision_id,
        prompt_release_id=release.id,
        published_at=NOW,
        published_by="owner",
    )
    return PublishedActiveSearchConfiguration(
        active=ActiveSearchConfiguration(
            generation=2,
            revision=revision,
            activated_at=NOW,
            activated_by="owner",
        ),
        publication=publication,
    )
