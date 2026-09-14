from __future__ import annotations

from uuid import UUID

from psycopg import Connection, sql
from psycopg.pq import TransactionStatus

from job_finder.jobs.decision_pipeline import THIN_BODY_THRESHOLD

_MIS_TITLED_REASON_PREFIXES = (
    "Title does not identify a role (",
    "Generic / talent-pool title (",
)
_CHUNK = 200

_THIN_BODY_FROM = "JOIN snapshot_corrections sc ON sc.snapshot_id = s.id"
_THIN_BODY_PREDICATE = "length(s.description) < %s AND sc.description IS NOT NULL"
_THIN_BODY_PARAMS: tuple[object, ...] = (THIN_BODY_THRESHOLD,)

ResetSelection = tuple[sql.SQL, sql.SQL, tuple[object, ...]]


def _require_autocommit(connection: Connection[tuple[object, ...]]) -> None:
    if not connection.autocommit:
        raise ValueError("Reprocessing requires an autocommit connection")


def _mis_titled_predicates() -> tuple[str, ...]:
    return tuple(f"{prefix}%" for prefix in _MIS_TITLED_REASON_PREFIXES)


def _mis_titled_selection() -> ResetSelection:
    return (
        sql.SQL(""),
        sql.SQL("d.outcome = 'rejected' AND (d.reason LIKE %s OR d.reason LIKE %s)"),
        _mis_titled_predicates(),
    )


def _thin_body_selection() -> ResetSelection:
    return sql.SQL(_THIN_BODY_FROM), sql.SQL(_THIN_BODY_PREDICATE), _THIN_BODY_PARAMS


def select_mis_titled_jobs(connection: Connection[tuple[object, ...]]) -> tuple[UUID, ...]:
    rows = connection.execute(
        """
        SELECT s.job_id
        FROM evaluation_decisions d
        JOIN job_snapshots s ON s.id = d.snapshot_id
        JOIN job_work_items w ON w.job_id = s.job_id
        WHERE d.outcome = 'rejected'
          AND (d.reason LIKE %s OR d.reason LIKE %s)
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
        """,
        _mis_titled_predicates(),
    ).fetchall()
    return tuple(UUID(str(row[0])) for row in rows)


def select_thin_body_jobs(connection: Connection[tuple[object, ...]]) -> tuple[UUID, ...]:
    rows = connection.execute(
        f"""
        SELECT s.job_id
        FROM evaluation_decisions d
        JOIN job_snapshots s ON s.id = d.snapshot_id
        {_THIN_BODY_FROM}
        JOIN job_work_items w ON w.job_id = s.job_id
        WHERE {_THIN_BODY_PREDICATE}
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
        """,
        _THIN_BODY_PARAMS,
    ).fetchall()
    return tuple(UUID(str(row[0])) for row in rows)


def reset_mis_titled_jobs(
    connection: Connection[tuple[object, ...]], job_ids: tuple[UUID, ...]
) -> int:
    return _reset_jobs(connection, job_ids, _mis_titled_selection())


def reset_thin_body_jobs(
    connection: Connection[tuple[object, ...]], job_ids: tuple[UUID, ...]
) -> int:
    return _reset_jobs(connection, job_ids, _thin_body_selection())


def _reset_jobs(
    connection: Connection[tuple[object, ...]],
    job_ids: tuple[UUID, ...],
    selection: ResetSelection,
) -> int:
    _require_autocommit(connection)
    reset = 0
    for start in range(0, len(job_ids), _CHUNK):
        reset += _reset_chunk(connection, job_ids[start : start + _CHUNK], selection)
    return reset


def _reset_chunk(
    connection: Connection[tuple[object, ...]],
    job_ids: tuple[UUID, ...],
    selection: ResetSelection,
) -> int:
    if not job_ids:
        return 0
    extra_from, predicate, predicate_params = selection
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
                            completed_at = NULL
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
                    ).format(extra_from=extra_from, predicate=predicate),
                    (job_id, job_id, *predicate_params),
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
                    ).format(extra_from=extra_from, predicate=predicate),
                    (job_id, *predicate_params),
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
