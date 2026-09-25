from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Literal

import psycopg
from psycopg.types.json import Jsonb
from pydantic import JsonValue, TypeAdapter

from job_finder.benchmarks.qualification_evidence import (
    ProviderAttemptEvidence,
    QualificationEvidence,
    qualification_evidence_id,
)
from job_finder.evaluation.models import ModelCallAttempt

_ATTEMPT: TypeAdapter[ModelCallAttempt] = TypeAdapter(ModelCallAttempt)
_JSON_OBJECT: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])
Provider = Literal["openrouter", "typesafe"]


def provider_attempt_evidence(attempt: ModelCallAttempt) -> ProviderAttemptEvidence:
    return ProviderAttemptEvidence(
        input_digest=attempt.context.input_digest,
        requested_model=attempt.requested_model,
        observed_model=attempt.response_model,
        provider_response_id=attempt.provider_response_id,
        status=attempt.status,
        response=attempt.raw_response,
        observed_at=attempt.observed_at,
    )


def store_provider_attempts(
    connection: psycopg.Connection[tuple[object, ...]],
    evidence: QualificationEvidence,
    attempts: Sequence[ModelCallAttempt],
    *,
    provider: Provider,
    created_at: datetime,
    created_by: str,
) -> None:
    if evidence.origin not in ("canonical", "synthetic") or evidence.phase not in (
        "relevance",
        "enrichment",
        "deduplication",
        "composition",
    ):
        raise ValueError("Provider attempts require executed model phase evidence")
    if not attempts:
        raise ValueError("Provider evidence requires at least one observed attempt")
    if tuple(provider_attempt_evidence(attempt) for attempt in attempts) != evidence.attempts:
        raise ValueError("Provider attempt summaries differ from recorded attempts")
    evidence_id = qualification_evidence_id(evidence)
    with connection.transaction():
        for attempt in attempts:
            content = _JSON_OBJECT.validate_python(_ATTEMPT.dump_python(attempt, mode="json"))
            _ = connection.execute(
                """
                INSERT INTO qualification_provider_attempts (
                  id, evidence_id, request_id, attempt_number, prompt_release_id,
                  prompt_name, prompt_version_id, provider, content, created_at, created_by
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (
                    attempt.id,
                    evidence_id,
                    attempt.request_id,
                    attempt.attempt_number,
                    attempt.context.prompt_release_id,
                    attempt.prompt_name,
                    attempt.prompt_version_id,
                    provider,
                    Jsonb(content),
                    created_at,
                    created_by,
                ),
            )
            row = connection.execute(
                "SELECT evidence_id, provider, content FROM qualification_provider_attempts WHERE id = %s",
                (attempt.id,),
            ).fetchone()
            if row is None or row != (evidence_id, provider, content):
                raise ValueError("Stored provider attempt differs from its evidence")
