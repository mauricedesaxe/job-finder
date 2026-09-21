from __future__ import annotations

from datetime import UTC, datetime
from collections.abc import Sequence
from typing import cast, final

import pytest
from pydantic import TypeAdapter, ValidationError

import job_finder.configuration_service as service_module
from job_finder.configuration_service import (
    ActivateConfigurationResult,
    ActivationTargetUnpublished,
    ActiveConfigurationChanged,
    ActivateConfigurationCommand,
    ConfigurationActivated,
    ConfigurationPublished,
    ConfigurationInvalid,
    ConfigurationPreview,
    DetailedConfigurationPreview,
    ConfigurationRevisionCursor,
    ConfigurationRevisionDetails,
    ConfigurationRevisionNotFound,
    ConfigurationRevisionPage,
    ConfigurationRevisionSummary,
    ConfigurationValid,
    DraftChanged,
    DraftSaveResult,
    DraftSaved,
    PublicationIdempotencyKeyConflict,
    PublishConfigurationCommand,
    PublishConfigurationResult,
    PublishDraftChanged,
    PublishedActiveSearchConfiguration,
    SaveDraftCommand,
    activate_search_configuration,
    get_search_configuration_revision,
    list_search_configuration_revisions,
    preview_search_configuration,
    preview_search_configuration_detailed,
    save_search_configuration_draft,
    validate_search_configuration,
)
from job_finder.evaluation.prompt_releases import build_prompt_release
from job_finder.search_configuration import (
    ActiveSearchConfiguration,
    DEFAULT_SEARCH_CONFIGURATION,
    Connection,
    SearchConfiguration,
    SearchConfigurationDraft,
    SearchConfigurationError,
    SearchConfigurationPublication,
    SearchConfigurationPublicationNotFound,
    SearchConfigurationRevision,
    SearchConfigurationRevisionId,
    SearchConfigurationRevisionNotFound,
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


def test_detailed_preview_builds_once_and_returns_the_full_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = build_prompt_release(DEFAULT_SEARCH_CONFIGURATION)
    calls = 0

    def build(_configuration: SearchConfiguration) -> object:
        nonlocal calls
        calls += 1
        return release

    monkeypatch.setattr(service_module, "build_prompt_release", build)

    preview = preview_search_configuration_detailed(DEFAULT_SEARCH_CONFIGURATION)

    assert calls == 1
    assert preview == DetailedConfigurationPreview(
        summary=ConfigurationPreview(
            configuration_revision_id=search_configuration_revision_id(
                DEFAULT_SEARCH_CONFIGURATION
            ),
            prompt_release_id=release.id,
            prompt_release_name=release.name,
            total_generated_search_count=128,
            search_samples=preview.summary.search_samples,
            total_compiled_prompt_count=len(release.versions),
            prompt_summaries=preview.summary.prompt_summaries,
        ),
        prompt_release=release,
    )


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


def test_configuration_result_collections_have_schema_and_runtime_bounds() -> None:
    invalid_schema = ConfigurationInvalid.model_json_schema()
    preview_schema = ConfigurationPreview.model_json_schema()
    page_schema = ConfigurationRevisionPage.model_json_schema()

    assert invalid_schema["properties"]["issues"]["maxItems"] == 100
    assert preview_schema["properties"]["search_samples"]["maxItems"] == 100
    assert preview_schema["properties"]["prompt_summaries"]["maxItems"] == 100
    assert page_schema["properties"]["items"]["maxItems"] == 100
    with pytest.raises(ValidationError, match="at most 100"):
        ConfigurationRevisionPage(items=tuple(_summary(index) for index in range(101)))


def test_revision_get_returns_optional_publication_and_maps_only_missing_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = _revision()
    publication = _publication()

    def load_revision(
        _connection: Connection, _revision_id: SearchConfigurationRevisionId
    ) -> SearchConfigurationRevision:
        return revision

    def load_publication(
        _connection: Connection, _revision_id: SearchConfigurationRevisionId
    ) -> SearchConfigurationPublication:
        return publication

    monkeypatch.setattr(service_module, "load_search_configuration_revision", load_revision)
    monkeypatch.setattr(service_module, "load_search_configuration_publication", load_publication)

    assert get_search_configuration_revision(cast(Connection, object()), revision.id) == (
        ConfigurationRevisionDetails(revision=revision, publication=publication)
    )

    def missing_publication(*_args: object) -> None:
        raise SearchConfigurationPublicationNotFound("missing")

    monkeypatch.setattr(
        service_module, "load_search_configuration_publication", missing_publication
    )
    assert (
        get_search_configuration_revision(cast(Connection, object()), revision.id).publication
        is None
    )

    def missing_revision(*_args: object) -> None:
        raise SearchConfigurationRevisionNotFound("missing")

    monkeypatch.setattr(service_module, "load_search_configuration_revision", missing_revision)
    with pytest.raises(ConfigurationRevisionNotFound, match="does not exist"):
        get_search_configuration_revision(cast(Connection, object()), revision.id)

    def corrupt_revision(*_args: object) -> None:
        raise SearchConfigurationError("corrupt")

    monkeypatch.setattr(service_module, "load_search_configuration_revision", corrupt_revision)
    with pytest.raises(SearchConfigurationError, match="corrupt"):
        get_search_configuration_revision(cast(Connection, object()), revision.id)


def test_revision_list_uses_bounded_compound_keyset_and_omits_bodies() -> None:
    historical_actor = "a" * 201
    rows: list[tuple[object, ...]] = [
        (
            str(index) * 64,
            NOW,
            historical_actor if index == 3 else f"owner-{index}",
            ("f" * 64) if index != 2 else None,
            NOW if index != 2 else None,
            "publisher" if index != 2 else None,
        )
        for index in (3, 2, 1)
    ]
    connection = _ListConnection(rows)
    cursor = ConfigurationRevisionCursor(
        created_at=NOW, revision_id=SearchConfigurationRevisionId("4" * 64)
    )

    page = list_search_configuration_revisions(
        cast(Connection, cast(object, connection)), limit=2, cursor=cursor
    )

    assert [item.revision_id for item in page.items] == ["3" * 64, "2" * 64]
    assert page.items[0].created_by == historical_actor
    assert page.items[0].publication is not None
    assert page.items[1].publication is None
    assert page.next_cursor == ConfigurationRevisionCursor(
        created_at=NOW, revision_id=SearchConfigurationRevisionId("2" * 64)
    )
    assert connection.parameters == (NOW, NOW, "4" * 64, 3)
    assert "r.content" not in connection.query


@pytest.mark.parametrize("limit", [0, 101])
def test_revision_list_rejects_invalid_limits(limit: int) -> None:
    with pytest.raises(ValueError, match="between 1 and 100"):
        list_search_configuration_revisions(
            cast(Connection, cast(object, _ListConnection([]))), limit=limit
        )


def test_activation_maps_only_a_missing_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def corrupt_publication(*_args: object) -> None:
        raise SearchConfigurationError("corrupt publication")

    monkeypatch.setattr(
        service_module, "load_search_configuration_publication", corrupt_publication
    )
    with pytest.raises(SearchConfigurationError, match="corrupt publication"):
        activate_search_configuration(
            cast(Connection, object()),
            ActivateConfigurationCommand(
                target_revision_id=BASE_REVISION_ID,
                expected_active_revision_id=BASE_REVISION_ID,
                expected_generation=0,
                actor="owner",
                timestamp=NOW,
            ),
        )


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


def test_publication_results_parse_as_discriminated_variants() -> None:
    publication = _publication()
    rebased = _draft(version=5, configuration=DEFAULT_SEARCH_CONFIGURATION).model_copy(
        update={"base_revision_id": publication.revision_id}
    )
    results = (
        ConfigurationPublished(
            replayed=False,
            publication=publication,
            rebased_draft=rebased,
        ),
        PublishDraftChanged(
            replayed=True,
            expected_draft_version=4,
            expected_configuration_revision_id=publication.revision_id,
            observed_draft_version=5,
            observed_configuration_revision_id=BASE_REVISION_ID,
        ),
        PublicationIdempotencyKeyConflict(
            idempotency_key="publish",
        ),
    )

    for result in results:
        assert (
            TypeAdapter(PublishConfigurationResult).validate_python(result.model_dump()) == result
        )
    with pytest.raises(ValueError, match="rebased draft differ"):
        ConfigurationPublished(
            replayed=False,
            publication=publication,
            rebased_draft=rebased.model_copy(update={"base_revision_id": BASE_REVISION_ID}),
        )
    with pytest.raises(ValueError, match="draft state must differ"):
        PublishDraftChanged(
            replayed=False,
            expected_draft_version=4,
            expected_configuration_revision_id=publication.revision_id,
            observed_draft_version=4,
            observed_configuration_revision_id=publication.revision_id,
        )


def test_activation_results_parse_and_active_pair_rejects_mismatched_revision() -> None:
    publication = _publication()
    revision = SearchConfigurationRevision(
        id=publication.revision_id,
        configuration=DEFAULT_SEARCH_CONFIGURATION,
        created_at=NOW,
        created_by="owner",
    )
    active = ActiveSearchConfiguration(
        generation=1,
        revision=revision,
        activated_at=NOW,
        activated_by="owner",
    )
    paired = PublishedActiveSearchConfiguration(active=active, publication=publication)
    results = (
        ConfigurationActivated(active_configuration=paired),
        ActivationTargetUnpublished(target_revision_id=BASE_REVISION_ID),
        ActiveConfigurationChanged(active_configuration=paired),
    )

    for result in results:
        assert (
            TypeAdapter(ActivateConfigurationResult).validate_python(result.model_dump()) == result
        )
    with pytest.raises(ValueError, match="revision IDs differ"):
        PublishedActiveSearchConfiguration(
            active=active,
            publication=publication.model_copy(update={"revision_id": BASE_REVISION_ID}),
        )


def test_publication_and_activation_commands_are_frozen_and_bound_text_lengths() -> None:
    revision_id = search_configuration_revision_id(DEFAULT_SEARCH_CONFIGURATION)
    publish = PublishConfigurationCommand(
        idempotency_key="publish",
        expected_draft_version=0,
        expected_configuration_revision_id=revision_id,
        actor="owner",
        timestamp=NOW,
    )
    activate = ActivateConfigurationCommand(
        target_revision_id=revision_id,
        expected_active_revision_id=revision_id,
        expected_generation=0,
        actor="owner",
        timestamp=NOW,
    )

    with pytest.raises(ValidationError, match="frozen"):
        setattr(publish, "actor", "other")
    with pytest.raises(ValidationError, match="frozen"):
        setattr(activate, "actor", "other")
    with pytest.raises(ValidationError, match="at most 200"):
        PublishConfigurationCommand.model_validate(
            {**publish.model_dump(), "idempotency_key": "x" * 201}
        )
    with pytest.raises(ValidationError, match="at most 200"):
        ActivateConfigurationCommand.model_validate({**activate.model_dump(), "actor": "x" * 201})
    with pytest.raises(ValidationError, match="PostgreSQL"):
        PublishConfigurationCommand.model_validate(
            {**publish.model_dump(), "idempotency_key": "publish\x00unsafe"}
        )
    with pytest.raises(ValidationError, match="less than or equal"):
        PublishConfigurationCommand.model_validate(
            {**publish.model_dump(), "expected_draft_version": 2**63}
        )


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


def _publication() -> SearchConfigurationPublication:
    return SearchConfigurationPublication(
        revision_id=search_configuration_revision_id(DEFAULT_SEARCH_CONFIGURATION),
        prompt_release_id=build_prompt_release(DEFAULT_SEARCH_CONFIGURATION).id,
        published_at=NOW,
        published_by="owner",
    )


def _revision() -> SearchConfigurationRevision:
    return SearchConfigurationRevision(
        id=search_configuration_revision_id(DEFAULT_SEARCH_CONFIGURATION),
        configuration=DEFAULT_SEARCH_CONFIGURATION,
        created_at=NOW,
        created_by="owner",
    )


def _summary(index: int) -> ConfigurationRevisionSummary:
    return ConfigurationRevisionSummary(
        revision_id=SearchConfigurationRevisionId(f"{index % 10}" * 64),
        created_at=NOW,
        created_by="owner",
        publication=None,
    )


@final
class _ListResult:
    def __init__(self, rows: Sequence[tuple[object, ...]]) -> None:
        self.rows = rows

    def fetchall(self) -> Sequence[tuple[object, ...]]:
        return self.rows


@final
class _ListConnection:
    autocommit: bool = True

    def __init__(self, rows: Sequence[tuple[object, ...]]) -> None:
        self.rows = rows
        self.query = ""
        self.parameters: tuple[object, ...] = ()

    def execute(self, query: str, parameters: tuple[object, ...]) -> _ListResult:
        self.query = query
        self.parameters = parameters
        return _ListResult(self.rows)
