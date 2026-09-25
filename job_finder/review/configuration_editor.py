from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from job_finder.configuration_service import (
    ActivateConfigurationCommand,
    ActivateConfigurationResult,
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
from job_finder.database import ConnectionFactory
from job_finder.search_configuration import (
    SearchConfiguration,
    SearchConfigurationDraft,
    SearchConfigurationPublication,
    SearchConfigurationRevisionId,
    search_configuration_revision_id,
)


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
