from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from psycopg import Connection, sql
from psycopg.pq import TransactionStatus

from job_finder.jobs.decision_pipeline import THIN_BODY_THRESHOLD
from job_finder.jobs.structural_filter import (
    GENERIC_TITLE_REASON,
    NON_ROLE_TITLE_REASON,
)

_MIS_TITLED_REASON_PREFIXES = (
    NON_ROLE_TITLE_REASON,
    GENERIC_TITLE_REASON,
)
_CHUNK = 200

_THIN_BODY_FROM = "JOIN snapshot_corrections sc ON sc.snapshot_id = s.id"
_THIN_BODY_PREDICATE = "length(s.description) < %s AND sc.description IS NOT NULL"
_THIN_BODY_PARAMS: tuple[object, ...] = (THIN_BODY_THRESHOLD,)


@dataclass(frozen=True)
class _ReprocessPolicy:
    extra_from: sql.SQL
    predicate: sql.SQL
    params: tuple[object, ...]


def _require_autocommit(connection: Connection[tuple[object, ...]]) -> None:
    if not connection.autocommit:
        raise ValueError("Reprocessing requires an autocommit connection")


def _mis_titled_predicates() -> tuple[str, ...]:
    return tuple(f"{reason} (%" for reason in _MIS_TITLED_REASON_PREFIXES)


def _mis_titled_policy() -> _ReprocessPolicy:
    return _ReprocessPolicy(
        extra_from=sql.SQL(""),
        predicate=sql.SQL("d.outcome = 'rejected' AND (d.reason LIKE %s OR d.reason LIKE %s)"),
        params=_mis_titled_predicates(),
    )


def _thin_body_policy() -> _ReprocessPolicy:
    return _ReprocessPolicy(
        extra_from=sql.SQL(_THIN_BODY_FROM),
        predicate=sql.SQL(_THIN_BODY_PREDICATE),
        params=_THIN_BODY_PARAMS,
    )


def select_mis_titled_jobs(connection: Connection[tuple[object, ...]]) -> tuple[UUID, ...]:
    return _select_jobs(connection, _mis_titled_policy())


def select_thin_body_jobs(connection: Connection[tuple[object, ...]]) -> tuple[UUID, ...]:
    return _select_jobs(connection, _thin_body_policy())


def _select_jobs(
    connection: Connection[tuple[object, ...]], policy: _ReprocessPolicy
) -> tuple[UUID, ...]:
    rows = connection.execute(
        sql.SQL(
            """
            SELECT s.job_id
            FROM evaluation_decisions d
            JOIN job_snapshots s ON s.id = d.snapshot_id
            {extra_from}
            JOIN job_work_items w ON w.job_id = s.job_id
            WHERE {predicate}
              AND NOT EXISTS (
                SELECT 1 FROM review_items i WHERE i.evaluation_id = d.id
              )
              AND NOT EXISTS (
                SELECT 1
                FROM evaluation_decisions d2
                JOIN job_snapshots s2 ON s2.id = d2.snapshot_id
                WHERE s2.job_id = s.job_id AND d2.id <> d.id
              )
              AND w.state = 'completed'
              AND w.terminal_decision_id = d.id
            ORDER BY s.job_id
            """
        ).format(extra_from=policy.extra_from, predicate=policy.predicate),
        policy.params,
    ).fetchall()
    return tuple(UUID(str(row[0])) for row in rows)


def reset_mis_titled_jobs(
    connection: Connection[tuple[object, ...]], job_ids: tuple[UUID, ...]
) -> int:
    return _reset_jobs(connection, job_ids, _mis_titled_policy())


def reset_thin_body_jobs(
    connection: Connection[tuple[object, ...]], job_ids: tuple[UUID, ...]
) -> int:
    return _reset_jobs(connection, job_ids, _thin_body_policy())


def _reset_jobs(
    connection: Connection[tuple[object, ...]],
    job_ids: tuple[UUID, ...],
    policy: _ReprocessPolicy,
) -> int:
    _require_autocommit(connection)
    reset = 0
    for start in range(0, len(job_ids), _CHUNK):
        reset += _reset_chunk(connection, job_ids[start : start + _CHUNK], policy)
    return reset


def _reset_chunk(
    connection: Connection[tuple[object, ...]],
    job_ids: tuple[UUID, ...],
    policy: _ReprocessPolicy,
) -> int:
    if not job_ids:
        return 0
    reset = 0
    with connection.transaction():
        _ = connection.execute(
            "ALTER TABLE evaluation_decisions DISABLE TRIGGER evaluation_decisions_are_immutable"
        )
        _ = connection.execute(
            "ALTER TABLE pipeline_receipts DISABLE TRIGGER pipeline_receipts_are_immutable"
        )
        try:
            for job_id in job_ids:
                cursor = connection.execute(
                    sql.SQL(
                        """
                        UPDATE job_work_items w
                        SET state = 'pending', owner_token = NULL, lease_expires_at = NULL,
                            retry_at = NULL, terminal_decision_id = NULL, last_error = NULL,
                            completed_at = NULL, attempt_count = 0
                        FROM evaluation_decisions d
                        JOIN job_snapshots s ON s.id = d.snapshot_id
                        {extra_from}
                        WHERE w.job_id = %s
                          AND w.terminal_decision_id = d.id
                          AND s.job_id = %s
                          AND {predicate}
                          AND NOT EXISTS (
                            SELECT 1 FROM review_items i WHERE i.evaluation_id = d.id
                          )
                          AND NOT EXISTS (
                            SELECT 1
                            FROM evaluation_decisions d2
                            JOIN job_snapshots s2 ON s2.id = d2.snapshot_id
                            WHERE s2.job_id = s.job_id AND d2.id <> d.id
                          )
                        """
                    ).format(extra_from=policy.extra_from, predicate=policy.predicate),
                    (job_id, job_id, *policy.params),
                )
                reset += cursor.rowcount
                _ = connection.execute(
                    "DELETE FROM pipeline_receipts WHERE job_id = %s",
                    (job_id,),
                )
                _ = connection.execute(
                    sql.SQL(
                        """
                        DELETE FROM evaluation_decisions d
                        USING job_snapshots s
                        {extra_from}
                        WHERE d.snapshot_id = s.id
                          AND s.job_id = %s
                          AND {predicate}
                          AND NOT EXISTS (
                            SELECT 1 FROM review_items i WHERE i.evaluation_id = d.id
                          )
                          AND NOT EXISTS (
                            SELECT 1
                            FROM evaluation_decisions d2
                            JOIN job_snapshots s2 ON s2.id = d2.snapshot_id
                            WHERE s2.job_id = s.job_id AND d2.id <> d.id
                          )
                        """
                    ).format(extra_from=policy.extra_from, predicate=policy.predicate),
                    (job_id, *policy.params),
                )
        finally:
            if connection.info.transaction_status != TransactionStatus.INERROR:
                _ = connection.execute(
                    "ALTER TABLE evaluation_decisions ENABLE TRIGGER evaluation_decisions_are_immutable"
                )
                _ = connection.execute(
                    "ALTER TABLE pipeline_receipts ENABLE TRIGGER pipeline_receipts_are_immutable"
                )
    return reset
