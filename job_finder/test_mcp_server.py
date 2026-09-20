from __future__ import annotations

import asyncio
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from typing import cast
from uuid import UUID

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import job_finder.mcp_server as server_module
from job_finder.configuration_service import (
    ActivationTargetUnpublished,
    ActiveConfigurationChanged,
    ActivateConfigurationCommand,
    ConfigurationRevisionCursor,
    ConfigurationRevisionDetails,
    ConfigurationRevisionNotFound,
    ConfigurationRevisionPage,
    ConfigurationRevisionSummary,
    DraftChanged,
    PublicationIdempotencyKeyConflict,
    PublishConfigurationCommand,
    PublishDraftChanged,
    PublishedActiveSearchConfiguration,
    SaveDraftCommand,
    preview_search_configuration,
)
from job_finder.evaluation.langfuse import ProjectionFailureSummary, ProjectionQueueStatus
from job_finder.evaluation.manifests import (
    CuratedReviewEvent,
    EvaluationManifest,
    ManifestPolicy,
    ManifestSummary,
    ManifestSummaryPage,
)
from job_finder.mcp_server import Connection, McpDependencies, create_mcp_server
from job_finder.review.models import (
    ReviewFeedback,
    ReviewFeedbackPage,
    ReviewFeedbackSummary,
    ReviewJob,
)
from job_finder.review.postgres import ReviewFeedbackNotFound
from job_finder.search_configuration import (
    ActiveSearchConfiguration,
    DEFAULT_SEARCH_CONFIGURATION,
    SearchConfigurationDraft,
    SearchConfigurationPublication,
    SearchConfigurationRevision,
    SearchConfigurationRevisionId,
    search_configuration_revision_id,
)

NOW = datetime(2026, 9, 19, 12, tzinfo=UTC)


@contextmanager
def _connect() -> Generator[Connection]:
    yield cast(Connection, object())


def _feedback() -> ReviewFeedback:
    return ReviewFeedback(
        review_event_id=UUID("00000000-0000-0000-0000-000000000001"),
        review_item_id=UUID("00000000-0000-0000-0000-000000000002"),
        evaluation_id="a" * 64,
        snapshot_id="b" * 64,
        decision="pursue",
        target_profile="early-stage-product-engineer",
        primary_reason="role-scope",
        note="Strong product ownership.",
        block_company=False,
        actor="owner",
        created_at=datetime(2026, 9, 19, tzinfo=UTC),
        original_outcome="rejected",
        matched_profile=None,
        evaluation_reason="Initially rejected.",
        job=ReviewJob(
            title="Product Engineer",
            company="Example",
            url="https://example.test/jobs/1",
            source="test",
            description="Build products.",
            location="Remote",
            keywords=("python",),
            date_posted=date(2026, 9, 18),
        ),
        frozen_manifest_count=0,
    )


def _feedback_summary(feedback: ReviewFeedback) -> ReviewFeedbackSummary:
    return ReviewFeedbackSummary(
        review_event_id=feedback.review_event_id,
        decision=feedback.decision,
        target_profile=feedback.target_profile,
        primary_reason=feedback.primary_reason,
        created_at=feedback.created_at,
        original_outcome=feedback.original_outcome,
        title=feedback.job.title,
        company=feedback.job.company,
        frozen_manifest_count=feedback.frozen_manifest_count,
    )


def test_mcp_tools_are_bounded_and_serialize_domain_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feedback = _feedback()

    def list_feedback(
        _connection: Connection,
        *,
        curation: str,
        limit: int,
        offset: int,
    ) -> ReviewFeedbackPage:
        assert (curation, limit, offset) == ("uncurated", 10, 0)
        return ReviewFeedbackPage(items=(_feedback_summary(feedback),), next_offset=None)

    monkeypatch.setattr(server_module, "list_review_feedback", list_feedback)
    server = create_mcp_server(McpDependencies(connect=_connect))

    async def exercise() -> None:
        async with Client(server) as client:
            tools = await client.list_tools()
            assert {tool.name for tool in tools} == {
                "feedback_list",
                "feedback_get",
                "feedback_curate",
                "manifest_preview",
                "manifest_create",
                "manifest_get",
                "manifest_list",
                "langfuse_projection_status",
                "configuration_active_get",
                "configuration_draft_get",
                "configuration_validate",
                "configuration_preview",
                "configuration_draft_update",
                "configuration_publish",
                "configuration_revision_list",
                "configuration_revision_get",
                "configuration_activate",
            }
            feedback_tool = next(tool for tool in tools if tool.name == "feedback_list")
            assert feedback_tool.annotations is not None
            assert feedback_tool.annotations.read_only_hint is True
            assert feedback_tool.input_schema["properties"]["limit"]["maximum"] == 100
            assert feedback_tool.output_schema is not None
            assert feedback_tool.output_schema["properties"]["items"]["maxItems"] == 100
            manifest_list_tool = next(tool for tool in tools if tool.name == "manifest_list")
            assert manifest_list_tool.output_schema is not None
            assert manifest_list_tool.output_schema["properties"]["items"]["maxItems"] == 100
            projection_tool = next(
                tool for tool in tools if tool.name == "langfuse_projection_status"
            )
            assert projection_tool.output_schema is not None
            assert projection_tool.output_schema["properties"]["failures"]["maxItems"] == 100
            configuration_tools = {
                tool.name: tool for tool in tools if tool.name.startswith("configuration_")
            }
            for name in (
                "configuration_active_get",
                "configuration_draft_get",
                "configuration_validate",
                "configuration_preview",
                "configuration_revision_list",
                "configuration_revision_get",
            ):
                annotations = configuration_tools[name].annotations
                assert annotations is not None
                assert annotations.read_only_hint is True
                assert annotations.destructive_hint is False
                assert annotations.open_world_hint is False
            for name in ("configuration_draft_update", "configuration_activate"):
                annotations = configuration_tools[name].annotations
                assert annotations is not None
                assert annotations.read_only_hint is False
                assert annotations.destructive_hint is True
                assert annotations.idempotent_hint is True
                assert annotations.open_world_hint is False
            publish_annotations = configuration_tools["configuration_publish"].annotations
            assert publish_annotations is not None
            assert publish_annotations.destructive_hint is False
            assert publish_annotations.idempotent_hint is True
            for tool in configuration_tools.values():
                assert "actor" not in tool.input_schema["properties"]
                assert "timestamp" not in tool.input_schema["properties"]
            assert (
                configuration_tools["configuration_validate"].input_schema["properties"][
                    "issue_limit"
                ]["maximum"]
                == 100
            )
            assert (
                configuration_tools["configuration_preview"].input_schema["properties"][
                    "search_sample_limit"
                ]["maximum"]
                == 100
            )
            assert (
                configuration_tools["configuration_preview"].input_schema["properties"][
                    "prompt_summary_limit"
                ]["maximum"]
                == 100
            )
            revision_list = configuration_tools["configuration_revision_list"]
            assert revision_list.input_schema["properties"]["limit"]["maximum"] == 100
            assert revision_list.output_schema is not None
            assert revision_list.output_schema["properties"]["items"]["maxItems"] == 100

            result = await client.call_tool(
                "feedback_list",
                {"curation": "uncurated", "limit": 10, "offset": 0},
            )
            assert ReviewFeedbackPage.model_validate(
                result.structured_content
            ) == ReviewFeedbackPage(items=(_feedback_summary(feedback),), next_offset=None)

            with pytest.raises(ToolError, match="validation error"):
                _ = await client.call_tool("feedback_list", {"limit": 0})

    asyncio.run(exercise())


def test_mcp_domain_errors_do_not_leak_internals(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing_feedback(_connection: Connection, _review_event_id: UUID) -> ReviewFeedback:
        raise ReviewFeedbackNotFound("Review feedback does not exist")

    monkeypatch.setattr(server_module, "load_review_feedback", missing_feedback)
    server = create_mcp_server(McpDependencies(connect=_connect))

    async def exercise() -> None:
        async with Client(server) as client:
            with pytest.raises(ToolError, match="Review feedback does not exist"):
                _ = await client.call_tool(
                    "feedback_get",
                    {"review_event_id": "00000000-0000-0000-0000-000000000001"},
                )

    asyncio.run(exercise())


def test_mcp_write_tools_supply_authority_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    review_event_id = UUID("00000000-0000-0000-0000-000000000001")
    curation = CuratedReviewEvent(
        id=UUID("00000000-0000-0000-0000-000000000003"),
        review_event_id=review_event_id,
        action="include",
        expected_outcome="qualified",
        critical=True,
        reason="Known false negative.",
        actor="agent-owner",
        created_at=NOW,
    )
    summary = ManifestSummary(
        id="c" * 64,
        policy=ManifestPolicy(),
        case_count=1,
        qualified_count=1,
        rejected_count=0,
        critical_count=1,
        trial_count=3,
        created_at=NOW,
        created_by="agent-owner",
    )

    def include_feedback(
        _connection: Connection,
        *,
        review_event_id: UUID,
        critical: bool,
        reason: str,
        actor: str,
        created_at: datetime,
        idempotency_key: str,
    ) -> CuratedReviewEvent:
        assert review_event_id == curation.review_event_id
        assert (critical, reason) == (True, "Known false negative.")
        assert (actor, created_at, idempotency_key) == ("agent-owner", NOW, "curation:1")
        return curation

    def freeze_manifest(
        _connection: Connection,
        *,
        policy: ManifestPolicy,
        created_at: datetime,
        created_by: str,
        idempotency_key: str,
    ) -> EvaluationManifest:
        assert policy == ManifestPolicy()
        assert (created_at, created_by) == (NOW, "agent-owner")
        assert idempotency_key == "manifest:1"
        return cast(EvaluationManifest, object())

    def summarize_frozen_manifest(_manifest: EvaluationManifest) -> ManifestSummary:
        return summary

    monkeypatch.setattr(server_module, "include_review_event", include_feedback)
    monkeypatch.setattr(server_module, "create_manifest", freeze_manifest)
    monkeypatch.setattr(server_module, "summarize_manifest", summarize_frozen_manifest)
    server = create_mcp_server(
        McpDependencies(connect=_connect, actor="agent-owner", now=lambda: NOW)
    )

    async def exercise() -> None:
        async with Client(server) as client:
            curated = await client.call_tool(
                "feedback_curate",
                {
                    "review_event_id": str(review_event_id),
                    "action": "include",
                    "reason": "Known false negative.",
                    "idempotency_key": "curation:1",
                    "critical": True,
                },
            )
            assert CuratedReviewEvent.model_validate(curated.structured_content) == curation

            manifest = await client.call_tool("manifest_create", {"idempotency_key": "manifest:1"})
            assert ManifestSummary.model_validate(manifest.structured_content) == summary

            with pytest.raises(ToolError, match="Excluded feedback cannot be marked critical"):
                _ = await client.call_tool(
                    "feedback_curate",
                    {
                        "review_event_id": str(review_event_id),
                        "action": "exclude",
                        "reason": "Not useful.",
                        "idempotency_key": "curation:2",
                        "critical": True,
                    },
                )

    asyncio.run(exercise())


def test_mcp_manifest_and_projection_reads_are_structured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_id = "d" * 64
    summary = ManifestSummary(
        id=manifest_id,
        policy=ManifestPolicy(),
        case_count=2,
        qualified_count=1,
        rejected_count=1,
        critical_count=1,
        trial_count=4,
        created_at=NOW,
        created_by="owner",
    )
    page = ManifestSummaryPage(items=(summary,), next_offset=10)
    status = ProjectionQueueStatus(
        pending_count=2,
        leased_count=1,
        completed_count=3,
        failed_count=1,
        failures=(
            ProjectionFailureSummary(
                id="e" * 64,
                kind="evaluation_manifest",
                source_id=manifest_id,
                attempt_count=2,
                retry_at=NOW,
                error_code="langfuse_unavailable",
            ),
        ),
    )

    def preview(_connection: Connection, policy: ManifestPolicy) -> ManifestSummary:
        assert policy == ManifestPolicy()
        return summary.model_copy(update={"id": "0" * 64, "created_at": None, "created_by": None})

    def load(_connection: Connection, requested_id: str) -> EvaluationManifest:
        assert requested_id == manifest_id
        return cast(EvaluationManifest, object())

    def summarize(_manifest: EvaluationManifest) -> ManifestSummary:
        return summary

    def list_frozen(_connection: Connection, *, limit: int, offset: int) -> ManifestSummaryPage:
        assert (limit, offset) == (10, 0)
        return page

    def projection_status(_connection: Connection, *, failure_limit: int) -> ProjectionQueueStatus:
        assert failure_limit == 5
        return status

    monkeypatch.setattr(server_module, "preview_manifest", preview)
    monkeypatch.setattr(server_module, "load_manifest", load)
    monkeypatch.setattr(server_module, "summarize_manifest", summarize)
    monkeypatch.setattr(server_module, "list_manifests", list_frozen)
    monkeypatch.setattr(server_module, "load_projection_queue_status", projection_status)
    server = create_mcp_server(McpDependencies(connect=_connect))

    async def exercise() -> None:
        async with Client(server) as client:
            preview_result = await client.call_tool("manifest_preview", {})
            assert ManifestSummary.model_validate(preview_result.structured_content).id == "0" * 64

            get_result = await client.call_tool("manifest_get", {"manifest_id": manifest_id})
            assert ManifestSummary.model_validate(get_result.structured_content) == summary

            list_result = await client.call_tool("manifest_list", {"limit": 10, "offset": 0})
            assert ManifestSummaryPage.model_validate(list_result.structured_content) == page

            status_result = await client.call_tool(
                "langfuse_projection_status", {"failure_limit": 5}
            )
            assert ProjectionQueueStatus.model_validate(status_result.structured_content) == status

    asyncio.run(exercise())


def test_mcp_configuration_tools_delegate_with_server_authority_and_structured_conflicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision_id = search_configuration_revision_id(DEFAULT_SEARCH_CONFIGURATION)
    preview = preview_search_configuration(DEFAULT_SEARCH_CONFIGURATION)
    revision = SearchConfigurationRevision(
        id=revision_id,
        configuration=DEFAULT_SEARCH_CONFIGURATION,
        created_at=NOW,
        created_by="agent-owner",
    )
    publication = SearchConfigurationPublication(
        revision_id=revision_id,
        prompt_release_id=preview.prompt_release_id,
        published_at=NOW,
        published_by="agent-owner",
    )
    draft = SearchConfigurationDraft(
        base_revision_id=revision_id,
        version=4,
        configuration=DEFAULT_SEARCH_CONFIGURATION,
        updated_at=NOW,
        updated_by="agent-owner",
    )
    active = PublishedActiveSearchConfiguration(
        active=ActiveSearchConfiguration(
            generation=2,
            revision=revision,
            activated_at=NOW,
            activated_by="agent-owner",
        ),
        publication=publication,
    )
    page = ConfigurationRevisionPage(
        items=(
            ConfigurationRevisionSummary(
                revision_id=revision_id,
                created_at=NOW,
                created_by="agent-owner",
                publication=publication,
            ),
        ),
        next_cursor=ConfigurationRevisionCursor(created_at=NOW, revision_id=revision_id),
    )
    next_cursor = ConfigurationRevisionCursor(created_at=NOW, revision_id=revision_id)
    details = ConfigurationRevisionDetails(revision=revision, publication=publication)
    clock_calls = 0

    def now() -> datetime:
        nonlocal clock_calls
        clock_calls += 1
        return NOW

    def get_active(_connection: Connection) -> PublishedActiveSearchConfiguration:
        return active

    def get_draft(_connection: Connection) -> SearchConfigurationDraft:
        return draft

    monkeypatch.setattr(server_module, "get_active_search_configuration", get_active)
    monkeypatch.setattr(server_module, "get_search_configuration_draft", get_draft)

    def list_revisions(
        _connection: Connection,
        *,
        limit: int,
        cursor: ConfigurationRevisionCursor | None,
    ) -> ConfigurationRevisionPage:
        assert limit == 5
        assert cursor == next_cursor
        return page

    monkeypatch.setattr(server_module, "list_search_configuration_revisions", list_revisions)

    def get_revision(
        _connection: Connection, _revision_id: SearchConfigurationRevisionId
    ) -> ConfigurationRevisionDetails:
        return details

    monkeypatch.setattr(server_module, "get_search_configuration_revision", get_revision)

    def save(_connection: Connection, command: SaveDraftCommand) -> DraftChanged:
        assert command.actor == "agent-owner"
        assert command.timestamp == NOW
        assert command.expected_version == 4
        return DraftChanged(current_draft=draft)

    def publish(
        _connection: Connection, command: PublishConfigurationCommand
    ) -> PublishDraftChanged | PublicationIdempotencyKeyConflict:
        assert command.actor == "agent-owner"
        assert command.timestamp == NOW
        if command.idempotency_key == "publish:conflict":
            return PublicationIdempotencyKeyConflict(idempotency_key=command.idempotency_key)
        return PublishDraftChanged(
            replayed=False,
            expected_draft_version=command.expected_draft_version,
            expected_configuration_revision_id=command.expected_configuration_revision_id,
            observed_draft_version=command.expected_draft_version + 1,
            observed_configuration_revision_id=command.expected_configuration_revision_id,
        )

    def activate(
        _connection: Connection, command: ActivateConfigurationCommand
    ) -> ActiveConfigurationChanged | ActivationTargetUnpublished:
        assert command.actor == "agent-owner"
        assert command.timestamp == NOW
        if command.expected_generation == 3:
            return ActivationTargetUnpublished(target_revision_id=command.target_revision_id)
        return ActiveConfigurationChanged(active_configuration=active)

    monkeypatch.setattr(server_module, "save_search_configuration_draft", save)
    monkeypatch.setattr(server_module, "publish_search_configuration", publish)
    monkeypatch.setattr(server_module, "activate_search_configuration", activate)
    server = create_mcp_server(McpDependencies(connect=_connect, actor="agent-owner", now=now))

    async def exercise() -> None:
        async with Client(server) as client:
            assert (
                PublishedActiveSearchConfiguration.model_validate(
                    (await client.call_tool("configuration_active_get", {})).structured_content
                )
                == active
            )
            assert (
                SearchConfigurationDraft.model_validate(
                    (await client.call_tool("configuration_draft_get", {})).structured_content
                )
                == draft
            )
            validation = await client.call_tool(
                "configuration_validate",
                {"candidate": DEFAULT_SEARCH_CONFIGURATION.model_dump(mode="json")},
            )
            assert validation.structured_content is not None
            assert validation.structured_content["result"]["kind"] == "valid"
            preview_result = await client.call_tool(
                "configuration_preview",
                {"configuration": DEFAULT_SEARCH_CONFIGURATION.model_dump(mode="json")},
            )
            assert preview_result.structured_content is not None
            assert preview_result.structured_content["configuration_revision_id"] == revision_id
            listed = await client.call_tool(
                "configuration_revision_list",
                {"limit": 5, "cursor": next_cursor.model_dump(mode="json")},
            )
            assert ConfigurationRevisionPage.model_validate(listed.structured_content) == page
            got = await client.call_tool("configuration_revision_get", {"revision_id": revision_id})
            assert ConfigurationRevisionDetails.model_validate(got.structured_content) == details
            saved = await client.call_tool(
                "configuration_draft_update",
                {
                    "expected_version": 4,
                    "configuration": DEFAULT_SEARCH_CONFIGURATION.model_dump(mode="json"),
                },
            )
            assert saved.structured_content is not None
            assert (
                DraftChanged.model_validate(saved.structured_content["result"]).current_draft
                == draft
            )
            published = await client.call_tool(
                "configuration_publish",
                {
                    "idempotency_key": "publish:1",
                    "expected_draft_version": 4,
                    "expected_configuration_revision_id": revision_id,
                },
            )
            assert published.structured_content is not None
            assert (
                PublishDraftChanged.model_validate(published.structured_content["result"]).kind
                == "draft_changed"
            )
            activated = await client.call_tool(
                "configuration_activate",
                {
                    "target_revision_id": revision_id,
                    "expected_active_revision_id": revision_id,
                    "expected_generation": 2,
                },
            )
            assert activated.structured_content is not None
            assert (
                ActiveConfigurationChanged.model_validate(
                    activated.structured_content["result"]
                ).active_configuration
                == active
            )
            publish_conflict = await client.call_tool(
                "configuration_publish",
                {
                    "idempotency_key": "publish:conflict",
                    "expected_draft_version": 4,
                    "expected_configuration_revision_id": revision_id,
                },
            )
            assert publish_conflict.structured_content is not None
            assert (
                PublicationIdempotencyKeyConflict.model_validate(
                    publish_conflict.structured_content["result"]
                ).kind
                == "idempotency_key_conflict"
            )
            unpublished = await client.call_tool(
                "configuration_activate",
                {
                    "target_revision_id": revision_id,
                    "expected_active_revision_id": revision_id,
                    "expected_generation": 3,
                },
            )
            assert unpublished.structured_content is not None
            assert (
                ActivationTargetUnpublished.model_validate(
                    unpublished.structured_content["result"]
                ).kind
                == "unpublished_target"
            )

    asyncio.run(exercise())
    assert clock_calls == 5


def test_mcp_configuration_revision_get_sanitizes_only_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision_id = search_configuration_revision_id(DEFAULT_SEARCH_CONFIGURATION)

    def missing(*_args: object) -> None:
        raise ConfigurationRevisionNotFound("Search configuration revision does not exist")

    monkeypatch.setattr(server_module, "get_search_configuration_revision", missing)
    server = create_mcp_server(McpDependencies(connect=_connect))

    async def exercise_missing() -> None:
        async with Client(server) as client:
            with pytest.raises(ToolError, match="Search configuration revision does not exist"):
                _ = await client.call_tool(
                    "configuration_revision_get", {"revision_id": revision_id}
                )

    asyncio.run(exercise_missing())

    def corrupt(*_args: object) -> None:
        raise RuntimeError("secret database details")

    monkeypatch.setattr(server_module, "get_search_configuration_revision", corrupt)
    server = create_mcp_server(McpDependencies(connect=_connect))

    async def exercise_corrupt() -> None:
        async with Client(server) as client:
            with pytest.raises(ToolError) as captured:
                _ = await client.call_tool(
                    "configuration_revision_get", {"revision_id": revision_id}
                )
            assert "secret database details" not in str(captured.value)

    asyncio.run(exercise_corrupt())


def test_mcp_configuration_validation_errors_hide_rejected_values() -> None:
    sensitive_value = "DO-NOT-LEAK"
    invalid_configuration = DEFAULT_SEARCH_CONFIGURATION.model_dump(mode="json")
    invalid_configuration["search_keywords"] = [sensitive_value, sensitive_value]
    server = create_mcp_server(McpDependencies(connect=_connect))

    async def exercise() -> None:
        async with Client(server) as client:
            for tool_name, arguments in (
                ("configuration_preview", {"configuration": invalid_configuration}),
                (
                    "configuration_draft_update",
                    {"expected_version": 0, "configuration": invalid_configuration},
                ),
            ):
                with pytest.raises(ToolError) as captured:
                    _ = await client.call_tool(tool_name, arguments)
                assert sensitive_value not in str(captured.value)

    asyncio.run(exercise())
