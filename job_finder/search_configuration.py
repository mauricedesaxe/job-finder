from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Annotated, ClassVar, Literal, NewType, Self

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from job_finder.discovery.catalog import SEARCH_DOMAINS, SEARCH_KEYWORDS
from job_finder.evaluation.prompts import EVALUATION_PROMPTS

Connection = psycopg.Connection[tuple[object, ...]]
SearchConfigurationRevisionId = NewType("SearchConfigurationRevisionId", str)
_BOUNDARY_WHITESPACE = " \t\n\r\f\v"
_ASCII_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_ASCII_LOWER = "abcdefghijklmnopqrstuvwxyz"
_ASCII_LOWER_TRANSLATION = str.maketrans(_ASCII_UPPER, _ASCII_LOWER)
ConfigurationKey = Annotated[
    str,
    Field(min_length=1, max_length=100, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$"),
]


class SupportedSearchSource(StrEnum):
    ASHBY = "ashby"
    LEVER = "lever"
    GREENHOUSE = "greenhouse"
    WORKABLE = "workable"


SEARCH_SOURCE_DOMAINS: dict[SupportedSearchSource, str] = {
    SupportedSearchSource.ASHBY: "jobs.ashbyhq.com",
    SupportedSearchSource.LEVER: "jobs.lever.co",
    SupportedSearchSource.GREENHOUSE: "boards.greenhouse.io",
    SupportedSearchSource.WORKABLE: "apply.workable.com",
}


def _require_database_safe_text(value: str) -> None:
    if "\x00" in value or any("\ud800" <= character <= "\udfff" for character in value):
        raise ValueError("Configuration text contains a character PostgreSQL cannot store")


class SearchConfigurationError(RuntimeError):
    pass


class SearchConfigurationModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class PersonalCriterion(SearchConfigurationModel):
    key: ConfigurationKey
    name: str = Field(min_length=1, max_length=100)
    instructions: str = Field(min_length=1, max_length=20_000)

    @field_validator("name", "instructions")
    @classmethod
    def text_is_trimmed(cls, value: str) -> str:
        _require_database_safe_text(value)
        if value != value.strip(_BOUNDARY_WHITESPACE):
            raise ValueError("Configuration text cannot start or end with whitespace")
        return value


class TargetProfile(SearchConfigurationModel):
    key: ConfigurationKey
    name: str = Field(min_length=1, max_length=100)
    instructions: str = Field(min_length=1, max_length=20_000)

    @field_validator("name", "instructions")
    @classmethod
    def text_is_trimmed(cls, value: str) -> str:
        _require_database_safe_text(value)
        if value != value.strip(_BOUNDARY_WHITESPACE):
            raise ValueError("Configuration text cannot start or end with whitespace")
        return value


class SearchConfiguration(SearchConfigurationModel):
    schema_version: Literal[1] = 1
    search_keywords: Annotated[tuple[str, ...], Field(min_length=1, max_length=100)]
    enabled_sources: Annotated[tuple[SupportedSearchSource, ...], Field(min_length=1, max_length=4)]
    personal_criteria: Annotated[tuple[PersonalCriterion, ...], Field(min_length=1, max_length=20)]
    target_profiles: Annotated[tuple[TargetProfile, ...], Field(min_length=1, max_length=20)]

    @field_validator("search_keywords")
    @classmethod
    def keywords_are_clean_and_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            _require_database_safe_text(value)
        if any(
            not value or value != value.strip(_BOUNDARY_WHITESPACE) or len(value) > 200
            for value in values
        ):
            raise ValueError(
                "Search keywords must be non-empty, trimmed, and at most 200 characters"
            )
        if len({value.translate(_ASCII_LOWER_TRANSLATION) for value in values}) != len(values):
            raise ValueError("Search keywords must be unique")
        return values

    @model_validator(mode="after")
    def members_are_unique(self) -> Self:
        if len(set(self.enabled_sources)) != len(self.enabled_sources):
            raise ValueError("Enabled sources must be unique")
        if len({criterion.key for criterion in self.personal_criteria}) != len(
            self.personal_criteria
        ):
            raise ValueError("Personal criterion keys must be unique")
        if len({profile.key for profile in self.target_profiles}) != len(self.target_profiles):
            raise ValueError("Target profile keys must be unique")
        return self


class SearchConfigurationRevision(SearchConfigurationModel):
    id: Annotated[SearchConfigurationRevisionId, Field(pattern=r"^[0-9a-f]{64}$")]
    configuration: SearchConfiguration
    created_at: datetime
    created_by: str = Field(min_length=1)


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


_SOURCE_BY_DOMAIN = {domain: source for source, domain in SEARCH_SOURCE_DOMAINS.items()}
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
    enabled_sources=tuple(_SOURCE_BY_DOMAIN[domain] for domain in SEARCH_DOMAINS),
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
        raise SearchConfigurationError(f"Search configuration revision not found: {revision_id}")
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
