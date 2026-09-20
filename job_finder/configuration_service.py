from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, ClassVar, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from job_finder.evaluation.models import PromptReleaseId, PromptVersionId
from job_finder.evaluation.prompt_releases import build_prompt_release
from job_finder.evaluation.prompts import PromptPhase
from job_finder.search_configuration import (
    SEARCH_SOURCE_DOMAINS,
    Connection,
    SearchConfiguration,
    SearchConfigurationDraft,
    SearchConfigurationRevisionId,
    load_search_configuration_draft,
    replace_search_configuration_draft,
    search_configuration_revision_id,
)

MIN_RESULT_LIMIT = 1
MAX_RESULT_LIMIT = 100
DEFAULT_RESULT_LIMIT = 20


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
    expected_version: int = Field(ge=0)
    configuration: SearchConfiguration
    actor: str = Field(min_length=1)
    timestamp: datetime


class DraftSaved(ConfigurationServiceModel):
    kind: Literal["saved"] = "saved"
    draft: SearchConfigurationDraft


class DraftChanged(ConfigurationServiceModel):
    kind: Literal["changed"] = "changed"
    current_draft: SearchConfigurationDraft


DraftSaveResult = Annotated[DraftSaved | DraftChanged, Field(discriminator="kind")]


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


def _require_result_limit(limit: int) -> None:
    if isinstance(limit, bool) or not MIN_RESULT_LIMIT <= limit <= MAX_RESULT_LIMIT:
        raise ValueError(f"Result limit must be between {MIN_RESULT_LIMIT} and {MAX_RESULT_LIMIT}")


def _validation_issue_location(error: Mapping[str, object]) -> tuple[str | int, ...]:
    location = cast(tuple[str | int, ...], error["loc"])
    if error["type"] == "extra_forbidden":
        return (*location[:-1], "<extra>")
    return location
