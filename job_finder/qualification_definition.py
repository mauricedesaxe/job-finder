from __future__ import annotations

import hashlib
import json
from typing import Annotated, ClassVar, Literal, NewType

from pydantic import BaseModel, ConfigDict, Field, field_validator

QualificationDefinitionRevisionId = NewType("QualificationDefinitionRevisionId", str)
_BOUNDARY_WHITESPACE = " \t\n\r\f\v"
ConfigurationKey = Annotated[
    str,
    Field(min_length=1, max_length=100, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$"),
]


class QualificationDefinitionModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class PersonalCriterion(QualificationDefinitionModel):
    key: ConfigurationKey
    name: str = Field(min_length=1, max_length=100)
    instructions: str = Field(min_length=1, max_length=20_000)

    @field_validator("name", "instructions")
    @classmethod
    def text_is_trimmed(cls, value: str) -> str:
        return validate_definition_text(value)


class TargetProfile(QualificationDefinitionModel):
    key: ConfigurationKey
    name: str = Field(min_length=1, max_length=100)
    instructions: str = Field(min_length=1, max_length=20_000)

    @field_validator("name", "instructions")
    @classmethod
    def text_is_trimmed(cls, value: str) -> str:
        return validate_definition_text(value)


def validate_definition_text(value: str) -> str:
    if "\x00" in value or any("\ud800" <= character <= "\udfff" for character in value):
        raise ValueError("Configuration text contains a character PostgreSQL cannot store")
    if value != value.strip(_BOUNDARY_WHITESPACE):
        raise ValueError("Configuration text cannot start or end with whitespace")
    return value


def validate_personal_criteria(
    values: tuple[PersonalCriterion, ...],
) -> tuple[PersonalCriterion, ...]:
    if len({criterion.key for criterion in values}) != len(values):
        raise ValueError("Personal criterion keys must be unique")
    return values


def validate_target_profiles(values: tuple[TargetProfile, ...]) -> tuple[TargetProfile, ...]:
    if len({profile.key for profile in values}) != len(values):
        raise ValueError("Target profile keys must be unique")
    return values


class QualificationDefinition(QualificationDefinitionModel):
    schema_version: Literal[1] = 1
    personal_criteria: Annotated[tuple[PersonalCriterion, ...], Field(min_length=1, max_length=20)]
    target_profiles: Annotated[tuple[TargetProfile, ...], Field(min_length=1, max_length=20)]

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


def qualification_definition_revision_id(
    definition: QualificationDefinition,
) -> QualificationDefinitionRevisionId:
    content = json.dumps(
        definition.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return QualificationDefinitionRevisionId(hashlib.sha256(content.encode()).hexdigest())
