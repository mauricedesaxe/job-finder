from __future__ import annotations

from datetime import UTC, datetime
import re
from typing import final
from uuid import UUID

import psycopg
import pytest
from starlette.testclient import TestClient

from job_finder.config import ReviewAppSettings
from job_finder.configuration_service import (
    ActivateConfigurationCommand,
    ActivateConfigurationResult,
    ActiveConfigurationChanged,
    ConfigurationActivated,
    ConfigurationPublished,
    ConfigurationRevisionDetails,
    DraftChanged,
    DraftSaveResult,
    DraftSaved,
    PublicationIdempotencyKeyConflict,
    PublishConfigurationCommand,
    PublishDraftChanged,
    PublishConfigurationResult,
    PublishedActiveSearchConfiguration,
    SaveDraftCommand,
    preview_search_configuration_detailed,
    validate_search_configuration,
)
from job_finder.evaluation.prompt_releases import build_prompt_release
from job_finder.review.app import create_review_app
from job_finder.review.configuration_editor import (
    ConfigurationEditorService,
    ConfigurationEditorState,
)
from job_finder.review.models import ReviewQueue, ReviewSaved
from job_finder.review.postgres import ReviewService
from job_finder.search_configuration import (
    ActiveSearchConfiguration,
    DEFAULT_SEARCH_CONFIGURATION,
    PersonalCriterion,
    SearchConfiguration,
    SearchConfigurationDraft,
    SearchConfigurationPublication,
    SearchConfigurationRevision,
    SupportedSearchSource,
    search_configuration_revision_id,
)

NOW = datetime(2026, 9, 20, 15, tzinfo=UTC)
SETTINGS = ReviewAppSettings(
    app_password="correct horse battery staple",
    session_secret="s" * 32,
    cookie_secure=False,
)


@final
class ServiceHarness:
    def __init__(self, configuration: SearchConfiguration | None = None) -> None:
        self.configuration = configuration or _configuration()
        self.state = _state(self.configuration)
        self.calls: list[str] = []
        self.saved_commands: list[SaveDraftCommand] = []
        self.publish_commands: list[PublishConfigurationCommand] = []
        self.activate_commands: list[ActivateConfigurationCommand] = []
        self.save_result: DraftSaveResult | None = None
        self.publish_result: PublishConfigurationResult | psycopg.Error | None = None
        self.activate_result: ActivateConfigurationResult | None = None

    def service(self) -> ConfigurationEditorService:
        def inspect() -> ConfigurationEditorState:
            self.calls.append("inspect")
            return self.state

        def validate(candidate: object):
            self.calls.append("validate")
            return validate_search_configuration(candidate)

        def preview(configuration: SearchConfiguration):
            self.calls.append("preview")
            return preview_search_configuration_detailed(configuration)

        def save(command: SaveDraftCommand):
            self.calls.append("save")
            self.saved_commands.append(command)
            if self.save_result is not None:
                return self.save_result
            return DraftSaved(
                draft=self.state.draft.model_copy(
                    update={
                        "version": self.state.draft.version + 1,
                        "configuration": command.configuration,
                    }
                )
            )

        def publish(command: PublishConfigurationCommand):
            self.calls.append("publish")
            self.publish_commands.append(command)
            if self.publish_result is not None:
                if isinstance(self.publish_result, Exception):
                    raise self.publish_result
                return self.publish_result
            publication = _publication(self.configuration)
            return ConfigurationPublished(
                replayed=False,
                publication=publication,
                rebased_draft=self.state.draft.model_copy(
                    update={
                        "version": self.state.draft.version + 1,
                        "base_revision_id": publication.revision_id,
                    }
                ),
            )

        def activate(command: ActivateConfigurationCommand):
            self.calls.append("activate")
            self.activate_commands.append(command)
            if self.activate_result is not None:
                return self.activate_result
            return ConfigurationActivated(active_configuration=self.state.active)

        return ConfigurationEditorService(
            inspect=inspect,
            validate=validate,
            preview=preview,
            save=save,
            publish=publish,
            activate=activate,
        )


def test_configuration_requires_the_existing_owner_session() -> None:
    harness = ServiceHarness()
    client = _client(harness, authenticate=False)

    response = client.get("/configuration", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Fconfiguration"
    assert harness.calls == []


@pytest.mark.parametrize(
    "path",
    [
        "/configuration/edit",
        "/configuration/preview",
        "/configuration/draft",
        "/configuration/publish",
        "/configuration/activate",
    ],
)
def test_every_configuration_post_checks_csrf_before_services(path: str) -> None:
    harness = ServiceHarness()
    client = _client(harness)
    harness.calls.clear()

    response = client.post(path, data={})

    assert response.status_code == 403
    assert "form expired" in response.text
    assert harness.calls == []


def test_initial_editor_preserves_order_and_uses_the_shared_responsive_chrome() -> None:
    harness = ServiceHarness()

    response = _client(harness).get("/configuration")

    assert response.status_code == 200
    assert "first keyword\nsecond keyword" in response.text
    assert response.text.index("Criterion B") < response.text.index("Criterion A")
    assert 'value="lever" checked' in response.text
    assert 'value="ashby" checked' in response.text
    assert 'value="greenhouse" checked' not in response.text
    assert 'value="greenhouse"' in response.text
    assert 'value="workable"' in response.text
    assert 'aria-label="Owner workbench"' in response.text
    assert 'href="/review"' in response.text
    assert 'aria-current="page">Search setup' in response.text
    assert "@media (max-width: 760px)" in response.text
    assert "@media (prefers-color-scheme: dark)" in response.text
    assert "min-height: 48px" in response.text
    assert "<script" not in response.text
    assert "<link" not in response.text


def test_edit_actions_rerender_without_persistence_and_preserve_exact_values() -> None:
    harness = ServiceHarness()
    client = _client(harness)
    data = _form_data(harness.configuration, _csrf(client))
    data["search_keywords"] = "  exact whitespace  \nsecond keyword"
    data["action"] = "criterion.down.0"
    harness.calls.clear()

    response = client.post("/configuration/edit", data=data)

    assert response.status_code == 200
    assert "  exact whitespace  \nsecond keyword" in response.text
    assert "Unsaved browser changes are shown below" in response.text
    assert "Release actions still apply to the saved draft" in response.text
    assert "Save or discard the browser entries before using release actions" in response.text
    assert 'action="/configuration/publish"' not in response.text
    assert 'action="/configuration/activate"' not in response.text
    assert harness.calls == ["inspect"]
    assert harness.saved_commands == []


@pytest.mark.parametrize(
    ("action", "present", "absent", "open_card"),
    [
        ("criterion.up.1", "Criterion A", None, False),
        ("criterion.remove.1", "Criterion B", "Criterion A", False),
        ("criterion.add", "New criterion", None, True),
        ("profile.add", "New profile", None, True),
    ],
)
def test_all_edit_action_shapes_round_trip_through_the_route(
    action: str,
    present: str,
    absent: str | None,
    open_card: bool,
) -> None:
    harness = ServiceHarness()
    client = _client(harness)
    data = _form_data(harness.configuration, _csrf(client))
    data["personal_criteria.0.instructions"] = "  preserve criterion whitespace  "
    data["action"] = action

    response = client.post("/configuration/edit", data=data)

    assert response.status_code == 200
    assert present in response.text
    assert "  preserve criterion whitespace  " in response.text
    if absent is not None:
        assert absent not in response.text
    if open_card:
        assert re.search(
            r'<details(?=[^>]*class="named-card affected")(?=[^>]*\bopen\b)',
            response.text,
        )


def test_invalid_preview_preserves_and_escapes_exact_values() -> None:
    harness = ServiceHarness()
    client = _client(harness)
    data = _form_data(harness.configuration, _csrf(client))
    data["search_keywords"] = '<script>alert("x")</script> \nsecond keyword'

    response = client.post("/configuration/preview", data=data)

    assert response.status_code == 422
    assert '&lt;script&gt;alert("x")&lt;/script&gt; \nsecond keyword' in response.text
    assert '<script>alert("x")' not in response.text
    assert 'role="alert"' in response.text
    assert 'aria-invalid="true"' in response.text
    assert 'aria-describedby="configuration-issue-0"' in response.text
    assert "Search keywords must be non-empty, trimmed" in response.text
    assert harness.calls[-2:] == ["validate", "inspect"]


def test_invalid_source_values_round_trip_until_the_owner_removes_them() -> None:
    harness = ServiceHarness()
    client = _client(harness)
    data = _form_data(harness.configuration, _csrf(client))
    data["source_count"] = "4"
    data["source_order.2"] = "unknown-source"
    data["source_order.3"] = "lever"

    response = client.post("/configuration/preview", data=data)

    assert response.status_code == 422
    assert 'name="source_order.2" value="unknown-source"' in response.text
    assert 'name="source_order.3" value="lever"' in response.text
    assert 'name="source_preserve" value="2"' in response.text
    assert 'name="source_preserve" value="3"' in response.text
    assert 'value="source.remove.2"' in response.text
    assert 'value="source.remove.3"' in response.text
    assert 'id="configuration"' in response.text
    assert 'aria-invalid="true"' in response.text


def test_duplicate_keys_open_and_describe_the_affected_cards() -> None:
    configuration = _configuration().model_copy(
        update={
            "personal_criteria": (
                *_configuration().personal_criteria,
                DEFAULT_SEARCH_CONFIGURATION.personal_criteria[2],
            )
        }
    )
    harness = ServiceHarness(configuration)
    client = _client(harness)
    data = _form_data(harness.configuration, _csrf(client))
    data["personal_criteria.1.key"] = data["personal_criteria.0.key"]

    response = client.post("/configuration/preview", data=data)

    assert response.status_code == 422
    assert "Personal criterion keys must be unique" in response.text
    assert response.text.count('<details open class="named-card affected">') == 2
    for index in (0, 1):
        pattern = rf'<input(?=[^>]*id="personal_criteria-{index}-key")'
        pattern += r'(?=[^>]*aria-invalid="true")'
        pattern += r'(?=[^>]*aria-describedby="configuration-issue-0")[^>]*>'
        assert re.search(
            pattern,
            response.text,
        )
    unique_key = re.search(
        r'<input[^>]*id="personal_criteria-2-key"[^>]*>',
        response.text,
    )
    assert unique_key is not None
    assert 'aria-invalid="true"' not in unique_key.group()


def test_dirty_published_editor_hides_activation_form() -> None:
    harness = ServiceHarness()
    harness.state = _state(
        harness.configuration,
        publication=_publication(harness.configuration),
    )
    client = _client(harness)
    data = _form_data(harness.configuration, _csrf(client))
    data["search_keywords"] = "first keyword\nunsaved keyword"

    response = client.post("/configuration/preview", data=data)

    assert response.status_code == 200
    assert "Save or discard the browser entries before using release actions" in response.text
    assert 'action="/configuration/activate"' not in response.text


def test_preview_shows_counts_samples_summaries_and_closed_escaped_prompts() -> None:
    configuration = _configuration().model_copy(
        update={
            "personal_criteria": (
                PersonalCriterion(key="safe", name="Safe", instructions="Use <unsafe> literally"),
            )
        }
    )
    harness = ServiceHarness(configuration)
    client = _client(harness)

    response = client.post(
        "/configuration/preview",
        data=_form_data(configuration, _csrf(client)),
    )

    assert response.status_code == 200
    assert "What this configuration will produce" in response.text
    assert "site:jobs.lever.co first keyword" in response.text
    assert "Prompt summary" in response.text
    assert "Advanced: compiled system messages" in response.text
    assert "Use &lt;unsafe&gt; literally" in response.text
    assert "Use <unsafe> literally" not in response.text
    assert '<details class="advanced-preview">' in response.text
    assert harness.calls[-3:] == ["validate", "inspect", "preview"]


def test_save_uses_submitted_cas_authority_and_redirects_with_allowlisted_notice() -> None:
    harness = ServiceHarness()
    client = _client(harness)
    data = _form_data(harness.configuration, _csrf(client))
    data["search_keywords"] = "first keyword\nsaved keyword"

    response = client.post("/configuration/draft", data=data, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/configuration?notice=draft-saved"
    command = harness.saved_commands[0]
    assert command.expected_version == 4
    assert command.configuration.search_keywords == ("first keyword", "saved keyword")
    assert command.actor == "owner"
    assert command.timestamp == NOW
    assert harness.publish_commands == []
    assert harness.activate_commands == []


def test_stale_save_keeps_local_form_and_binds_explicit_retry_to_observed_version() -> None:
    harness = ServiceHarness()
    current = harness.state.draft.model_copy(update={"version": 5, "updated_by": "other"})
    harness.state = _state(harness.configuration, version=5)
    harness.save_result = DraftChanged(current_draft=current)
    client = _client(harness)
    data = _form_data(harness.configuration, _csrf(client))
    data["search_keywords"] = "first keyword\nlocal unsaved value"

    response = client.post("/configuration/draft", data=data)

    assert response.status_code == 409
    assert "first keyword\nlocal unsaved value" in response.text
    assert 'name="expected_draft_version" value="5"' in response.text
    assert "explicitly overwrite draft version 5" in response.text


def test_publish_uses_the_rendered_private_key_and_never_activates() -> None:
    harness = ServiceHarness()
    client = _client(harness)
    page = client.get("/configuration")
    key = _hidden(page.text, "idempotency_key")
    revision_id = search_configuration_revision_id(harness.configuration)

    response = client.post(
        "/configuration/publish",
        data={
            "csrf_token": _csrf(client),
            "idempotency_key": key,
            "expected_draft_version": "4",
            "expected_configuration_revision_id": revision_id,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/configuration?notice=published"
    assert harness.publish_commands[0].idempotency_key == key
    assert harness.publish_commands[0].expected_configuration_revision_id == revision_id
    assert harness.activate_commands == []


def test_publish_conflict_and_replay_are_explicit() -> None:
    harness = ServiceHarness()
    harness.publish_result = PublicationIdempotencyKeyConflict(idempotency_key="stable-key")
    client = _client(harness)
    data = _publish_data(client, harness, "stable-key")

    conflict = client.post("/configuration/publish", data=data)

    assert conflict.status_code == 409
    assert "belongs to a different request" in conflict.text
    publication = _publication(harness.configuration)
    harness.publish_result = ConfigurationPublished(
        replayed=True,
        publication=publication,
        rebased_draft=harness.state.draft.model_copy(
            update={"version": 5, "base_revision_id": publication.revision_id}
        ),
    )
    replay = client.post("/configuration/publish", data=data, follow_redirects=False)
    assert replay.status_code == 303
    assert replay.headers["location"] == "/configuration?notice=publication-replayed"


def test_publish_draft_change_conflict_names_the_observed_authority() -> None:
    harness = ServiceHarness()
    revision_id = search_configuration_revision_id(harness.configuration)
    harness.publish_result = PublishDraftChanged(
        replayed=False,
        expected_draft_version=4,
        expected_configuration_revision_id=revision_id,
        observed_draft_version=5,
        observed_configuration_revision_id=search_configuration_revision_id(
            DEFAULT_SEARCH_CONFIGURATION
        ),
    )
    client = _client(harness)

    response = client.post(
        "/configuration/publish",
        data=_publish_data(client, harness, "draft-conflict-key"),
    )

    assert response.status_code == 409
    assert "saved draft changed before publication" in response.text
    assert "Nothing was published" in response.text
    assert "Expected version 4" in response.text
    assert "observed version 5" in response.text
    assert revision_id in response.text


def test_ambiguous_publish_failure_preserves_the_exact_private_key_in_the_body() -> None:
    harness = ServiceHarness()
    harness.publish_result = psycopg.OperationalError("database down")
    client = _client(harness)
    data = _publish_data(client, harness, "stable-secret-key")

    response = client.post("/configuration/publish", data=data)

    assert response.status_code == 503
    assert 'name="idempotency_key" value="stable-secret-key"' in response.text
    assert "stable-secret-key" not in str(response.url)
    assert "Retry exact publication" in response.text


def test_activation_is_separate_and_reports_cas_conflict() -> None:
    harness = ServiceHarness()
    publication = _publication(harness.configuration)
    harness.state = _state(harness.configuration, publication=publication)
    current = harness.state.active
    harness.activate_result = ActiveConfigurationChanged(active_configuration=current)
    client = _client(harness)

    response = client.post(
        "/configuration/activate",
        data={
            "csrf_token": _csrf(client),
            "target_revision_id": publication.revision_id,
            "expected_active_revision_id": current.active.revision.id,
            "expected_generation": str(current.active.generation),
        },
    )

    assert response.status_code == 409
    assert "Nothing was overwritten" in response.text
    assert f"generation {current.active.generation}" in response.text
    assert current.active.revision.id in response.text
    assert harness.activate_commands[0].target_revision_id == publication.revision_id
    assert harness.publish_commands == []


def test_successful_activation_uses_prg_without_publishing() -> None:
    harness = ServiceHarness()
    publication = _publication(harness.configuration)
    harness.state = _state(harness.configuration, publication=publication)
    client = _client(harness)
    active = harness.state.active.active

    response = client.post(
        "/configuration/activate",
        data={
            "csrf_token": _csrf(client),
            "target_revision_id": publication.revision_id,
            "expected_active_revision_id": active.revision.id,
            "expected_generation": str(active.generation),
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/configuration?notice=activated"
    assert harness.activate_commands[0].expected_generation == active.generation
    assert harness.publish_commands == []


def test_configuration_database_failure_is_a_retryable_503() -> None:
    harness = ServiceHarness()

    def unavailable() -> ConfigurationEditorState:
        raise psycopg.OperationalError("database down")

    service = harness.service()
    service = ConfigurationEditorService(
        inspect=unavailable,
        validate=service.validate,
        preview=service.preview,
        save=service.save,
        publish=service.publish,
        activate=service.activate,
    )
    review = ReviewService(
        review_queue=lambda: ReviewQueue(),
        submit=lambda _review: ReviewSaved(review_event_id=UUID(int=1)),
    )
    client = TestClient(create_review_app(review, service, SETTINGS, now=lambda: NOW))
    _authenticate(client)

    response = client.get("/configuration")

    assert response.status_code == 503
    assert "Search setup is unavailable" in response.text
    assert "Saved configuration was not overwritten" in response.text


def test_malformed_transport_is_a_400_and_unknown_notice_is_not_reflected() -> None:
    harness = ServiceHarness()
    client = _client(harness)

    malformed = client.post(
        "/configuration/edit",
        data={
            "csrf_token": _csrf(client),
            "search_keywords": "first keyword",
            "source_count": "not-an-integer",
        },
    )
    unknown_notice = client.get("/configuration?notice=%3Cscript%3Ealert(1)%3C%2Fscript%3E")

    assert malformed.status_code == 400
    assert "Malformed configuration form" in malformed.text
    assert "<script>alert(1)</script>" not in unknown_notice.text


def _client(harness: ServiceHarness, *, authenticate: bool = True) -> TestClient:
    review = ReviewService(
        review_queue=lambda: ReviewQueue(),
        submit=lambda _review: ReviewSaved(review_event_id=UUID(int=1)),
    )
    client = TestClient(create_review_app(review, harness.service(), SETTINGS, now=lambda: NOW))
    if authenticate:
        _authenticate(client)
    return client


def _authenticate(client: TestClient) -> None:
    response = client.post(
        "/login",
        data={"password": SETTINGS.app_password, "next": "/configuration"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def _csrf(client: TestClient) -> str:
    return _hidden(client.get("/configuration").text, "csrf_token")


def _hidden(html: str, name: str) -> str:
    match = re.search(rf'name="{re.escape(name)}" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


def _form_data(configuration: SearchConfiguration, csrf_token: str) -> dict[str, str | list[str]]:
    data: dict[str, str | list[str]] = {
        "csrf_token": csrf_token,
        "expected_draft_version": "4",
        "search_keywords": "\n".join(configuration.search_keywords),
        "source_count": str(len(configuration.enabled_sources)),
        "source_selected": [source.value for source in configuration.enabled_sources],
        "criterion_count": str(len(configuration.personal_criteria)),
        "profile_count": str(len(configuration.target_profiles)),
    }
    for index, source in enumerate(configuration.enabled_sources):
        data[f"source_order.{index}"] = source.value
    for prefix, rows in (
        ("personal_criteria", configuration.personal_criteria),
        ("target_profiles", configuration.target_profiles),
    ):
        for index, row in enumerate(rows):
            data[f"{prefix}.{index}.key"] = row.key
            data[f"{prefix}.{index}.name"] = row.name
            data[f"{prefix}.{index}.instructions"] = row.instructions
    return data


def _publish_data(client: TestClient, harness: ServiceHarness, key: str) -> dict[str, str]:
    return {
        "csrf_token": _csrf(client),
        "idempotency_key": key,
        "expected_draft_version": "4",
        "expected_configuration_revision_id": search_configuration_revision_id(
            harness.configuration
        ),
    }


def _configuration() -> SearchConfiguration:
    criteria = DEFAULT_SEARCH_CONFIGURATION.personal_criteria
    return DEFAULT_SEARCH_CONFIGURATION.model_copy(
        update={
            "search_keywords": ("first keyword", "second keyword"),
            "enabled_sources": (
                SupportedSearchSource.LEVER,
                SupportedSearchSource.ASHBY,
            ),
            "personal_criteria": (
                criteria[1].model_copy(update={"name": "Criterion B"}),
                criteria[0].model_copy(update={"name": "Criterion A"}),
            ),
        }
    )


def _state(
    configuration: SearchConfiguration,
    *,
    version: int = 4,
    publication: SearchConfigurationPublication | None = None,
) -> ConfigurationEditorState:
    draft_revision_id = search_configuration_revision_id(configuration)
    draft = SearchConfigurationDraft(
        base_revision_id=search_configuration_revision_id(DEFAULT_SEARCH_CONFIGURATION),
        version=version,
        configuration=configuration,
        updated_at=NOW,
        updated_by="owner",
    )
    active_revision = SearchConfigurationRevision(
        id=search_configuration_revision_id(DEFAULT_SEARCH_CONFIGURATION),
        configuration=DEFAULT_SEARCH_CONFIGURATION,
        created_at=NOW,
        created_by="bootstrap",
    )
    active_publication = _publication(DEFAULT_SEARCH_CONFIGURATION)
    active = PublishedActiveSearchConfiguration(
        active=ActiveSearchConfiguration(
            generation=3,
            revision=active_revision,
            activated_at=NOW,
            activated_by="owner",
        ),
        publication=active_publication,
    )
    saved_revision = None
    if publication is not None:
        saved_revision = ConfigurationRevisionDetails(
            revision=SearchConfigurationRevision(
                id=draft_revision_id,
                configuration=configuration,
                created_at=NOW,
                created_by="owner",
            ),
            publication=publication,
        )
    return ConfigurationEditorState(
        draft=draft,
        active=active,
        content_revision_id=draft_revision_id,
        saved_revision=saved_revision,
    )


def _publication(configuration: SearchConfiguration) -> SearchConfigurationPublication:
    return SearchConfigurationPublication(
        revision_id=search_configuration_revision_id(configuration),
        prompt_release_id=build_prompt_release(configuration).id,
        published_at=NOW,
        published_by="owner",
    )
