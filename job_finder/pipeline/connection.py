from __future__ import annotations

import psycopg

Connection = psycopg.Connection[tuple[object, ...]]


def require_autocommit(connection: Connection) -> None:
    if not connection.autocommit:
        raise ValueError("Pipeline state operations require an autocommit connection")
