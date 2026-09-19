from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest
from pydantic import TypeAdapter, ValidationError

import job_finder.configuration_service as service_module
from job_finder.configuration_service import (
    ConfigurationInvalid,
    ConfigurationPreview,
    ConfigurationValid,
    DraftChanged,
    DraftSaveResult,
    DraftSaved,
    SaveDraftCommand,
    preview_search_configuration,
    save_search_configuration_draft,
    validate_search_configuration,
)
from job_finder.evaluation.prompt_releases import build_prompt_release
from job_finder.search_configuration import (
    DEFAULT_SEARCH_CONFIGURATION,
    Connection,
    SearchConfiguration,
    SearchConfigurationDraft,
    SearchConfigurationRevisionId,
    SupportedSearchSource,
    search_configuration_revision_id,
)

NOW = datetime(2026, 9, 19, 12, tzinfo=UTC)
BASE_REVISION_ID = SearchConfigurationRevisionId("1" * 64)


def test_validation_returns_the_typed_configuration() -> None:
    result = validate_search_configuration(DEFAULT_SEARCH_CONFIGURATION.model_dump(mode="json"))

    assert isinstance(result, ConfigurationValid)
    assert result.configuration == DEFAULT_SEARCH_CONFIGURATION
    assert (
        TypeAdapter(service_module.ConfigurationValidationResult).validate_python(result) == result
    )


def test_validation_truncates_structured_issues_and_reports_omitted_count() -> None:
    result = validate_search_configuration({}, issue_limit=2)

    assert isinstance(result, ConfigurationInvalid)
    assert result.total_issue_count == 4
    assert result.omitted_issue_count == 2
    assert [issue.location for issue in result.issues] == [
        ("search_keywords",),
        ("enabled_sources",),
    ]
    assert all(issue.message for issue in result.issues)
    assert [issue.error_code for issue in result.issues] == ["missing", "missing"]


def test_validation_does_not_expose_rejected_input() -> None:
    secret = "do-not-return-this-secret"
    result = validate_search_configuration(
        {
            **DEFAULT_SEARCH_CONFIGURATION.model_dump(mode="json"),
            "search_keywords": [secret, secret.upper()],
            secret: secret,
        }
    )

    assert isinstance(result, ConfigurationInvalid)
    assert secret not in result.model_dump_json()


@pytest.mark.parametrize("limit", [0, 101])
def test_validation_rejects_limits_outside_the_fixed_range(limit: int) -> None:
    with pytest.raises(ValueError, match="between 1 and 100"):
        validate_search_configuration({}, issue_limit=limit)


def test_preview_reports_exact_identities_counts_order_and_truncation() -> None:
    configuration = DEFAULT_SEARCH_CONFIGURATION.model_copy(
        update={
            "search_keywords": ("first keyword", "second keyword"),
            "enabled_sources": (
                SupportedSearchSource.LEVER,
                SupportedSearchSource.ASHBY,
            ),
        }
    )
    release = build_prompt_release(configuration)

    preview = preview_search_configuration(
        configuration,
        search_sample_limit=3,
        prompt_summary_limit=2,
    )

    assert preview.configuration_revision_id == search_configuration_revision_id(configuration)
    assert preview.prompt_release_id == release.id
    assert preview.prompt_release_name == release.name
    assert preview.total_generated_search_count == 4
    assert preview.search_samples == (
        "site:jobs.lever.co first keyword",
        "site:jobs.ashbyhq.com first keyword",
        "site:jobs.lever.co second keyword",
    )
    assert preview.total_compiled_prompt_count == len(release.versions)
    assert [summary.model_dump() for summary in preview.prompt_summaries] == [
        {
            "name": version.definition.name,
            "criterion": version.definition.criterion,
            "phase": version.definition.phase,
            "prompt_version_id": version.id,
        }
        for version in release.versions[:2]
    ]


def test_preview_returns_a_frozen_stable_summary() -> None:
    preview = preview_search_configuration(DEFAULT_SEARCH_CONFIGURATION)

    assert ConfigurationPreview.model_validate(preview.model_dump()) == preview
    with pytest.raises(ValidationError, match="frozen"):
        setattr(preview, "prompt_release_name", "changed")


@pytest.mark.parametrize(
    ("search_limit", "prompt_limit"),
    [(0, 1), (101, 1), (1, 0), (1, 101)],
)
def test_preview_rejects_limits_outside_the_fixed_range(
    search_limit: int,
    prompt_limit: int,
) -> None:
    with pytest.raises(ValueError, match="between 1 and 100"):
        preview_search_configuration(
            DEFAULT_SEARCH_CONFIGURATION,
            search_sample_limit=search_limit,
            prompt_summary_limit=prompt_limit,
        )


def test_preview_has_no_sql_or_network_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("I/O was attempted")

    monkeypatch.setattr("psycopg.connect", fail)
    monkeypatch.setattr("requests.post", fail)

    preview = preview_search_configuration(DEFAULT_SEARCH_CONFIGURATION)

    assert preview.total_generated_search_count == 128


def test_draft_save_preserves_loaded_base_and_returns_typed_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = _draft(version=4, configuration=DEFAULT_SEARCH_CONFIGURATION)
    changed = DEFAULT_SEARCH_CONFIGURATION.model_copy(update={"search_keywords": ("changed",)})
    saved = _draft(version=5, configuration=changed)
    received: dict[str, object] = {}

    def load(_connection: Connection) -> SearchConfigurationDraft:
        return current

    monkeypatch.setattr(service_module, "load_search_configuration_draft", load)

    def replace(_connection: Connection, **kwargs: object) -> SearchConfigurationDraft:
        received.update(kwargs)
        return saved

    monkeypatch.setattr(service_module, "replace_search_configuration_draft", replace)

    result = save_search_configuration_draft(
        cast(Connection, object()),
        SaveDraftCommand(
            expected_version=4,
            configuration=changed,
            actor="owner",
            timestamp=NOW,
        ),
    )

    assert isinstance(result, DraftSaved)
    assert result.draft == saved
    assert received["base_revision_id"] == BASE_REVISION_ID
    assert received["expected_version"] == 4


def test_draft_save_returns_current_draft_after_a_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _draft(version=4, configuration=DEFAULT_SEARCH_CONFIGURATION)
    current = before.model_copy(
        update={
            "version": 5,
            "base_revision_id": SearchConfigurationRevisionId("2" * 64),
            "updated_by": "publisher",
        }
    )
    drafts = iter((before, current))

    def load(_connection: Connection) -> SearchConfigurationDraft:
        return next(drafts)

    def reject(_connection: Connection, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(service_module, "load_search_configuration_draft", load)
    monkeypatch.setattr(service_module, "replace_search_configuration_draft", reject)

    result = save_search_configuration_draft(
        cast(Connection, object()),
        SaveDraftCommand(
            expected_version=4,
            configuration=DEFAULT_SEARCH_CONFIGURATION,
            actor="owner",
            timestamp=NOW,
        ),
    )

    assert isinstance(result, DraftChanged)
    assert result.current_draft == current
    assert TypeAdapter(DraftSaveResult).validate_python(result) == result


def test_draft_save_does_not_write_when_the_loaded_version_differs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = _draft(version=4, configuration=DEFAULT_SEARCH_CONFIGURATION)

    def load(_connection: Connection) -> SearchConfigurationDraft:
        return current

    monkeypatch.setattr(service_module, "load_search_configuration_draft", load)

    def fail_replace(_connection: Connection, **_kwargs: object) -> None:
        raise AssertionError("stale draft was written")

    monkeypatch.setattr(service_module, "replace_search_configuration_draft", fail_replace)

    result = save_search_configuration_draft(
        cast(Connection, object()),
        SaveDraftCommand(
            expected_version=5,
            configuration=DEFAULT_SEARCH_CONFIGURATION,
            actor="owner",
            timestamp=NOW,
        ),
    )

    assert result == DraftChanged(current_draft=current)


def _draft(
    *,
    version: int,
    configuration: SearchConfiguration,
) -> SearchConfigurationDraft:
    return SearchConfigurationDraft(
        base_revision_id=BASE_REVISION_ID,
        version=version,
        configuration=configuration,
        updated_at=NOW,
        updated_by="owner",
    )
