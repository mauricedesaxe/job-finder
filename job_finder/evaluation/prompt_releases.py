from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg
from psycopg.types.json import Jsonb
from pydantic import JsonValue

from job_finder.evaluation.models import PromptReleaseId, PromptVersionId
from job_finder.evaluation.prompts import EVALUATION_PROMPTS, EvaluationPrompt

RELEASE_NAME = "release-2026-09-10-1"
MODEL = "google/gemini-2.5-flash"
PARAMETERS: dict[str, JsonValue] = {"temperature": 0, "max_tokens": 256}
OUTPUT_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "properties": {
        "pass": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["pass", "reason"],
    "additionalProperties": False,
}


class PromptReleaseError(RuntimeError):
    """The stored prompt release does not match the application catalog."""


@dataclass(frozen=True)
class PromptVersion:
    id: PromptVersionId
    definition: EvaluationPrompt
    content_digest: str
    messages: tuple[dict[str, str], ...]
    input_schema: dict[str, JsonValue]
    output_schema: dict[str, JsonValue]
    model: str
    parameters: dict[str, JsonValue]


@dataclass(frozen=True)
class EvaluationPromptRelease:
    id: PromptReleaseId
    name: str
    content_digest: str
    versions: tuple[PromptVersion, ...]


def build_evaluation_prompt_release() -> EvaluationPromptRelease:
    versions = tuple(_build_version(prompt) for prompt in EVALUATION_PROMPTS)
    digest = _digest([[version.definition.name, version.id] for version in versions])
    return EvaluationPromptRelease(
        id=PromptReleaseId(digest),
        name=RELEASE_NAME,
        content_digest=digest,
        versions=versions,
    )


def bootstrap_evaluation_prompt_release(
    connection: psycopg.Connection[tuple[object, ...]],
) -> EvaluationPromptRelease:
    release = build_evaluation_prompt_release()
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
                    Jsonb(OUTPUT_SCHEMA),
                    MODEL,
                    Jsonb(PARAMETERS),
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
    loaded = load_evaluation_prompt_release(connection, release.id)
    if loaded != release:
        raise PromptReleaseError(f"Stored release {release.name} differs from the prompt catalog")
    return loaded


def load_evaluation_prompt_release(
    connection: psycopg.Connection[tuple[object, ...]], release_id: PromptReleaseId
) -> EvaluationPromptRelease:
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
    expected = build_evaluation_prompt_release()
    if not rows:
        raise PromptReleaseError(f"Prompt release not found: {release_id}")
    by_name = {prompt.name: prompt for prompt in EVALUATION_PROMPTS}
    if len(rows) != len(by_name) or {str(row[3]) for row in rows} != set(by_name):
        raise PromptReleaseError(
            f"Prompt release {release_id} does not contain the evaluation catalog"
        )
    versions: list[PromptVersion] = []
    for prompt in EVALUATION_PROMPTS:
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
            OUTPUT_SCHEMA,
            MODEL,
            PARAMETERS,
        )
        if stored != wanted:
            raise PromptReleaseError(f"Stored prompt differs from catalog: {prompt.name}")
        versions.append(version)
    loaded = EvaluationPromptRelease(
        id=release_id,
        name=str(rows[0][0]),
        content_digest=str(rows[0][1]),
        versions=tuple(versions),
    )
    if loaded != expected:
        raise PromptReleaseError(f"Stored release differs from catalog: {release_id}")
    return loaded


def _build_version(prompt: EvaluationPrompt) -> PromptVersion:
    messages = (
        {"role": "system", "content": prompt.system_message},
        {"role": "user", "content": "{job}"},
    )
    input_schema: dict[str, JsonValue] = {
        "type": "object",
        "properties": {name: {"type": "string"} for name in prompt.inputs},
        "required": list(prompt.inputs),
        "additionalProperties": False,
    }
    content = {
        "messages": messages,
        "input_schema": input_schema,
        "output_schema": OUTPUT_SCHEMA,
        "model": MODEL,
        "parameters": PARAMETERS,
    }
    content_digest = _digest(content)
    return PromptVersion(
        id=PromptVersionId(_digest([prompt.name, content_digest])),
        definition=prompt,
        content_digest=content_digest,
        messages=messages,
        input_schema=input_schema,
        output_schema=OUTPUT_SCHEMA,
        model=MODEL,
        parameters=PARAMETERS,
    )


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()
