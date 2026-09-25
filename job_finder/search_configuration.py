from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Annotated, ClassVar, Literal, NewType

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, field_validator

from job_finder.acquisition_policy import (
    SearchQuery as SearchQuery,
    build_search_queries as build_search_queries,
    validate_enabled_sources,
    validate_search_keywords,
)
from job_finder.discovery.catalog import (
    SEARCH_KEYWORDS,
    SEARCH_SOURCE_DOMAINS as SEARCH_SOURCE_DOMAINS,
    SupportedSearchSource as SupportedSearchSource,
)
from job_finder.evaluation.models import PromptReleaseId
from job_finder.evaluation.prompts import EVALUATION_PROMPTS
from job_finder.qualification_definition import (
    PersonalCriterion as PersonalCriterion,
    TargetProfile as TargetProfile,
    validate_personal_criteria,
    validate_target_profiles,
)

Connection = psycopg.Connection[tuple[object, ...]]
SearchConfigurationRevisionId = NewType("SearchConfigurationRevisionId", str)


class SearchConfigurationError(RuntimeError):
    pass


class SearchConfigurationRevisionNotFound(SearchConfigurationError):
    pass


class SearchConfigurationPublicationNotFound(SearchConfigurationError):
    pass


class SearchConfigurationModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class SearchConfiguration(SearchConfigurationModel):
    schema_version: Literal[1] = 1
    search_keywords: Annotated[tuple[str, ...], Field(min_length=1, max_length=100)]
    enabled_sources: Annotated[tuple[SupportedSearchSource, ...], Field(min_length=1, max_length=4)]
    personal_criteria: Annotated[tuple[PersonalCriterion, ...], Field(min_length=1, max_length=20)]
    target_profiles: Annotated[tuple[TargetProfile, ...], Field(min_length=1, max_length=20)]

    @field_validator("search_keywords")
    @classmethod
    def keywords_are_clean_and_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return validate_search_keywords(values)

    @field_validator("enabled_sources")
    @classmethod
    def sources_are_unique(
        cls, values: tuple[SupportedSearchSource, ...]
    ) -> tuple[SupportedSearchSource, ...]:
        return validate_enabled_sources(values)

    @field_validator("personal_criteria")
    @classmethod
    def criterion_keys_are_unique(
        cls, values: tuple[PersonalCriterion, ...]
    ) -> tuple[PersonalCriterion, ...]:
        return validate_personal_criteria(values)

    @field_validator("target_profiles")
    @classmethod
    def profile_keys_are_unique(
        cls, values: tuple[TargetProfile, ...]
    ) -> tuple[TargetProfile, ...]:
        return validate_target_profiles(values)


class SearchConfigurationRevision(SearchConfigurationModel):
    id: Annotated[SearchConfigurationRevisionId, Field(pattern=r"^[0-9a-f]{64}$")]
    configuration: SearchConfiguration
    created_at: datetime
    created_by: str = Field(min_length=1)


class SearchConfigurationPublication(SearchConfigurationModel):
    revision_id: Annotated[SearchConfigurationRevisionId, Field(pattern=r"^[0-9a-f]{64}$")]
    prompt_release_id: Annotated[PromptReleaseId, Field(pattern=r"^[0-9a-f]{64}$")]
    published_at: datetime
    published_by: str = Field(min_length=1)


class SearchConfigurationDraft(SearchConfigurationModel):
    base_revision_id: Annotated[SearchConfigurationRevisionId, Field(pattern=r"^[0-9a-f]{64}$")]
    version: int = Field(ge=0)
    configuration: SearchConfiguration
    updated_at: datetime
    updated_by: str = Field(min_length=1)


class ActiveSearchConfiguration(SearchConfigurationModel):
    generation: int = Field(ge=0)
    revision: SearchConfigurationRevision
    activated_at: datetime
    activated_by: str = Field(min_length=1)


_CRITERION_NAMES = {
    "remote-europe-eligible": "Location eligibility",
    "compensation-minimum": "Compensation minimum",
    "role-quality": "Role quality",
    "cheap-shop-placement": "Company quality",
}
_PROFILE_NAMES = {
    "early-stage-product-engineer": "Early-stage product engineer",
    "applied-ai-product-engineer": "Applied AI product engineer",
}
_DEFAULT_CRITERIA = tuple(prompt for prompt in EVALUATION_PROMPTS if prompt.phase == "filter")
_DEFAULT_PROFILES = tuple(prompt for prompt in EVALUATION_PROMPTS if prompt.phase == "profile")

DEFAULT_SEARCH_CONFIGURATION = SearchConfiguration(
    search_keywords=SEARCH_KEYWORDS,
    enabled_sources=tuple(SEARCH_SOURCE_DOMAINS),
    personal_criteria=tuple(
        PersonalCriterion(
            key=prompt.criterion,
            name=_CRITERION_NAMES[prompt.criterion],
            instructions=prompt.system_message,
        )
        for prompt in _DEFAULT_CRITERIA
    ),
    target_profiles=tuple(
        TargetProfile(
            key=prompt.criterion,
            name=_PROFILE_NAMES[prompt.criterion],
            instructions=prompt.system_message,
        )
        for prompt in _DEFAULT_PROFILES
    ),
)


def search_configuration_revision_id(
    configuration: SearchConfiguration,
) -> SearchConfigurationRevisionId:
    return SearchConfigurationRevisionId(
        hashlib.sha256(_canonical_configuration(configuration).encode()).hexdigest()
    )


def build_search_configuration_revision(
    configuration: SearchConfiguration,
    *,
    created_at: datetime,
    created_by: str,
) -> SearchConfigurationRevision:
    return SearchConfigurationRevision(
        id=search_configuration_revision_id(configuration),
        configuration=configuration,
        created_at=created_at,
        created_by=created_by,
    )


def load_search_configuration_revision(
    connection: Connection,
    revision_id: SearchConfigurationRevisionId,
) -> SearchConfigurationRevision:
    row = connection.execute(
        """
        SELECT content, created_at, created_by
        FROM search_configuration_revisions
        WHERE id = %s
        """,
        (revision_id,),
    ).fetchone()
    if row is None:
        raise SearchConfigurationRevisionNotFound(
            f"Search configuration revision not found: {revision_id}"
        )
    revision = SearchConfigurationRevision.model_validate(
        {
            "id": revision_id,
            "configuration": row[0],
            "created_at": row[1],
            "created_by": row[2],
        }
    )
    if search_configuration_revision_id(revision.configuration) != revision.id:
        raise SearchConfigurationError(f"Search configuration revision is corrupt: {revision_id}")
    return revision


def store_search_configuration_revision(
    connection: Connection,
    revision: SearchConfigurationRevision,
) -> SearchConfigurationRevision:
    _require_autocommit(connection)
    expected_id = search_configuration_revision_id(revision.configuration)
    if revision.id != expected_id:
        raise ValueError("Search configuration revision ID does not match its content")
    with connection.transaction():
        _ = connection.execute(
            """
            INSERT INTO search_configuration_revisions (
              id, content, created_at, created_by
            ) VALUES (%s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (
                revision.id,
                Jsonb(revision.configuration.model_dump(mode="json")),
                revision.created_at,
                revision.created_by,
            ),
        )
    stored = load_search_configuration_revision(connection, revision.id)
    if stored.configuration != revision.configuration:
        raise SearchConfigurationError(
            f"Stored search configuration differs from revision {revision.id}"
        )
    return stored


def load_search_configuration_publication(
    connection: Connection,
    revision_id: SearchConfigurationRevisionId,
) -> SearchConfigurationPublication:
    row = connection.execute(
        """
        SELECT prompt_release_id, published_at, published_by
        FROM search_configuration_publications
        WHERE revision_id = %s
        """,
        (revision_id,),
    ).fetchone()
    if row is None:
        raise SearchConfigurationPublicationNotFound(
            f"Search configuration publication not found: {revision_id}"
        )
    return SearchConfigurationPublication.model_validate(
        {
            "revision_id": revision_id,
            "prompt_release_id": row[0],
            "published_at": row[1],
            "published_by": row[2],
        }
    )


def load_search_configuration_draft(connection: Connection) -> SearchConfigurationDraft:
    row = connection.execute(
        """
        SELECT base_revision_id, version, content, updated_at, updated_by
        FROM search_configuration_drafts
        WHERE singleton_id = 1
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


def replace_search_configuration_draft(
    connection: Connection,
    *,
    expected_version: int,
    base_revision_id: SearchConfigurationRevisionId,
    configuration: SearchConfiguration,
    updated_at: datetime,
    updated_by: str,
) -> SearchConfigurationDraft | None:
    _require_autocommit(connection)
    with connection.transaction():
        row = connection.execute(
            """
            UPDATE search_configuration_drafts
            SET base_revision_id = %s, content = %s, version = version + 1,
                updated_at = %s, updated_by = %s
            WHERE singleton_id = 1 AND version = %s
            RETURNING base_revision_id, version, content, updated_at, updated_by
            """,
            (
                base_revision_id,
                Jsonb(configuration.model_dump(mode="json")),
                updated_at,
                updated_by,
                expected_version,
            ),
        ).fetchone()
    if row is None:
        return None
    return SearchConfigurationDraft.model_validate(
        {
            "base_revision_id": row[0],
            "version": row[1],
            "configuration": row[2],
            "updated_at": row[3],
            "updated_by": row[4],
        }
    )


def load_active_search_configuration(connection: Connection) -> ActiveSearchConfiguration:
    row = connection.execute(
        """
        SELECT a.generation, a.activated_at, a.activated_by,
               r.id, r.content, r.created_at, r.created_by
        FROM active_search_configuration a
        JOIN search_configuration_revisions r ON r.id = a.revision_id
        WHERE a.singleton_id = 1
        """
    ).fetchone()
    if row is None:
        raise SearchConfigurationError("Active search configuration is missing")
    revision = SearchConfigurationRevision.model_validate(
        {
            "id": row[3],
            "configuration": row[4],
            "created_at": row[5],
            "created_by": row[6],
        }
    )
    if search_configuration_revision_id(revision.configuration) != revision.id:
        raise SearchConfigurationError(f"Search configuration revision is corrupt: {revision.id}")
    return ActiveSearchConfiguration(
        generation=int(str(row[0])),
        revision=revision,
        activated_at=datetime.fromisoformat(str(row[1])),
        activated_by=str(row[2]),
    )


def compare_and_swap_active_search_configuration(
    connection: Connection,
    *,
    expected_revision_id: SearchConfigurationRevisionId,
    expected_generation: int,
    revision_id: SearchConfigurationRevisionId,
    activated_at: datetime,
    activated_by: str,
) -> ActiveSearchConfiguration | None:
    _require_autocommit(connection)
    with connection.transaction():
        row = connection.execute(
            """
            WITH activated AS (
              UPDATE active_search_configuration
              SET revision_id = %s, generation = generation + 1,
                  activated_at = %s, activated_by = %s
              WHERE singleton_id = 1 AND revision_id = %s AND generation = %s
              RETURNING generation, revision_id, activated_at, activated_by
            )
            SELECT a.generation, a.activated_at, a.activated_by,
                   r.id, r.content, r.created_at, r.created_by
            FROM activated a
            JOIN search_configuration_revisions r ON r.id = a.revision_id
            """,
            (
                revision_id,
                activated_at,
                activated_by,
                expected_revision_id,
                expected_generation,
            ),
        ).fetchone()
    if row is None:
        return None
    return ActiveSearchConfiguration.model_validate(
        {
            "generation": row[0],
            "activated_at": row[1],
            "activated_by": row[2],
            "revision": {
                "id": row[3],
                "configuration": row[4],
                "created_at": row[5],
                "created_by": row[6],
            },
        }
    )


def _canonical_configuration(configuration: SearchConfiguration) -> str:
    return json.dumps(
        configuration.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _require_autocommit(connection: Connection) -> None:
    if not connection.autocommit:
        raise ValueError("Search configuration operations require an autocommit connection")
