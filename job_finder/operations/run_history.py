from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import cast
from uuid import UUID

from job_finder.database import Connection, ConnectionFactory
from job_finder.operations._common import OperationsUnavailable, PipelineRunStatus, error_summary


@dataclass(frozen=True)
class RunListItem:
    id: UUID
    kind: str
    status: PipelineRunStatus
    started_at: datetime
    completed_at: datetime | None
    discoveries: int
    processing_attempts: int
    processed_jobs: int
    model_calls: int
    known_cost_usd: Decimal
    error_summary: str | None

    @property
    def idle_tick(self) -> bool:
        return (
            self.kind == "orchestration"
            and self.status == "completed"
            and self.discoveries == 0
            and self.processing_attempts == 0
        )


@dataclass(frozen=True)
class RunKeywordSummary:
    keyword: str
    jobs: int


@dataclass(frozen=True)
class RunAttemptSummary:
    job_id: UUID | None
    operation_key: str
    attempt_number: int
    status: str
    error_summary: str | None


@dataclass(frozen=True)
class RunModelSummary:
    model: str
    status: str
    calls: int
    input_tokens: int
    output_tokens: int
    known_cost_usd: Decimal
    max_latency_ms: int


@dataclass(frozen=True)
class RunDecisionSummary:
    outcome: str
    count: int


@dataclass(frozen=True)
class RunDetail:
    item: RunListItem
    parameters: Mapping[str, object]
    unknown_cost_calls: int
    keywords: tuple[RunKeywordSummary, ...]
    attempts: tuple[RunAttemptSummary, ...]
    models: tuple[RunModelSummary, ...]
    decisions: tuple[RunDecisionSummary, ...]


class RunNotFound(LookupError):
    pass


def _unavailable_runs_list(_limit: int) -> tuple[RunListItem, ...]:
    raise OperationsUnavailable("Pipeline runs are unavailable")


def _unavailable_run_detail(_run_id: UUID) -> RunDetail:
    raise OperationsUnavailable("Pipeline runs are unavailable")


@dataclass(frozen=True)
class RunsService:
    list: Callable[[int], tuple[RunListItem, ...]] = _unavailable_runs_list
    detail: Callable[[UUID], RunDetail] = _unavailable_run_detail


def postgres_runs_service(connect: ConnectionFactory) -> RunsService:
    def list_runs(limit: int) -> tuple[RunListItem, ...]:
        with connect() as connection:
            return load_pipeline_runs(connection, limit=limit)

    def detail(run_id: UUID) -> RunDetail:
        with connect() as connection:
            return load_run_detail(connection, run_id)

    return RunsService(list=list_runs, detail=detail)


def load_pipeline_runs(connection: Connection, *, limit: int = 50) -> tuple[RunListItem, ...]:
    if limit < 1:
        raise ValueError("run limit must be positive")
    rows = connection.execute(
        """
        SELECT run.id, run.kind, run.status, run.started_at, run.completed_at, run.error,
          (SELECT count(*) FROM job_discoveries d WHERE d.pipeline_run_id = run.id),
          (SELECT count(*) FROM processing_attempts pa WHERE pa.pipeline_run_id = run.id),
          (SELECT count(DISTINCT pa.job_id) FROM processing_attempts pa
             WHERE pa.pipeline_run_id = run.id AND pa.job_id IS NOT NULL),
          (SELECT count(*) FROM model_call_attempts m WHERE m.pipeline_run_id = run.id),
          (SELECT COALESCE(sum(m.cost_usd), 0) FROM model_call_attempts m
             WHERE m.pipeline_run_id = run.id)
        FROM pipeline_runs run
        ORDER BY run.started_at DESC, run.id DESC
        LIMIT %s
        """,
        (limit,),
    ).fetchall()
    return tuple(_parse_run_item(row) for row in rows)


def load_run_detail(connection: Connection, run_id: UUID) -> RunDetail:
    item_row = connection.execute(
        """
        SELECT run.id, run.kind, run.status, run.started_at, run.completed_at, run.error,
          (SELECT count(*) FROM job_discoveries d WHERE d.pipeline_run_id = run.id),
          (SELECT count(*) FROM processing_attempts pa WHERE pa.pipeline_run_id = run.id),
          (SELECT count(DISTINCT pa.job_id) FROM processing_attempts pa
             WHERE pa.pipeline_run_id = run.id AND pa.job_id IS NOT NULL),
          (SELECT count(*) FROM model_call_attempts m WHERE m.pipeline_run_id = run.id),
          (SELECT COALESCE(sum(m.cost_usd), 0) FROM model_call_attempts m
             WHERE m.pipeline_run_id = run.id),
          run.parameters,
          (SELECT count(*) FROM model_call_attempts m
             WHERE m.pipeline_run_id = run.id AND m.cost_usd IS NULL)
        FROM pipeline_runs run
        WHERE run.id = %s
        """,
        (run_id,),
    ).fetchone()
    if item_row is None:
        raise RunNotFound("Pipeline run does not exist")

    keywords = tuple(
        RunKeywordSummary(keyword=str(row[0]), jobs=int(str(row[1])))
        for row in connection.execute(
            """
            SELECT keyword, count(*) FROM job_discoveries
            WHERE pipeline_run_id = %s
            GROUP BY keyword ORDER BY count(*) DESC, keyword
            """,
            (run_id,),
        ).fetchall()
    )
    attempts = tuple(
        RunAttemptSummary(
            job_id=cast(UUID | None, row[0]),
            operation_key=str(row[1]),
            attempt_number=int(str(row[2])),
            status=str(row[3]),
            error_summary=None if row[4] is None else error_summary(row[4]),
        )
        for row in connection.execute(
            """
            SELECT job_id, operation_key, attempt_number, status, error
            FROM processing_attempts
            WHERE pipeline_run_id = %s
            ORDER BY started_at, operation_key, attempt_number
            LIMIT 200
            """,
            (run_id,),
        ).fetchall()
    )
    models = tuple(
        RunModelSummary(
            model=str(row[0]),
            status=str(row[1]),
            calls=int(str(row[2])),
            input_tokens=int(str(row[3] or 0)),
            output_tokens=int(str(row[4] or 0)),
            known_cost_usd=Decimal(str(row[5])),
            max_latency_ms=int(str(row[6] or 0)),
        )
        for row in connection.execute(
            """
            SELECT requested_model, status, count(*),
                   sum(input_tokens), sum(output_tokens), COALESCE(sum(cost_usd), 0),
                   max(latency_ms)
            FROM model_call_attempts
            WHERE pipeline_run_id = %s
            GROUP BY requested_model, status
            ORDER BY count(*) DESC
            """,
            (run_id,),
        ).fetchall()
    )
    decisions = tuple(
        RunDecisionSummary(outcome=str(row[0]), count=int(str(row[1])))
        for row in connection.execute(
            """
            SELECT outcome, count(*) FROM evaluation_decisions
            WHERE pipeline_run_id = %s
            GROUP BY outcome ORDER BY count(*) DESC
            """,
            (run_id,),
        ).fetchall()
    )
    parameters = item_row[11]
    if not isinstance(parameters, dict):
        raise RuntimeError("Run parameters are invalid")
    return RunDetail(
        item=_parse_run_item(item_row),
        parameters=cast(Mapping[str, object], parameters),
        unknown_cost_calls=int(str(item_row[12])),
        keywords=keywords,
        attempts=attempts,
        models=models,
        decisions=decisions,
    )


def _parse_run_item(row: tuple[object, ...]) -> RunListItem:
    run_id, kind, status, started_at, completed_at, raw_error = row[:6]
    if not isinstance(run_id, UUID):
        raise RuntimeError("Pipeline run id is invalid")
    if not isinstance(kind, str):
        raise RuntimeError("Pipeline run kind is invalid")
    if status not in {"running", "completed", "failed"}:
        raise RuntimeError("Pipeline run status is invalid")
    if not isinstance(started_at, datetime):
        raise RuntimeError("Pipeline run start time is invalid")
    if completed_at is not None and not isinstance(completed_at, datetime):
        raise RuntimeError("Pipeline run completion time is invalid")
    return RunListItem(
        id=run_id,
        kind=kind,
        status=cast(PipelineRunStatus, status),
        started_at=started_at,
        completed_at=completed_at,
        discoveries=int(str(row[6])),
        processing_attempts=int(str(row[7])),
        processed_jobs=int(str(row[8])),
        model_calls=int(str(row[9])),
        known_cost_usd=Decimal(str(row[10])),
        error_summary=None if raw_error is None else error_summary(raw_error),
    )
