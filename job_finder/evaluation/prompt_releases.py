from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import ClassVar

import psycopg
from psycopg.types.json import Jsonb
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from job_finder.evaluation.models import PromptReleaseId, PromptVersionId
from job_finder.evaluation.prompts import PROMPTS, PromptDefinition, PromptPhase

RELEASE_NAME = "release-2026-09-12-1"
MODEL = "google/gemini-2.5-flash"
EVALUATION_OUTPUT_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "properties": {
        "pass": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["pass", "reason"],
    "additionalProperties": False,
}
ENRICHMENT_OUTPUT_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "company": {"type": "string"},
        "description": {"type": "string"},
        "location": {"type": "string"},
    },
    "required": ["title", "company", "description", "location"],
    "additionalProperties": False,
}
DEDUPLICATION_OUTPUT_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "properties": {
        "isDuplicate": {"type": "boolean"},
        "matchedTitle": {"type": "string"},
    },
    "required": ["isDuplicate"],
    "additionalProperties": False,
}
_STRINGS = TypeAdapter(tuple[str, ...])


class PromptReleaseError(RuntimeError):
    """The stored prompt release is missing or corrupt."""


class PromptModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class PromptExecution(PromptModel):
    name: str = Field(min_length=1)
    criterion: str = Field(min_length=1)
    phase: PromptPhase
    inputs: tuple[str, ...] = Field(min_length=1)


class PromptVersion(PromptModel):
    id: PromptVersionId
    definition: PromptExecution
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    messages: tuple[dict[str, str], ...] = Field(min_length=1)
    input_schema: dict[str, JsonValue]
    output_schema: dict[str, JsonValue]
    model: str = Field(min_length=1)
    parameters: dict[str, JsonValue]
    tool_name: str = Field(min_length=1)
    tool_description: str = Field(min_length=1)

    @model_validator(mode="after")
    def execution_matches_content(self) -> PromptVersion:
        if self.definition.inputs != _input_names(self.input_schema):
            raise ValueError("Prompt inputs do not match the stored input schema")
        if self.parameters.get("tool_name") != self.tool_name:
            raise ValueError("Prompt tool name does not match the stored parameters")
        if self.parameters.get("tool_description") != self.tool_description:
            raise ValueError("Prompt tool description does not match the stored parameters")
        return self


class PromptRelease(PromptModel):
    id: PromptReleaseId
    name: str = Field(min_length=1)
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    versions: tuple[PromptVersion, ...] = Field(min_length=1)

    def version(self, name: str) -> PromptVersion:
        for version in self.versions:
            if version.definition.name == name:
                return version
        raise PromptReleaseError(f"Prompt release {self.name} does not contain {name}")


def build_prompt_release() -> PromptRelease:
    versions = tuple(_build_version(prompt) for prompt in PROMPTS)
    digest = _digest([[version.definition.name, version.id] for version in versions])
    return PromptRelease(
        id=PromptReleaseId(digest),
        name=RELEASE_NAME,
        content_digest=digest,
        versions=versions,
    )


def bootstrap_prompt_release(
    connection: psycopg.Connection[tuple[object, ...]],
) -> PromptRelease:
    release = build_prompt_release()
    now = datetime.now(UTC)
    with connection.transaction():
        for version in release.versions:
            _ = connection.execute(
                """
                INSERT INTO prompt_versions (
                  id, prompt_name, criterion, phase, content_digest, messages,
                  input_schema, output_schema, model, parameters, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (
                    version.id,
                    version.definition.name,
                    version.definition.criterion,
                    version.definition.phase,
                    version.content_digest,
                    Jsonb(version.messages),
                    Jsonb(version.input_schema),
                    Jsonb(version.output_schema),
                    version.model,
                    Jsonb(version.parameters),
                    now,
                ),
            )
        _ = connection.execute(
            """
            INSERT INTO prompt_releases (
              id, name, content_digest, expected_member_count, created_at, created_by
            ) VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (
                release.id,
                release.name,
                release.content_digest,
                len(release.versions),
                now,
                "bootstrap",
            ),
        )
        for position, version in enumerate(release.versions):
            _ = connection.execute(
                """
                INSERT INTO prompt_release_members (
                  release_id, prompt_name, prompt_version_id, position
                ) VALUES (%s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (release.id, version.definition.name, version.id, position),
            )
    loaded = load_prompt_release(connection, release.id)
    if loaded != release:
        raise PromptReleaseError(f"Stored release {release.name} differs from the prompt catalog")
    return loaded


def load_prompt_release(
    connection: psycopg.Connection[tuple[object, ...]], release_id: PromptReleaseId
) -> PromptRelease:
    rows = connection.execute(
        """
        SELECT r.name, r.content_digest, r.expected_member_count,
               v.id, v.prompt_name, v.criterion, v.phase, v.content_digest,
               v.messages, v.input_schema, v.output_schema, v.model, v.parameters
        FROM prompt_releases r
        JOIN prompt_release_members m ON m.release_id = r.id
        JOIN prompt_versions v ON v.id = m.prompt_version_id
        WHERE r.id = %s
        ORDER BY m.position
        """,
        (release_id,),
    ).fetchall()
    if not rows:
        raise PromptReleaseError(f"Prompt release not found: {release_id}")
    expected_member_count = int(str(rows[0][2]))
    if len(rows) != expected_member_count:
        raise PromptReleaseError(f"Prompt release {release_id} is incomplete")
    try:
        versions = tuple(_load_version(row) for row in rows)
    except (ValidationError, ValueError) as error:
        raise PromptReleaseError(f"Prompt release {release_id} contains invalid content") from error
    for version in versions:
        content_digest = _version_digest(version)
        if version.content_digest != content_digest:
            raise PromptReleaseError(f"Stored prompt content is corrupt: {version.definition.name}")
        expected_id = PromptVersionId(_digest([version.definition.name, content_digest]))
        if version.id != expected_id:
            raise PromptReleaseError(
                f"Stored prompt identity is corrupt: {version.definition.name}"
            )
    digest = _digest([[version.definition.name, version.id] for version in versions])
    stored_digest = str(rows[0][1])
    if stored_digest != digest or str(release_id) != digest:
        raise PromptReleaseError(f"Stored release identity is corrupt: {release_id}")
    return PromptRelease(
        id=release_id,
        name=str(rows[0][0]),
        content_digest=stored_digest,
        versions=versions,
    )


def _build_version(prompt: PromptDefinition) -> PromptVersion:
    model = prompt.model or MODEL
    definition = PromptExecution(
        name=prompt.name,
        criterion=prompt.criterion,
        phase=prompt.phase,
        inputs=prompt.inputs,
    )
    messages = (
        {"role": "system", "content": prompt.system_message},
        {"role": "user", "content": prompt.user_message},
    )
    input_schema: dict[str, JsonValue] = {
        "type": "object",
        "properties": {name: {"type": "string"} for name in prompt.inputs},
        "required": list(prompt.inputs),
        "additionalProperties": False,
    }
    output_schema = _output_schema(prompt)
    tool_name, tool_description = _tool(prompt)
    parameters: dict[str, JsonValue] = {
        "temperature": 0,
        "max_tokens": prompt.max_tokens,
        "tool_name": tool_name,
        "tool_description": tool_description,
        "criterion": prompt.criterion,
        "phase": prompt.phase,
    }
    content_digest = _digest(
        _version_content(messages, input_schema, output_schema, model, parameters)
    )
    return PromptVersion(
        id=PromptVersionId(_digest([prompt.name, content_digest])),
        definition=definition,
        content_digest=content_digest,
        messages=messages,
        input_schema=input_schema,
        output_schema=output_schema,
        model=model,
        parameters=parameters,
        tool_name=tool_name,
        tool_description=tool_description,
    )


def _load_version(row: tuple[object, ...]) -> PromptVersion:
    parameters = TypeAdapter(dict[str, JsonValue]).validate_python(row[12])
    tool_name = TypeAdapter(str).validate_python(parameters.get("tool_name"))
    tool_description = TypeAdapter(str).validate_python(parameters.get("tool_description"))
    input_schema = TypeAdapter(dict[str, JsonValue]).validate_python(row[9])
    return PromptVersion.model_validate(
        {
            "id": row[3],
            "definition": {
                "name": row[4],
                "criterion": row[5],
                "phase": row[6],
                "inputs": _input_names(input_schema),
            },
            "content_digest": row[7],
            "messages": row[8],
            "input_schema": input_schema,
            "output_schema": row[10],
            "model": row[11],
            "parameters": parameters,
            "tool_name": tool_name,
            "tool_description": tool_description,
        }
    )


def _version_digest(version: PromptVersion) -> str:
    return _digest(
        _version_content(
            version.messages,
            version.input_schema,
            version.output_schema,
            version.model,
            version.parameters,
        )
    )


def _version_content(
    messages: tuple[dict[str, str], ...],
    input_schema: dict[str, JsonValue],
    output_schema: dict[str, JsonValue],
    model: str,
    parameters: dict[str, JsonValue],
) -> dict[str, object]:
    return {
        "messages": messages,
        "input_schema": input_schema,
        "output_schema": output_schema,
        "model": model,
        "parameters": parameters,
    }


def _input_names(input_schema: dict[str, JsonValue]) -> tuple[str, ...]:
    return _STRINGS.validate_python(input_schema.get("required"))


def _output_schema(prompt: PromptDefinition) -> dict[str, JsonValue]:
    match prompt.output:
        case "evaluation":
            return EVALUATION_OUTPUT_SCHEMA
        case "enrichment":
            return ENRICHMENT_OUTPUT_SCHEMA
        case "deduplication":
            return DEDUPLICATION_OUTPUT_SCHEMA


def _tool(prompt: PromptDefinition) -> tuple[str, str]:
    match prompt.output:
        case "evaluation":
            return "evaluate_job", "Submit the evaluation result for a job listing"
        case "enrichment":
            return "enrich_job", "Submit the normalized and cleaned job data"
        case "deduplication":
            return (
                "check_duplicate",
                "Decide whether the new job title refers to the same role as any existing title at the same company",
            )


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()
