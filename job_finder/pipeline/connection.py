from __future__ import annotations

import psycopg

Connection = psycopg.Connection[tuple[object, ...]]


def require_autocommit(connection: Connection, *, operation: str | None = None) -> None:
    if not connection.autocommit:
        message = (
            "Pipeline state operations require an autocommit connection"
            if operation is None
            else f"{operation} requires an autocommit connection"
        )
        raise ValueError(message)


def acquire_transaction_lock(connection: Connection, key: str) -> None:
    _ = connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (key,),
    ).fetchone()
