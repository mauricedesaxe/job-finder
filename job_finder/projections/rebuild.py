from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

import psycopg
from pydantic import JsonValue, TypeAdapter

from job_finder.benchmarks.executions import load_run as _load_run
from job_finder.benchmarks.manifests import load_manifest as _load_manifest
from job_finder.benchmarks.promotions import load_promotion_decision as _load_promotion_decision
from job_finder.evaluation.models import ModelCallAttempt
from job_finder.evaluation.openrouter import (
    enqueue_model_call_projection as _enqueue_model_call_projection,
)
from job_finder.projections.outbox import enqueue_projection as _enqueue_projection

_ATTEMPT_COLUMNS = """
    SELECT id, processing_attempt_id, pipeline_run_id, prompt_release_id,
           request_id, attempt_number, operation_key, prompt_name,
           prompt_version_id, input_digest, requested_model, response_model,
           provider, provider_response_id, status, parsed_output, raw_response,
           input_tokens, output_tokens, cost_usd, latency_ms, error,
           observed_at, request_messages
    FROM model_call_attempts ORDER BY observed_at
"""
_DICT_OUTPUT: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])
_MESSAGE_LIST: TypeAdapter[list[dict[str, str]]] = TypeAdapter(list[dict[str, str]])
_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


def rebuild_langfuse_projections(
    connection: psycopg.Connection[tuple[object, ...]],
) -> dict[str, int]:
    return {
        "model calls": _rebuild_model_calls(connection),
        "manifests": _rebuild_manifests(connection),
        "runs": _rebuild_runs(connection),
        "promotions": _rebuild_promotions(connection),
    }


def _moment(value: object) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


def _uuid(value: object) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def _decimal(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _rebuild_model_calls(connection: psycopg.Connection[tuple[object, ...]]) -> int:
    rows = connection.execute(_ATTEMPT_COLUMNS).fetchall()
    with connection.transaction():
        for row in rows:
            _enqueue_model_call_projection(connection, _attempt_from_row(row))
    return len(rows)


def _attempt_from_row(row: tuple[object, ...]) -> ModelCallAttempt:
    return TypeAdapter(ModelCallAttempt).validate_python(
        {
            "id": _uuid(row[0]),
            "context": {
                "processing_attempt_id": _uuid(row[1]),
                "pipeline_run_id": _uuid(row[2]),
                "prompt_release_id": str(row[3]),
                "operation_key": str(row[6]),
                "input_digest": str(row[9]),
            },
            "request_id": str(row[4]),
            "attempt_number": int(str(row[5])),
            "prompt_name": str(row[7]),
            "prompt_version_id": str(row[8]),
            "requested_model": str(row[10]),
            "response_model": None if row[11] is None else str(row[11]),
            "provider_response_id": None if row[13] is None else str(row[13]),
            "status": str(row[14]),
            "parsed_output": None if row[15] is None else _DICT_OUTPUT.validate_python(row[15]),
            "raw_response": None if row[16] is None else _JSON.validate_python(row[16]),
            "input_tokens": None if row[17] is None else int(str(row[17])),
            "output_tokens": None if row[18] is None else int(str(row[18])),
            "cost_usd": None if row[19] is None else _decimal(row[19]),
            "latency_ms": int(str(row[20])),
            "error": None if row[21] is None else _DICT_OUTPUT.validate_python(row[21]),
            "observed_at": _moment(row[22]),
            "request_messages": _MESSAGE_LIST.validate_python(row[23] or []),
        }
    )


def _rebuild_manifests(connection: psycopg.Connection[tuple[object, ...]]) -> int:
    rows = connection.execute(
        "SELECT id, created_at FROM evaluation_manifests ORDER BY created_at"
    ).fetchall()
    with connection.transaction():
        for row in rows:
            manifest = _load_manifest(connection, str(row[0]))
            _enqueue_projection(
                connection,
                "evaluation_manifest",
                str(row[0]),
                manifest,
                _moment(row[1]),
            )
    return len(rows)


def _rebuild_runs(connection: psycopg.Connection[tuple[object, ...]]) -> int:
    rows = connection.execute(
        "SELECT id, completed_at FROM evaluation_runs ORDER BY completed_at"
    ).fetchall()
    with connection.transaction():
        for row in rows:
            run = _load_run(connection, str(row[0]))
            _enqueue_projection(connection, "evaluation_run", str(row[0]), run, _moment(row[1]))
    return len(rows)


def _rebuild_promotions(connection: psycopg.Connection[tuple[object, ...]]) -> int:
    query = (
        "SELECT idempotency_key, created_at FROM prompt_promotion_decisions" " ORDER BY created_at"
    )
    rows = connection.execute(query).fetchall()
    with connection.transaction():
        for row in rows:
            decision = _load_promotion_decision(connection, str(row[0]))
            if decision is None:
                raise RuntimeError(f"Promotion decision row vanished for {row[0]}")
            _enqueue_projection(
                connection,
                "prompt_promotion",
                str(decision.id),
                decision,
                _moment(row[1]),
            )
    return len(rows)
