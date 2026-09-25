from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Annotated, ClassVar, Literal, NewType, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from job_finder.discovery.catalog import (
    SEARCH_SOURCE_DOMAINS,
    SupportedSearchSource,
)

AcquisitionPolicyRevisionId = NewType("AcquisitionPolicyRevisionId", str)
_BOUNDARY_WHITESPACE = " \t\n\r\f\v"
_ASCII_LOWER_TRANSLATION = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


class SearchQuerySource(Protocol):
    search_keywords: tuple[str, ...]
    enabled_sources: tuple[SupportedSearchSource, ...]


@dataclass(frozen=True, slots=True)
class SearchQuery:
    keyword: str
    domain: str

    @property
    def text(self) -> str:
        return f"site:{self.domain} {self.keyword}"


def build_search_queries(source: SearchQuerySource) -> tuple[SearchQuery, ...]:
    return tuple(
        SearchQuery(keyword=keyword, domain=SEARCH_SOURCE_DOMAINS[search_source])
        for keyword in source.search_keywords
        for search_source in source.enabled_sources
    )


def validate_search_keywords(values: tuple[str, ...]) -> tuple[str, ...]:
    for value in values:
        _require_database_safe_text(value)
    if any(
        not value or value != value.strip(_BOUNDARY_WHITESPACE) or len(value) > 200
        for value in values
    ):
        raise ValueError("Search keywords must be non-empty, trimmed, and at most 200 characters")
    if len({value.translate(_ASCII_LOWER_TRANSLATION) for value in values}) != len(values):
        raise ValueError("Search keywords must be unique")
    return values


def validate_enabled_sources(
    values: tuple[SupportedSearchSource, ...],
) -> tuple[SupportedSearchSource, ...]:
    if len(set(values)) != len(values):
        raise ValueError("Enabled sources must be unique")
    return values


class AcquisitionPolicy(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    search_keywords: Annotated[tuple[str, ...], Field(min_length=1, max_length=100)]
    enabled_sources: Annotated[tuple[SupportedSearchSource, ...], Field(min_length=1, max_length=4)]

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


def acquisition_policy_revision_id(policy: AcquisitionPolicy) -> AcquisitionPolicyRevisionId:
    content = json.dumps(
        policy.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return AcquisitionPolicyRevisionId(hashlib.sha256(content.encode()).hexdigest())


def _require_database_safe_text(value: str) -> None:
    if "\x00" in value or any("\ud800" <= character <= "\udfff" for character in value):
        raise ValueError("Configuration text contains a character PostgreSQL cannot store")
