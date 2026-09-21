from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from typing import LiteralString, cast

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

MIGRATIONS_PATH = Path(__file__).with_name("migrations")
SEARCH_CONFIGURATION_MIGRATION = "0016_search_configuration_revisions.sql"
SEARCH_CONFIGURATION_PUBLICATION_MIGRATION = "0017_search_configuration_publications.sql"
PIPELINE_RUN_CONFIGURATION_MIGRATION = "0020_pipeline_run_configuration_revisions.sql"
RELEASE_TARGET_LIFECYCLE_MIGRATION = "0026_release_target_lifecycle.sql"
INITIAL_SEARCH_CONFIGURATION_REVISION_ID = (
    "621346c249608e7d8766902c2cd9fbcfb83ac687f58de8a8fdb7f81980a14099"
)


class SchemaMigrationError(RuntimeError):
    """The database schema does not match the migration history."""


def apply_migrations(connection: psycopg.Connection[tuple[object, ...]]) -> tuple[str, ...]:
    with connection.transaction():
        _ = connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext('job_finder_schema_migrations'))"
        )
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
        if path.name == PIPELINE_RUN_CONFIGURATION_MIGRATION:
            _backfill_search_configuration_publications(connection)
        _ = connection.execute(sql.SQL(cast(LiteralString, content.decode())), prepare=False)
        if path.name == SEARCH_CONFIGURATION_MIGRATION:
            _seed_initial_search_configuration(connection)
        if path.name == SEARCH_CONFIGURATION_PUBLICATION_MIGRATION:
            _backfill_search_configuration_publications(connection)
        if path.name == RELEASE_TARGET_LIFECYCLE_MIGRATION:
            _seed_initial_release_target(connection)
        _ = connection.execute(
            "INSERT INTO job_finder_schema_migrations (name, sha256) VALUES (%s, %s)",
            (path.name, digest),
        )
        migration_names.append(path.name)
    unknown = sorted(set(applied) - set(migration_names))
    if unknown:
        raise SchemaMigrationError(f"Database contains unknown migrations: {', '.join(unknown)}")
    return tuple(migration_names)


def _seed_initial_search_configuration(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    from job_finder.search_configuration import (  # noqa: PLC0415
        DEFAULT_SEARCH_CONFIGURATION,
        search_configuration_revision_id,
    )

    # This default is migration data, not a live application fallback. Its fixed hash
    # makes any future source edit fail tests and migration before changing fresh installs.
    revision_id = search_configuration_revision_id(DEFAULT_SEARCH_CONFIGURATION)
    if revision_id != INITIAL_SEARCH_CONFIGURATION_REVISION_ID:
        raise SchemaMigrationError("Initial search configuration changed after migration 0016")
    content = Jsonb(DEFAULT_SEARCH_CONFIGURATION.model_dump(mode="json"))
    actor = f"migration:{SEARCH_CONFIGURATION_MIGRATION}"
    _ = connection.execute(
        """
        INSERT INTO search_configuration_revisions (id, content, created_at, created_by)
        VALUES (%s, %s, CURRENT_TIMESTAMP, %s)
        """,
        (revision_id, content, actor),
    )
    _ = connection.execute(
        """
        INSERT INTO search_configuration_drafts (
          singleton_id, base_revision_id, version, content, updated_at, updated_by
        ) VALUES (1, %s, 0, %s, CURRENT_TIMESTAMP, %s)
        """,
        (revision_id, content, actor),
    )
    _ = connection.execute(
        """
        INSERT INTO active_search_configuration (
          singleton_id, revision_id, generation, activated_at, activated_by
        ) VALUES (1, %s, 0, CURRENT_TIMESTAMP, %s)
        """,
        (revision_id, actor),
    )


def _backfill_search_configuration_publications(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    from job_finder.evaluation.prompt_releases import (  # noqa: PLC0415
        build_prompt_release,
        store_prompt_release,
    )
    from job_finder.search_configuration import (  # noqa: PLC0415
        SearchConfigurationRevisionId,
        load_search_configuration_publication,
        load_search_configuration_revision,
    )

    actor = f"migration:{SEARCH_CONFIGURATION_PUBLICATION_MIGRATION}"
    published_at_row = connection.execute("SELECT CURRENT_TIMESTAMP").fetchone()
    assert published_at_row is not None
    published_at = cast(datetime, published_at_row[0])
    revision_ids = connection.execute(
        """
        SELECT base_revision_id FROM search_configuration_drafts
        UNION
        SELECT revision_id FROM active_search_configuration
        UNION
        SELECT %s::CHAR(64)
        ORDER BY 1
        """,
        (INITIAL_SEARCH_CONFIGURATION_REVISION_ID,),
    ).fetchall()
    for row in revision_ids:
        revision = load_search_configuration_revision(
            connection, SearchConfigurationRevisionId(str(row[0]))
        )
        release = build_prompt_release(revision.configuration)
        _ = store_prompt_release(
            connection,
            release,
            created_at=published_at,
            created_by=actor,
        )
        _ = connection.execute(
            """
            INSERT INTO search_configuration_publications (
              revision_id, prompt_release_id, published_at, published_by
            ) VALUES (%s, %s, %s, %s)
            ON CONFLICT (revision_id) DO NOTHING
            """,
            (revision.id, release.id, published_at, actor),
        )
        publication = load_search_configuration_publication(connection, revision.id)
        if publication.prompt_release_id != release.id:
            raise SchemaMigrationError(
                f"Published search configuration differs from revision {revision.id}"
            )


def _seed_initial_release_target(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    from job_finder.evaluation.models import PromptReleaseId, ReleaseTarget  # noqa: PLC0415
    from job_finder.evaluation.prompt_releases import load_prompt_release  # noqa: PLC0415
    from job_finder.evaluation.relevance_releases import (  # noqa: PLC0415
        RelevanceReleaseError,
        build_jev_atomic_policy,
        build_jev_faithful_policy,
        build_relevance_release,
        store_relevance_release,
        validate_release_target,
    )

    actor = f"migration:{RELEASE_TARGET_LIFECYCLE_MIGRATION}"
    row = connection.execute(
        """
        SELECT publication.prompt_release_id, active.activated_at
        FROM active_search_configuration active
        JOIN search_configuration_publications publication
          ON publication.revision_id = active.revision_id
        WHERE active.singleton_id = 1
        """
    ).fetchone()
    if row is None:
        row = connection.execute(
            """
            SELECT id, created_at
            FROM prompt_releases
            ORDER BY created_at, id
            LIMIT 1
            """
        ).fetchone()
    if row is None:
        raise SchemaMigrationError("Release-target bootstrap requires a prompt release")
    prompt_release = load_prompt_release(connection, PromptReleaseId(str(row[0])))
    relevance_data = build_relevance_release(build_jev_atomic_policy())
    target = ReleaseTarget(
        prompt_release_id=prompt_release.id,
        relevance_release_id=relevance_data.id,
    )
    try:
        validate_release_target(target, prompt_release, relevance_data)
    except RelevanceReleaseError:
        relevance_data = build_relevance_release(build_jev_faithful_policy(prompt_release))
        target = ReleaseTarget(
            prompt_release_id=prompt_release.id,
            relevance_release_id=relevance_data.id,
        )
        validate_release_target(target, prompt_release, relevance_data)
    relevance = store_relevance_release(
        connection,
        relevance_data,
        created_at=cast(datetime, row[1]),
        created_by=actor,
    )
    if relevance.id != target.relevance_release_id:
        raise SchemaMigrationError("Stored bootstrap relevance release changed identity")
    _ = connection.execute(
        """
        INSERT INTO active_release_target (
          singleton_id, prompt_release_id, relevance_release_id,
          generation, activated_at, activated_by
        ) VALUES (1, %s, %s, 0, %s, %s)
        """,
        (target.prompt_release_id, target.relevance_release_id, row[1], actor),
    )
    _ = connection.execute(
        "ALTER TABLE pipeline_runs VALIDATE CONSTRAINT pipeline_runs_configuration_revision_fk"
    )
