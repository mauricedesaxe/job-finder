from __future__ import annotations

from uuid import UUID

from psycopg import Connection
from psycopg.pq import TransactionStatus

_MIS_TITLED_REASON_PREFIXES = (
    "Title does not identify a role (",
    "Generic / talent-pool title (",
)
_CHUNK = 200


def _require_autocommit(connection: Connection[tuple[object, ...]]) -> None:
    if not connection.autocommit:
        raise ValueError("Reprocessing requires an autocommit connection")


def _mis_titled_predicates() -> tuple[str, ...]:
    return tuple(f"{prefix}%" for prefix in _MIS_TITLED_REASON_PREFIXES)


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


def reset_mis_titled_jobs(
    connection: Connection[tuple[object, ...]], job_ids: tuple[UUID, ...]
) -> int:
    _require_autocommit(connection)
    reset = 0
    for start in range(0, len(job_ids), _CHUNK):
        reset += _reset_chunk(connection, job_ids[start : start + _CHUNK])
    return reset


def _reset_chunk(connection: Connection[tuple[object, ...]], job_ids: tuple[UUID, ...]) -> int:
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
                    """
                    UPDATE job_work_items w
                    SET state = 'pending', owner_token = NULL, lease_expires_at = NULL,
                        retry_at = NULL, terminal_decision_id = NULL, last_error = NULL,
                        completed_at = NULL
                    FROM evaluation_decisions d
                    JOIN job_snapshots s ON s.id = d.snapshot_id
                    WHERE w.job_id = %s
                      AND w.terminal_decision_id = d.id
                      AND s.job_id = %s
                      AND d.outcome = 'rejected'
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
                    """,
                    (job_id, job_id, *_mis_titled_predicates()),
                )
                reset += cursor.rowcount
                _ = connection.execute(
                    "DELETE FROM pipeline_receipts WHERE job_id = %s",
                    (job_id,),
                )
                _ = connection.execute(
                    """
                    DELETE FROM evaluation_decisions d
                    USING job_snapshots s
                    WHERE d.snapshot_id = s.id
                      AND s.job_id = %s
                      AND d.outcome = 'rejected'
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
                    """,
                    (job_id, *_mis_titled_predicates()),
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
