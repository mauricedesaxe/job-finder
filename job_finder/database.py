from __future__ import annotations

import hashlib
from pathlib import Path
from typing import LiteralString, cast

import psycopg
from psycopg import sql

MIGRATIONS_PATH = Path(__file__).with_name("migrations")


class SchemaMigrationError(RuntimeError):
    """The database schema does not match the migration history."""


def apply_migrations(connection: psycopg.Connection[tuple[object, ...]]) -> tuple[str, ...]:
    with connection.transaction():
        return _apply_migrations(connection)


def _apply_migrations(connection: psycopg.Connection[tuple[object, ...]]) -> tuple[str, ...]:
    _ = connection.execute(
        """
        CREATE TABLE IF NOT EXISTS job_finder_schema_migrations (
          name TEXT PRIMARY KEY,
          sha256 CHAR(64) NOT NULL,
          applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    applied = {
        str(row[0]): str(row[1])
        for row in connection.execute(
            "SELECT name, sha256 FROM job_finder_schema_migrations ORDER BY name"
        ).fetchall()
    }
    migration_names: list[str] = []
    for path in sorted(MIGRATIONS_PATH.glob("*.sql")):
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        recorded_digest = applied.get(path.name)
        if recorded_digest is not None:
            if recorded_digest != digest:
                raise SchemaMigrationError(f"Applied migration changed: {path.name}")
            migration_names.append(path.name)
            continue
        _ = connection.execute(sql.SQL(cast(LiteralString, content.decode())), prepare=False)
        _ = connection.execute(
            "INSERT INTO job_finder_schema_migrations (name, sha256) VALUES (%s, %s)",
            (path.name, digest),
        )
        migration_names.append(path.name)
    unknown = sorted(set(applied) - set(migration_names))
    if unknown:
        raise SchemaMigrationError(f"Database contains unknown migrations: {', '.join(unknown)}")
    return tuple(migration_names)
