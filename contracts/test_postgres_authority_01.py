from __future__ import annotations

from contracts.test_postgres_authority import (
    AcquisitionPolicy,
    DEFAULT_SEARCH_CONFIGURATION,
    EXPECTED_MIGRATIONS,
    Jsonb,
    QualificationDefinition,
    UTC,
    _apply_migrations_through,
    _connection,
    _public_tables,
    _store_default_qualification_target,
    _table_contents,
    acquisition_policy_revision_id,
    apply_migrations,
    build_prompt_release,
    build_qualification_target,
    build_search_configuration_revision,
    datetime,
    project_legacy_search_configuration,
    psycopg,
    pytest,
    qualification_definition_revision_id,
    qualification_target_id,
    store_component_release,
    store_prompt_release,
    store_qualification_target,
    store_search_configuration_revision,
    timedelta,
)

pytest_plugins = ("contracts.test_postgres_authority",)


def test_migrations_are_repeatable(authority_schema: str) -> None:
    with _connection(authority_schema) as connection:
        first = apply_migrations(connection)
        second = apply_migrations(connection)

        assert first == EXPECTED_MIGRATIONS
        assert list(first) == sorted(first)
        assert second == first
        assert connection.execute(
            "SELECT count(*) FROM job_finder_schema_migrations"
        ).fetchone() == (len(EXPECTED_MIGRATIONS),)
        assert connection.execute(
            "SELECT stage, password_hash FROM owner_onboarding WHERE singleton_id = 1"
        ).fetchone() == ("owner_account", None)


def test_policy_projection_migration_preserves_legacy_rows_and_repeats(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 25, tzinfo=UTC)
    original = DEFAULT_SEARCH_CONFIGURATION
    configurations = (
        original,
        original.model_copy(update={"search_keywords": (*original.search_keywords, "a new role")}),
        original.model_copy(
            update={
                "personal_criteria": (
                    original.personal_criteria[0].model_copy(update={"name": "Renamed criterion"}),
                    *original.personal_criteria[1:],
                )
            }
        ),
    )
    with _connection(authority_schema) as connection:
        _apply_migrations_through(connection, "0037_run_budget_authority_guard.sql")
        revisions = tuple(
            store_search_configuration_revision(
                connection,
                build_search_configuration_revision(
                    configuration, created_at=now, created_by="migration-test"
                ),
            )
            for configuration in configurations
        )
        legacy_tables = _public_tables(connection)
        legacy_rows = _table_contents(connection, legacy_tables)

        assert apply_migrations(connection) == EXPECTED_MIGRATIONS
        assert _table_contents(connection, legacy_tables) == legacy_rows
        assert connection.execute(
            "SELECT count(*) FROM legacy_search_configuration_policy_projections"
        ).fetchone() == (3,)
        assert connection.execute(
            "SELECT count(*) FROM acquisition_policy_revisions"
        ).fetchone() == (2,)
        assert connection.execute(
            "SELECT count(*) FROM qualification_definition_revisions"
        ).fetchone() == (2,)

        for revision in revisions:
            projection = project_legacy_search_configuration(revision.configuration)
            row = connection.execute(
                """
                SELECT bridge.acquisition_policy_revision_id, acquisition.content,
                       bridge.qualification_definition_revision_id, qualification.content
                FROM legacy_search_configuration_policy_projections bridge
                JOIN acquisition_policy_revisions acquisition
                  ON acquisition.id = bridge.acquisition_policy_revision_id
                JOIN qualification_definition_revisions qualification
                  ON qualification.id = bridge.qualification_definition_revision_id
                WHERE bridge.legacy_revision_id = %s
                """,
                (revision.id,),
            ).fetchone()
            assert row is not None
            acquisition = AcquisitionPolicy.model_validate(row[1])
            qualification = QualificationDefinition.model_validate(row[3])
            assert row[0] == acquisition_policy_revision_id(projection.acquisition)
            assert row[2] == qualification_definition_revision_id(projection.qualification)
            assert acquisition == projection.acquisition
            assert qualification == projection.qualification

        projection_tables = (
            "acquisition_policy_revisions",
            "qualification_definition_revisions",
            "legacy_search_configuration_policy_projections",
        )
        projected_rows = _table_contents(connection, projection_tables)
        _ = connection.execute("SELECT backfill_legacy_search_configuration_policies()")
        assert _table_contents(connection, projection_tables) == projected_rows
        assert _table_contents(connection, legacy_tables) == legacy_rows

        later_revision = store_search_configuration_revision(
            connection,
            build_search_configuration_revision(
                original.model_copy(
                    update={
                        "enabled_sources": tuple(reversed(original.enabled_sources)),
                        "target_profiles": (
                            original.target_profiles[0].model_copy(
                                update={"name": "Later profile name"}
                            ),
                            *original.target_profiles[1:],
                        ),
                    }
                ),
                created_at=now + timedelta(days=1),
                created_by="migration-test",
            ),
        )
        _ = connection.execute("SELECT backfill_legacy_search_configuration_policies()")
        assert connection.execute(
            """
            SELECT count(*) FROM legacy_search_configuration_policy_projections
            WHERE legacy_revision_id = %s
            """,
            (later_revision.id,),
        ).fetchone() == (1,)
        caught_up_rows = _table_contents(connection, projection_tables)
        _ = connection.execute("SELECT backfill_legacy_search_configuration_policies()")
        assert _table_contents(connection, projection_tables) == caught_up_rows


def test_split_policy_state_seeds_without_changing_legacy_authority(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 25, tzinfo=UTC)
    first = DEFAULT_SEARCH_CONFIGURATION
    second = first.model_copy(update={"search_keywords": (*first.search_keywords, "another role")})
    with _connection(authority_schema) as connection:
        _apply_migrations_through(connection, "0038_policy_revisions.sql")
        first_revision = store_search_configuration_revision(
            connection,
            build_search_configuration_revision(first, created_at=now, created_by="owner"),
        )
        second_revision = store_search_configuration_revision(
            connection,
            build_search_configuration_revision(
                second, created_at=now + timedelta(seconds=1), created_by="owner"
            ),
        )
        prompt = build_prompt_release(first)
        _ = store_prompt_release(connection, prompt, created_at=now, created_by="owner")
        alternate_prompt = build_prompt_release(
            first.model_copy(
                update={
                    "target_profiles": (
                        first.target_profiles[0].model_copy(
                            update={"instructions": "Revised profile instructions."}
                        ),
                        *first.target_profiles[1:],
                    )
                }
            )
        )
        _ = store_prompt_release(connection, alternate_prompt, created_at=now, created_by="owner")
        assert alternate_prompt.id != prompt.id
        for revision, release in ((first_revision, prompt), (second_revision, alternate_prompt)):
            _ = connection.execute(
                """
                INSERT INTO search_configuration_publications (
                  revision_id, prompt_release_id, published_at, published_by
                ) VALUES (%s, %s, %s, 'owner')
                """,
                (revision.id, release.id, now),
            )
        _ = connection.execute(
            """
            INSERT INTO search_configuration_drafts (
              singleton_id, base_revision_id, version, content, updated_at, updated_by
            ) VALUES (1, %s, 7, %s, %s, 'owner')
            """,
            (second_revision.id, Jsonb(second.model_dump(mode="json")), now),
        )
        _ = connection.execute(
            """
            INSERT INTO active_search_configuration (
              singleton_id, revision_id, generation, activated_at, activated_by
            ) VALUES (1, %s, 4, %s, 'owner')
            """,
            (second_revision.id, now),
        )
        legacy_tables = tuple(
            table
            for table in _public_tables(connection)
            if table
            not in {
                "acquisition_policy_revisions",
                "qualification_definition_revisions",
                "legacy_search_configuration_policy_projections",
            }
        )
        legacy_rows = _table_contents(connection, legacy_tables)

        assert apply_migrations(connection) == EXPECTED_MIGRATIONS
        assert _table_contents(connection, legacy_tables) == legacy_rows
        projection = project_legacy_search_configuration(second)
        acquisition_id = acquisition_policy_revision_id(projection.acquisition)
        qualification_id = qualification_definition_revision_id(projection.qualification)
        assert connection.execute(
            "SELECT revision_id, generation FROM active_acquisition_policy"
        ).fetchone() == (acquisition_id, 0)
        assert connection.execute(
            "SELECT base_revision_id, version, content FROM acquisition_policy_drafts"
        ).fetchone() == (acquisition_id, 0, projection.acquisition.model_dump(mode="json"))
        assert connection.execute(
            "SELECT base_revision_id, version, content FROM qualification_definition_drafts"
        ).fetchone() == (qualification_id, 0, projection.qualification.model_dump(mode="json"))
        assert connection.execute(
            "SELECT count(*) FROM legacy_search_configuration_publication_projections"
        ).fetchone() == (2,)
        assert connection.execute(
            "SELECT count(*) FROM acquisition_policy_publications"
        ).fetchone() == (2,)
        assert connection.execute(
            "SELECT count(*) FROM qualification_definition_publications"
        ).fetchone() == (1,)

        split_tables = (
            "acquisition_policy_publications",
            "qualification_definition_publications",
            "legacy_search_configuration_publication_projections",
            "acquisition_policy_drafts",
            "qualification_definition_drafts",
            "active_acquisition_policy",
        )
        seeded_rows = _table_contents(connection, split_tables)
        _ = connection.execute("SELECT backfill_legacy_search_configuration_publications()")
        assert _table_contents(connection, split_tables) == seeded_rows
        assert _table_contents(connection, legacy_tables) == legacy_rows

        later = second.model_copy(
            update={"search_keywords": (*second.search_keywords, "later role")}
        )
        later_revision = store_search_configuration_revision(
            connection,
            build_search_configuration_revision(
                later, created_at=now + timedelta(days=1), created_by="owner"
            ),
        )
        _ = connection.execute(
            """
            INSERT INTO search_configuration_publications (
              revision_id, prompt_release_id, published_at, published_by
            ) VALUES (%s, %s, %s, 'owner')
            """,
            (later_revision.id, prompt.id, now + timedelta(days=1)),
        )
        _ = connection.execute("SELECT backfill_legacy_search_configuration_publications()")
        assert connection.execute(
            """
            SELECT count(*) FROM legacy_search_configuration_publication_projections
            WHERE legacy_revision_id = %s
            """,
            (later_revision.id,),
        ).fetchone() == (1,)
        assert _table_contents(connection, split_tables[3:]) == {
            table: seeded_rows[table] for table in split_tables[3:]
        }

        with pytest.raises(psycopg.errors.CheckViolation):
            _ = connection.execute("UPDATE acquisition_policy_drafts SET version = version + 2")
        with pytest.raises(psycopg.errors.CheckViolation):
            _ = connection.execute(
                "UPDATE active_acquisition_policy SET generation = generation + 1"
            )


def test_composite_promotion_authority_starts_unactivated_and_is_immutable(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 25, tzinfo=UTC)
    with _connection(authority_schema) as connection:
        _ = apply_migrations(connection)
        assert connection.execute(
            "SELECT target_id, generation, activated_at, activated_by FROM active_qualification_target WHERE singleton_id = 1"
        ).fetchone() == (None, 0, None, None)
        artifact, components, baseline = _store_default_qualification_target(connection, now)
        alternate_input = components[0].model_copy(
            update={"ats_sources": components[0].ats_sources[:-1]}
        )
        _ = store_component_release(connection, alternate_input, created_at=now, created_by="owner")
        candidate = build_qualification_target(alternate_input, *components[1:])
        _ = store_qualification_target(connection, candidate, created_at=now, created_by="owner")
        assert artifact.id == baseline.artifact_id == candidate.artifact_id
        values = (
            "e" * 64,
            "rejected-case",
            qualification_target_id(baseline),
            qualification_target_id(candidate),
            "rejected",
            "Missing evidence",
            "owner",
            now,
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            _ = connection.execute(
                """INSERT INTO qualification_promotion_decisions (
                  id, idempotency_key, baseline_target_id, candidate_target_id,
                  decision, reason, actor, created_at
                ) VALUES (%s, %s, %s, %s, 'approved', %s, %s, %s)""",
                values[:4] + values[5:],
            )
        _ = connection.execute(
            """INSERT INTO qualification_promotion_decisions (
              id, idempotency_key, baseline_target_id, candidate_target_id,
              decision, reason, actor, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            values,
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            _ = connection.execute(
                "UPDATE qualification_promotion_decisions SET reason = 'changed'"
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            _ = connection.execute("UPDATE active_qualification_target SET generation = 1")
        assert connection.execute("SELECT count(*) FROM prompt_promotion_decisions").fetchone() == (
            0,
        )
