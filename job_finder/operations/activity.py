from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal, LiteralString, cast
from uuid import UUID

from psycopg import sql

from job_finder.database import Connection, ConnectionFactory
from job_finder.operations._common import (
    OperationsUnavailable,
    PipelineRunStatus,
    WorkItemState,
    error_summary,
)

ActivityEntryStatus = Literal["running", "completed", "failed", "retrying", "terminal", "dismissed"]
ActivityEntryType = Literal["run", "work"]

ACTIVITY_STATUSES: frozenset[str] = frozenset(
    {"running", "completed", "failed", "retrying", "terminal", "dismissed"}
)


@dataclass(frozen=True)
class ActivityRun:
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
class ActivityWork:
    job_id: UUID
    state: WorkItemState
    attempt_count: int
    occurred_at: datetime
    retry_at: datetime | None
    failure_summary: str | None
    dismissed: bool


@dataclass(frozen=True)
class ActivityEntry:
    occurred_at: datetime
    ref: str
    item: ActivityRun | ActivityWork

    @property
    def entry_type(self) -> ActivityEntryType:
        return "run" if isinstance(self.item, ActivityRun) else "work"

    @property
    def kind(self) -> str:
        return self.item.kind if isinstance(self.item, ActivityRun) else "work"

    @property
    def status(self) -> ActivityEntryStatus:
        if isinstance(self.item, ActivityRun):
            return cast(ActivityEntryStatus, self.item.status)
        if self.item.dismissed:
            return "dismissed"
        if self.item.state == "failed":
            return "retrying"
        if self.item.state == "terminal_error":
            return "terminal"
        if self.item.state in {"pending", "leased"}:
            return "running"
        return "completed"

    @property
    def did_something(self) -> bool:
        if isinstance(self.item, ActivityWork):
            return True
        run = self.item
        if run.status == "failed":
            return True
        return bool(run.discoveries or run.processing_attempts or run.model_calls)


@dataclass(frozen=True)
class ActivityQuery:
    statuses: frozenset[str] = frozenset()
    kind: str | None = None
    from_at: datetime | None = None
    to_at: datetime | None = None
    limit: int = 50
    cursor: str | None = None
    show_no_ops: bool = False

    def __post_init__(self) -> None:
        unknown = self.statuses - ACTIVITY_STATUSES
        if unknown:
            raise ValueError(f"Unknown activity statuses: {sorted(unknown)}")
        if self.limit < 1:
            raise ValueError("Activity page limit must be positive")
        if self.cursor is not None:
            decode_activity_cursor(self.cursor)


@dataclass(frozen=True)
class ActivityPage:
    entries: tuple[ActivityEntry, ...]
    next_cursor: str | None
    hidden_no_op_count: int = 0


def _unavailable_activity_page(_query: ActivityQuery) -> ActivityPage:
    raise OperationsUnavailable("Recent activity is unavailable")


@dataclass(frozen=True)
class ActivityService:
    list: Callable[[ActivityQuery], ActivityPage] = _unavailable_activity_page


def postgres_activity_service(connect: ConnectionFactory) -> ActivityService:
    def list_page(query: ActivityQuery) -> ActivityPage:
        with connect() as connection:
            return load_activity_page(connection, query)

    return ActivityService(list=list_page)


def encode_activity_cursor(entry: ActivityEntry) -> str:
    raw = f"{entry.occurred_at.isoformat()}|{entry.entry_type}|{entry.ref}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_activity_cursor(cursor: str) -> tuple[datetime, str, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        occurred_raw, entry_type, ref = raw.split("|", 2)
        occurred_at = datetime.fromisoformat(occurred_raw)
    except (ValueError, UnicodeDecodeError) as error:
        raise ValueError("Activity cursor is invalid") from error
    if entry_type not in {"run", "work"}:
        raise ValueError("Activity cursor is invalid")
    return occurred_at, entry_type, ref


_ACTIVITY_RUN_SELECT = """
  SELECT 'run' AS entry_type,
    run.id::text AS ref,
    run.kind AS kind,
    run.status AS status,
    run.started_at AS occurred_at,
    run.completed_at AS completed_at,
    NULL::timestamptz AS retry_at,
    NULL::integer AS attempt_count,
    NULL::uuid AS job_id,
    run.error AS last_error,
    FALSE AS dismissed,
    (SELECT count(*) FROM job_discoveries d WHERE d.pipeline_run_id = run.id)
      AS discoveries,
    (SELECT count(*) FROM processing_attempts pa WHERE pa.pipeline_run_id = run.id)
      AS processing_attempts,
    (SELECT count(DISTINCT pa.job_id) FROM processing_attempts pa
       WHERE pa.pipeline_run_id = run.id AND pa.job_id IS NOT NULL)
      AS processed_jobs,
    (SELECT count(*) FROM model_call_attempts m WHERE m.pipeline_run_id = run.id)
      AS model_calls,
    (SELECT COALESCE(sum(m.cost_usd), 0) FROM model_call_attempts m
       WHERE m.pipeline_run_id = run.id)
      AS known_cost_usd
  FROM pipeline_runs run
"""

_ACTIVITY_WORK_SELECT = """
  SELECT 'work' AS entry_type,
    item.job_id::text AS ref,
    'work' AS kind,
    item.state AS status,
    COALESCE(item.last_failed_at, item.completed_at, item.retry_at, item.created_at)
      AS occurred_at,
    NULL::timestamptz AS completed_at,
    item.retry_at,
    item.attempt_count,
    item.job_id,
    item.last_error AS last_error,
    EXISTS (
      SELECT 1 FROM work_dismissals d
      WHERE d.job_id = item.job_id AND d.attempt_count = item.attempt_count
    ) AS dismissed,
    NULL::bigint AS discoveries,
    NULL::bigint AS processing_attempts,
    NULL::bigint AS processed_jobs,
    NULL::bigint AS model_calls,
    NULL::numeric AS known_cost_usd
  FROM job_work_items item
"""

_ACTIVITY_STATUS_FILTERS: tuple[tuple[str, str], ...] = (
    (
        "running",
        "(a.entry_type = 'run' AND a.status = 'running')"
        + " OR (a.entry_type = 'work' AND a.status IN ('pending', 'leased'))",
    ),
    ("completed", "(a.status = 'completed')"),
    ("failed", "(a.entry_type = 'run' AND a.status = 'failed')"),
    ("retrying", "(a.entry_type = 'work' AND a.status = 'failed')"),
    (
        "terminal",
        "(a.entry_type = 'work' AND a.status = 'terminal_error' AND NOT a.dismissed)",
    ),
    ("dismissed", "(a.entry_type = 'work' AND a.dismissed)"),
)

_ACTIVITY_DID_SOMETHING_SQL = (
    "(a.entry_type = 'work'"
    + " OR a.status = 'failed'"
    + " OR COALESCE(a.discoveries, 0) > 0"
    + " OR COALESCE(a.processing_attempts, 0) > 0"
    + " OR COALESCE(a.model_calls, 0) > 0)"
)


def load_activity_page(connection: Connection, query: ActivityQuery) -> ActivityPage:
    conditions: list[str] = []
    parameters: list[object] = []
    if query.statuses:
        matching = [
            sql_fragment
            for status, sql_fragment in _ACTIVITY_STATUS_FILTERS
            if status in query.statuses
        ]
        conditions.append("(" + " OR ".join(matching) + ")")
    if query.kind is not None:
        conditions.append("a.kind = %s")
        parameters.append(query.kind)
    if query.from_at is not None:
        conditions.append("a.occurred_at >= %s")
        parameters.append(query.from_at)
    if query.to_at is not None:
        conditions.append("a.occurred_at < %s")
        parameters.append(query.to_at)
    hidden_no_op_count = 0
    if not query.show_no_ops:
        count_sql = (
            "SELECT count(*) FROM ("
            + _ACTIVITY_RUN_SELECT
            + ") a WHERE "
            + " AND ".join((*conditions, f"NOT {_ACTIVITY_DID_SOMETHING_SQL}"))
        )
        count_row = connection.execute(
            sql.SQL(cast(LiteralString, count_sql)), tuple(parameters)
        ).fetchone()
        hidden_no_op_count = int(str(count_row[0])) if count_row is not None else 0
        conditions.append(_ACTIVITY_DID_SOMETHING_SQL)
    if query.cursor is not None:
        cursor_at, cursor_type, cursor_ref = decode_activity_cursor(query.cursor)
        conditions.append("ROW(a.occurred_at, a.entry_type, a.ref)" + " < ROW(%s, %s, %s)")
        parameters.extend((cursor_at, cursor_type, cursor_ref))

    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    fetch = query.limit + 1
    activity_sql = (
        "SELECT * FROM ("
        + _ACTIVITY_RUN_SELECT
        + " UNION ALL "
        + _ACTIVITY_WORK_SELECT
        + ") a"
        + where
        + " ORDER BY a.occurred_at DESC, a.entry_type DESC, a.ref DESC LIMIT %s"
    )
    rows = connection.execute(
        sql.SQL(cast(LiteralString, activity_sql)), (*parameters, fetch)
    ).fetchall()

    has_more = len(rows) > query.limit
    entries = tuple(_parse_activity_row(row) for row in rows[: query.limit])
    next_cursor = encode_activity_cursor(entries[-1]) if has_more and entries else None
    return ActivityPage(
        entries=entries,
        next_cursor=next_cursor,
        hidden_no_op_count=hidden_no_op_count,
    )


def _parse_activity_row(row: tuple[object, ...]) -> ActivityEntry:
    entry_type = str(row[0])
    ref = str(row[1])
    occurred_at = row[4]
    if not isinstance(occurred_at, datetime):
        raise RuntimeError("Activity entry time is invalid")
    if entry_type == "run":
        return ActivityEntry(
            occurred_at=occurred_at,
            ref=ref,
            item=_parse_activity_run(row),
        )
    if entry_type == "work":
        return ActivityEntry(
            occurred_at=occurred_at,
            ref=ref,
            item=_parse_activity_work(row),
        )
    raise RuntimeError("Activity entry type is invalid")


def _parse_activity_run(row: tuple[object, ...]) -> ActivityRun:
    run_id, kind, status, started_at, completed_at = row[1], row[2], row[3], row[4], row[5]
    if not isinstance(run_id, str) or not kind or not isinstance(started_at, datetime):
        raise RuntimeError("Pipeline run activity is invalid")
    if status not in {"running", "completed", "failed"}:
        raise RuntimeError("Pipeline run activity status is invalid")
    if completed_at is not None and not isinstance(completed_at, datetime):
        raise RuntimeError("Pipeline run activity completion is invalid")
    return ActivityRun(
        id=UUID(run_id),
        kind=str(kind),
        status=cast(PipelineRunStatus, str(status)),
        started_at=started_at,
        completed_at=completed_at,
        discoveries=int(str(row[11])),
        processing_attempts=int(str(row[12])),
        processed_jobs=int(str(row[13])),
        model_calls=int(str(row[14])),
        known_cost_usd=Decimal(str(row[15])),
        error_summary=None if row[9] is None else error_summary(row[9]),
    )


def _parse_activity_work(row: tuple[object, ...]) -> ActivityWork:
    job_ref, state, retry_at, attempt_count, job_id, failure_summary = (
        row[1],
        row[3],
        row[6],
        row[7],
        row[8],
        row[9],
    )
    occurred_at = row[4]
    if (
        not isinstance(job_ref, str)
        or not isinstance(job_id, UUID)
        or not isinstance(occurred_at, datetime)
        or state not in {"pending", "leased", "failed", "completed", "terminal_error"}
    ):
        raise RuntimeError("Work activity is invalid")
    if retry_at is not None and not isinstance(retry_at, datetime):
        raise RuntimeError("Work activity retry time is invalid")
    return ActivityWork(
        job_id=job_id,
        state=cast(WorkItemState, str(state)),
        attempt_count=int(str(attempt_count)),
        occurred_at=occurred_at,
        retry_at=retry_at,
        failure_summary=None if failure_summary is None else error_summary(failure_summary),
        dismissed=bool(row[10]),
    )
