from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg
from psycopg.types.json import Jsonb
from pydantic import JsonValue

from job_finder.evaluation.models import PromptReleaseId, PromptVersionId
from job_finder.evaluation.prompts import PROMPTS, PromptDefinition

RELEASE_NAME = "release-2026-09-10-2"
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


class PromptReleaseError(RuntimeError):
    """The stored prompt release does not match the application catalog."""


@dataclass(frozen=True)
class PromptVersion:
    id: PromptVersionId
    definition: PromptDefinition
    content_digest: str
    messages: tuple[dict[str, str], ...]
    input_schema: dict[str, JsonValue]
    output_schema: dict[str, JsonValue]
    model: str
    parameters: dict[str, JsonValue]
    tool_name: str
    tool_description: str


@dataclass(frozen=True)
class PromptRelease:
    id: PromptReleaseId
    name: str
    content_digest: str
    versions: tuple[PromptVersion, ...]

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
                  id, prompt_name, content_digest, messages, input_schema, output_schema,
                  model, parameters, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (
                    version.id,
                    version.definition.name,
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
            ON CONFLICT DO NOTHING
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
        for version in release.versions:
            _ = connection.execute(
                """
                INSERT INTO prompt_release_members (release_id, prompt_name, prompt_version_id)
                VALUES (%s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (release.id, version.definition.name, version.id),
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
        SELECT r.name, r.content_digest, v.id, v.prompt_name, v.content_digest,
               v.messages, v.input_schema, v.output_schema, v.model, v.parameters
        FROM prompt_releases r
        JOIN prompt_release_members m ON m.release_id = r.id
        JOIN prompt_versions v ON v.id = m.prompt_version_id
        WHERE r.id = %s
        """,
        (release_id,),
    ).fetchall()
    expected = build_prompt_release()
    if not rows:
        raise PromptReleaseError(f"Prompt release not found: {release_id}")
    by_name = {prompt.name: prompt for prompt in PROMPTS}
    if len(rows) != len(by_name) or {str(row[3]) for row in rows} != set(by_name):
        raise PromptReleaseError(f"Prompt release {release_id} does not contain the prompt catalog")
    versions: list[PromptVersion] = []
    for prompt in PROMPTS:
        row = next(item for item in rows if str(item[3]) == prompt.name)
        version = _build_version(prompt)
        stored = (
            str(row[2]),
            str(row[4]),
            row[5],
            row[6],
            row[7],
            str(row[8]),
            row[9],
        )
        wanted = (
            str(version.id),
            version.content_digest,
            list(version.messages),
            version.input_schema,
            version.output_schema,
            version.model,
            version.parameters,
        )
        if stored != wanted:
            raise PromptReleaseError(f"Stored prompt differs from catalog: {prompt.name}")
        versions.append(version)
    loaded = PromptRelease(
        id=release_id,
        name=str(rows[0][0]),
        content_digest=str(rows[0][1]),
        versions=tuple(versions),
    )
    if loaded != expected:
        raise PromptReleaseError(f"Stored release differs from catalog: {release_id}")
    return loaded


def _build_version(prompt: PromptDefinition) -> PromptVersion:
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
    }
    content = {
        "messages": messages,
        "input_schema": input_schema,
        "output_schema": output_schema,
        "model": MODEL,
        "parameters": parameters,
    }
    content_digest = _digest(content)
    return PromptVersion(
        id=PromptVersionId(_digest([prompt.name, content_digest])),
        definition=prompt,
        content_digest=content_digest,
        messages=messages,
        input_schema=input_schema,
        output_schema=output_schema,
        model=MODEL,
        parameters=parameters,
        tool_name=tool_name,
        tool_description=tool_description,
    )


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
