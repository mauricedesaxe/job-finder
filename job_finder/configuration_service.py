from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, ClassVar, Literal, cast

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)
from psycopg.types.json import Jsonb

from job_finder.evaluation.models import PromptReleaseId, PromptVersionId
from job_finder.evaluation.prompt_releases import build_prompt_release, store_prompt_release
from job_finder.evaluation.prompts import PromptPhase
from job_finder.search_configuration import (
    SEARCH_SOURCE_DOMAINS,
    ActiveSearchConfiguration,
    Connection,
    SearchConfiguration,
    SearchConfigurationDraft,
    SearchConfigurationError,
    SearchConfigurationPublication,
    SearchConfigurationRevisionId,
    build_search_configuration_revision,
    compare_and_swap_active_search_configuration,
    load_active_search_configuration,
    load_search_configuration_draft,
    load_search_configuration_publication,
    load_search_configuration_revision,
    replace_search_configuration_draft,
    search_configuration_revision_id,
    store_search_configuration_revision,
)

MIN_RESULT_LIMIT = 1
MAX_RESULT_LIMIT = 100
DEFAULT_RESULT_LIMIT = 20
MAX_IDEMPOTENCY_KEY_LENGTH = 200
MAX_ACTOR_LENGTH = 200
MAX_POSTGRES_BIGINT = 2**63 - 1


def _require_database_safe_text(value: str, label: str) -> str:
    if "\x00" in value or any("\ud800" <= character <= "\udfff" for character in value):
        raise ValueError(f"{label} contains a character PostgreSQL cannot store")
    return value


def _require_database_safe_actor(value: str) -> str:
    return _require_database_safe_text(value, "Actor")


def _require_database_safe_idempotency_key(value: str) -> str:
    return _require_database_safe_text(value, "Idempotency key")


ConfigurationRevisionId = Annotated[SearchConfigurationRevisionId, Field(pattern=r"^[0-9a-f]{64}$")]
Actor = Annotated[
    str,
    Field(min_length=1, max_length=MAX_ACTOR_LENGTH),
    AfterValidator(_require_database_safe_actor),
]
IdempotencyKey = Annotated[
    str,
    Field(min_length=1, max_length=MAX_IDEMPOTENCY_KEY_LENGTH),
    AfterValidator(_require_database_safe_idempotency_key),
]


class ConfigurationServiceModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class ConfigurationValidationIssue(ConfigurationServiceModel):
    location: tuple[str | int, ...]
    message: str
    error_code: str


class ConfigurationValid(ConfigurationServiceModel):
    kind: Literal["valid"] = "valid"
    configuration: SearchConfiguration


class ConfigurationInvalid(ConfigurationServiceModel):
    kind: Literal["invalid"] = "invalid"
    issues: tuple[ConfigurationValidationIssue, ...]
    total_issue_count: int = Field(ge=1)
    omitted_issue_count: int = Field(ge=0)


ConfigurationValidationResult = Annotated[
    ConfigurationValid | ConfigurationInvalid,
    Field(discriminator="kind"),
]


class PromptSummary(ConfigurationServiceModel):
    name: str
    criterion: str
    phase: PromptPhase
    prompt_version_id: PromptVersionId


class ConfigurationPreview(ConfigurationServiceModel):
    configuration_revision_id: SearchConfigurationRevisionId
    prompt_release_id: PromptReleaseId
    prompt_release_name: str
    total_generated_search_count: int = Field(ge=1)
    search_samples: tuple[str, ...]
    total_compiled_prompt_count: int = Field(ge=1)
    prompt_summaries: tuple[PromptSummary, ...]


class SaveDraftCommand(ConfigurationServiceModel):
    expected_version: int = Field(ge=0, le=MAX_POSTGRES_BIGINT)
    configuration: SearchConfiguration
    actor: Actor
    timestamp: datetime


class DraftSaved(ConfigurationServiceModel):
    kind: Literal["saved"] = "saved"
    draft: SearchConfigurationDraft


class DraftChanged(ConfigurationServiceModel):
    kind: Literal["changed"] = "changed"
    current_draft: SearchConfigurationDraft


DraftSaveResult = Annotated[DraftSaved | DraftChanged, Field(discriminator="kind")]


class PublishConfigurationCommand(ConfigurationServiceModel):
    idempotency_key: IdempotencyKey
    expected_draft_version: int = Field(ge=0, le=MAX_POSTGRES_BIGINT)
    expected_configuration_revision_id: ConfigurationRevisionId
    actor: Actor
    timestamp: datetime


class ConfigurationPublished(ConfigurationServiceModel):
    kind: Literal["published"] = "published"
    replayed: bool
    publication: SearchConfigurationPublication
    rebased_draft: SearchConfigurationDraft

    @model_validator(mode="after")
    def publication_matches_draft(self) -> ConfigurationPublished:
        if (
            self.publication.revision_id != self.rebased_draft.base_revision_id
            or search_configuration_revision_id(self.rebased_draft.configuration)
            != self.publication.revision_id
        ):
            raise ValueError("Published configuration and rebased draft differ")
        return self


class PublishDraftChanged(ConfigurationServiceModel):
    kind: Literal["draft_changed"] = "draft_changed"
    replayed: bool
    expected_draft_version: int = Field(ge=0)
    expected_configuration_revision_id: ConfigurationRevisionId
    observed_draft_version: int = Field(ge=0)
    observed_configuration_revision_id: ConfigurationRevisionId

    @model_validator(mode="after")
    def state_changed(self) -> PublishDraftChanged:
        if (
            self.expected_draft_version == self.observed_draft_version
            and self.expected_configuration_revision_id == self.observed_configuration_revision_id
        ):
            raise ValueError("Expected and observed draft state must differ")
        return self


class PublicationIdempotencyKeyConflict(ConfigurationServiceModel):
    kind: Literal["idempotency_key_conflict"] = "idempotency_key_conflict"
    idempotency_key: IdempotencyKey


PublishConfigurationResult = Annotated[
    ConfigurationPublished | PublishDraftChanged | PublicationIdempotencyKeyConflict,
    Field(discriminator="kind"),
]


class ActivateConfigurationCommand(ConfigurationServiceModel):
    target_revision_id: ConfigurationRevisionId
    expected_active_revision_id: ConfigurationRevisionId
    expected_generation: int = Field(ge=0, le=MAX_POSTGRES_BIGINT)
    actor: Actor
    timestamp: datetime


class PublishedActiveSearchConfiguration(ConfigurationServiceModel):
    active: ActiveSearchConfiguration
    publication: SearchConfigurationPublication

    @model_validator(mode="after")
    def revision_ids_match(self) -> PublishedActiveSearchConfiguration:
        if (
            self.active.revision.id != self.publication.revision_id
            or search_configuration_revision_id(self.active.revision.configuration)
            != self.active.revision.id
        ):
            raise ValueError("Active configuration and publication revision IDs differ")
        return self


class ConfigurationActivated(ConfigurationServiceModel):
    kind: Literal["activated"] = "activated"
    active_configuration: PublishedActiveSearchConfiguration


class ActivationTargetUnpublished(ConfigurationServiceModel):
    kind: Literal["unpublished_target"] = "unpublished_target"
    target_revision_id: ConfigurationRevisionId


class ActiveConfigurationChanged(ConfigurationServiceModel):
    kind: Literal["active_changed"] = "active_changed"
    active_configuration: PublishedActiveSearchConfiguration


ActivateConfigurationResult = Annotated[
    ConfigurationActivated | ActivationTargetUnpublished | ActiveConfigurationChanged,
    Field(discriminator="kind"),
]


class _PublicationReceipt(ConfigurationServiceModel):
    outcome: Literal["published", "draft_changed"]
    expected_draft_version: int = Field(ge=0, le=MAX_POSTGRES_BIGINT)
    expected_configuration_revision_id: ConfigurationRevisionId
    actor: Actor
    requested_at: datetime
    observed_draft_version: int | None = Field(default=None, ge=0)
    observed_configuration_revision_id: ConfigurationRevisionId | None = None
    publication_revision_id: ConfigurationRevisionId | None = None
    rebased_draft_version: int | None = Field(default=None, ge=1)


def validate_search_configuration(
    candidate: object,
    *,
    issue_limit: int = DEFAULT_RESULT_LIMIT,
) -> ConfigurationValidationResult:
    _require_result_limit(issue_limit)
    try:
        configuration = SearchConfiguration.model_validate(candidate)
    except ValidationError as error:
        errors = error.errors(include_url=False, include_context=False, include_input=False)
        issues = tuple(
            ConfigurationValidationIssue(
                location=_validation_issue_location(item),
                message=item["msg"],
                error_code=item["type"],
            )
            for item in errors[:issue_limit]
        )
        return ConfigurationInvalid(
            issues=issues,
            total_issue_count=len(errors),
            omitted_issue_count=len(errors) - len(issues),
        )
    return ConfigurationValid(configuration=configuration)


def preview_search_configuration(
    configuration: SearchConfiguration,
    *,
    search_sample_limit: int = DEFAULT_RESULT_LIMIT,
    prompt_summary_limit: int = DEFAULT_RESULT_LIMIT,
) -> ConfigurationPreview:
    _require_result_limit(search_sample_limit)
    _require_result_limit(prompt_summary_limit)
    searches = tuple(
        f"site:{SEARCH_SOURCE_DOMAINS[source]} {keyword}"
        for keyword in configuration.search_keywords
        for source in configuration.enabled_sources
    )
    release = build_prompt_release(configuration)
    return ConfigurationPreview(
        configuration_revision_id=search_configuration_revision_id(configuration),
        prompt_release_id=release.id,
        prompt_release_name=release.name,
        total_generated_search_count=len(searches),
        search_samples=searches[:search_sample_limit],
        total_compiled_prompt_count=len(release.versions),
        prompt_summaries=tuple(
            PromptSummary(
                name=version.definition.name,
                criterion=version.definition.criterion,
                phase=version.definition.phase,
                prompt_version_id=version.id,
            )
            for version in release.versions[:prompt_summary_limit]
        ),
    )


def save_search_configuration_draft(
    connection: Connection,
    command: SaveDraftCommand,
) -> DraftSaveResult:
    current = load_search_configuration_draft(connection)
    if current.version != command.expected_version:
        return DraftChanged(current_draft=current)
    saved = replace_search_configuration_draft(
        connection,
        expected_version=command.expected_version,
        base_revision_id=current.base_revision_id,
        configuration=command.configuration,
        updated_at=command.timestamp,
        updated_by=command.actor,
    )
    if saved is not None:
        return DraftSaved(draft=saved)
    return DraftChanged(current_draft=load_search_configuration_draft(connection))


def publish_search_configuration(
    connection: Connection,
    command: PublishConfigurationCommand,
) -> PublishConfigurationResult:
    if not connection.autocommit:
        raise ValueError("Configuration publication requires an autocommit connection")
    result: PublishConfigurationResult
    with connection.transaction():
        _ = connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext('search_configuration_publication'), hashtext(%s))",
            (command.idempotency_key,),
        ).fetchone()
        receipt = _load_publication_receipt(connection, command.idempotency_key)
        if receipt is not None:
            if not _publication_request_matches(receipt, command):
                result = PublicationIdempotencyKeyConflict(
                    idempotency_key=command.idempotency_key,
                )
            else:
                result = _publication_result_from_receipt(connection, receipt, replayed=True)
        else:
            draft = _load_locked_draft(connection)
            observed_revision_id = search_configuration_revision_id(draft.configuration)
            if (
                draft.version != command.expected_draft_version
                or observed_revision_id != command.expected_configuration_revision_id
            ):
                _insert_draft_changed_receipt(
                    connection,
                    command,
                    draft.version,
                    observed_revision_id,
                )
                stored_receipt = _load_publication_receipt(connection, command.idempotency_key)
                if stored_receipt is None:
                    raise SearchConfigurationError("Stored publication receipt is missing")
                result = _publication_result_from_receipt(
                    connection, stored_receipt, replayed=False
                )
            else:
                revision = build_search_configuration_revision(
                    draft.configuration,
                    created_at=command.timestamp,
                    created_by=command.actor,
                )
                stored_revision = store_search_configuration_revision(connection, revision)
                release = store_prompt_release(
                    connection,
                    build_prompt_release(stored_revision.configuration),
                    created_at=command.timestamp,
                    created_by=command.actor,
                )
                _ = connection.execute(
                    """
                    INSERT INTO search_configuration_publications (
                      revision_id, prompt_release_id, published_at, published_by
                    ) VALUES (%s, %s, %s, %s)
                    ON CONFLICT (revision_id) DO NOTHING
                    """,
                    (stored_revision.id, release.id, command.timestamp, command.actor),
                )
                publication = load_search_configuration_publication(connection, stored_revision.id)
                if publication.prompt_release_id != release.id:
                    raise SearchConfigurationError(
                        f"Stored publication differs from revision {stored_revision.id}"
                    )
                rebased_draft = _rebase_locked_draft(
                    connection,
                    draft,
                    stored_revision.id,
                    command,
                )
                _ = connection.execute(
                    """
                    INSERT INTO search_configuration_publication_receipts (
                      idempotency_key, outcome,
                      expected_draft_version, expected_configuration_revision_id,
                      actor, requested_at, publication_revision_id, rebased_draft_version
                    ) VALUES (%s, 'published', %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        command.idempotency_key,
                        command.expected_draft_version,
                        command.expected_configuration_revision_id,
                        command.actor,
                        command.timestamp,
                        publication.revision_id,
                        rebased_draft.version,
                    ),
                )
                stored_receipt = _load_publication_receipt(connection, command.idempotency_key)
                if stored_receipt is None:
                    raise SearchConfigurationError("Stored publication receipt is missing")
                result = _publication_result_from_receipt(
                    connection, stored_receipt, replayed=False
                )
    return result


def load_published_active_search_configuration(
    connection: Connection,
) -> PublishedActiveSearchConfiguration:
    active = load_active_search_configuration(connection)
    publication = load_search_configuration_publication(connection, active.revision.id)
    return PublishedActiveSearchConfiguration(active=active, publication=publication)


def activate_search_configuration(
    connection: Connection,
    command: ActivateConfigurationCommand,
) -> ActivateConfigurationResult:
    try:
        publication = load_search_configuration_publication(connection, command.target_revision_id)
    except SearchConfigurationError:
        return ActivationTargetUnpublished(target_revision_id=command.target_revision_id)
    active = compare_and_swap_active_search_configuration(
        connection,
        expected_revision_id=command.expected_active_revision_id,
        expected_generation=command.expected_generation,
        revision_id=command.target_revision_id,
        activated_at=command.timestamp,
        activated_by=command.actor,
    )
    if active is None:
        return ActiveConfigurationChanged(
            active_configuration=load_published_active_search_configuration(connection)
        )
    return ConfigurationActivated(
        active_configuration=PublishedActiveSearchConfiguration(
            active=active, publication=publication
        )
    )


def _load_publication_receipt(
    connection: Connection,
    idempotency_key: str,
) -> _PublicationReceipt | None:
    row = connection.execute(
        """
        SELECT outcome, expected_draft_version, expected_configuration_revision_id,
               actor, requested_at,
               observed_draft_version, observed_configuration_revision_id,
               publication_revision_id, rebased_draft_version
        FROM search_configuration_publication_receipts
        WHERE idempotency_key = %s
        """,
        (idempotency_key,),
    ).fetchone()
    if row is None:
        return None
    return _PublicationReceipt.model_validate(
        {
            "outcome": row[0],
            "expected_draft_version": row[1],
            "expected_configuration_revision_id": row[2],
            "actor": row[3],
            "requested_at": row[4],
            "observed_draft_version": row[5],
            "observed_configuration_revision_id": row[6],
            "publication_revision_id": row[7],
            "rebased_draft_version": row[8],
        }
    )


def _publication_request_matches(
    receipt: _PublicationReceipt,
    command: PublishConfigurationCommand,
) -> bool:
    return (
        receipt.expected_draft_version == command.expected_draft_version
        and receipt.expected_configuration_revision_id == command.expected_configuration_revision_id
        and receipt.actor == command.actor
    )


def _publication_result_from_receipt(
    connection: Connection,
    receipt: _PublicationReceipt,
    *,
    replayed: bool,
) -> ConfigurationPublished | PublishDraftChanged:
    if receipt.outcome == "draft_changed":
        if (
            receipt.observed_draft_version is None
            or receipt.observed_configuration_revision_id is None
        ):
            raise SearchConfigurationError("Stored draft conflict receipt is incomplete")
        return PublishDraftChanged(
            replayed=replayed,
            expected_draft_version=receipt.expected_draft_version,
            expected_configuration_revision_id=receipt.expected_configuration_revision_id,
            observed_draft_version=receipt.observed_draft_version,
            observed_configuration_revision_id=receipt.observed_configuration_revision_id,
        )
    if receipt.publication_revision_id is None or receipt.rebased_draft_version is None:
        raise SearchConfigurationError("Stored publication receipt is incomplete")
    revision_id = receipt.publication_revision_id
    publication = load_search_configuration_publication(connection, revision_id)
    revision = load_search_configuration_revision(connection, revision_id)
    return ConfigurationPublished(
        replayed=replayed,
        publication=publication,
        rebased_draft=SearchConfigurationDraft(
            base_revision_id=revision_id,
            version=receipt.rebased_draft_version,
            configuration=revision.configuration,
            updated_at=receipt.requested_at,
            updated_by=receipt.actor,
        ),
    )


def _load_locked_draft(connection: Connection) -> SearchConfigurationDraft:
    row = connection.execute(
        """
        SELECT base_revision_id, version, content, updated_at, updated_by
        FROM search_configuration_drafts
        WHERE singleton_id = 1
        FOR UPDATE
        """
    ).fetchone()
    if row is None:
        raise SearchConfigurationError("Search configuration draft is missing")
    return SearchConfigurationDraft.model_validate(
        {
            "base_revision_id": row[0],
            "version": row[1],
            "configuration": row[2],
            "updated_at": row[3],
            "updated_by": row[4],
        }
    )


def _insert_draft_changed_receipt(
    connection: Connection,
    command: PublishConfigurationCommand,
    observed_draft_version: int,
    observed_revision_id: SearchConfigurationRevisionId,
) -> None:
    _ = connection.execute(
        """
        INSERT INTO search_configuration_publication_receipts (
          idempotency_key, outcome,
          expected_draft_version, expected_configuration_revision_id,
          actor, requested_at, observed_draft_version,
          observed_configuration_revision_id
        ) VALUES (%s, 'draft_changed', %s, %s, %s, %s, %s, %s)
        """,
        (
            command.idempotency_key,
            command.expected_draft_version,
            command.expected_configuration_revision_id,
            command.actor,
            command.timestamp,
            observed_draft_version,
            observed_revision_id,
        ),
    )


def _rebase_locked_draft(
    connection: Connection,
    draft: SearchConfigurationDraft,
    revision_id: SearchConfigurationRevisionId,
    command: PublishConfigurationCommand,
) -> SearchConfigurationDraft:
    row = connection.execute(
        """
        UPDATE search_configuration_drafts
        SET base_revision_id = %s, content = %s, version = version + 1,
            updated_at = %s, updated_by = %s
        WHERE singleton_id = 1 AND version = %s
        RETURNING base_revision_id, version, content, updated_at, updated_by
        """,
        (
            revision_id,
            Jsonb(draft.configuration.model_dump(mode="json")),
            command.timestamp,
            command.actor,
            draft.version,
        ),
    ).fetchone()
    if row is None:
        raise SearchConfigurationError("Locked search configuration draft changed unexpectedly")
    return SearchConfigurationDraft.model_validate(
        {
            "base_revision_id": row[0],
            "version": row[1],
            "configuration": row[2],
            "updated_at": row[3],
            "updated_by": row[4],
        }
    )


def _require_result_limit(limit: int) -> None:
    if isinstance(limit, bool) or not MIN_RESULT_LIMIT <= limit <= MAX_RESULT_LIMIT:
        raise ValueError(f"Result limit must be between {MIN_RESULT_LIMIT} and {MAX_RESULT_LIMIT}")


def _validation_issue_location(error: Mapping[str, object]) -> tuple[str | int, ...]:
    location = cast(tuple[str | int, ...], error["loc"])
    if error["type"] == "extra_forbidden":
        return (*location[:-1], "<extra>")
    return location
