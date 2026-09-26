from __future__ import annotations

from contracts.test_postgres_authority import (
    ActivateConfigurationCommand,
    ActivationTargetUnpublished,
    ActiveConfigurationChanged,
    Barrier,
    ConfigurationActivated,
    ConfigurationPublished,
    DEFAULT_SEARCH_CONFIGURATION,
    PublishDraftChanged,
    RelevanceReleaseError,
    SearchConfigurationRevisionId,
    ThreadPoolExecutor,
    UTC,
    _connection,
    _insert_run,
    _insert_run_and_job,
    _publication_command,
    _publish_configuration_revision,
    activate_search_configuration,
    apply_migrations,
    bootstrap_prompt_release,
    build_gemini_policy,
    build_jev_faithful_policy,
    build_prompt_release,
    build_relevance_release,
    build_search_configuration_revision,
    compare_and_swap_active_search_configuration,
    datetime,
    load_active_search_configuration,
    load_prompt_release,
    load_published_active_search_configuration,
    load_relevance_release,
    load_search_configuration_draft,
    psycopg,
    publish_search_configuration,
    pytest,
    replace_search_configuration_draft,
    search_configuration_revision_id,
    store_prompt_release,
    store_relevance_release,
    store_search_configuration_revision,
    timedelta,
    uuid4,
)

pytest_plugins = ("contracts.test_postgres_authority",)


def test_configuration_publication_rolls_back_every_write(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    changed = DEFAULT_SEARCH_CONFIGURATION.model_copy(
        update={
            "search_keywords": ("rollback",),
            "target_profiles": (
                DEFAULT_SEARCH_CONFIGURATION.target_profiles[0].model_copy(
                    update={"instructions": "A profile that must roll back."}
                ),
                *DEFAULT_SEARCH_CONFIGURATION.target_profiles[1:],
            ),
        }
    )
    release = build_prompt_release(changed)
    default_release = build_prompt_release(DEFAULT_SEARCH_CONFIGURATION)
    new_prompt_version_ids = {version.id for version in release.versions} - {
        version.id for version in default_release.versions
    }
    assert new_prompt_version_ids
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        draft = load_search_configuration_draft(connection)
        saved = replace_search_configuration_draft(
            connection,
            expected_version=draft.version,
            base_revision_id=draft.base_revision_id,
            configuration=changed,
            updated_at=now,
            updated_by="owner",
        )
        assert saved is not None
        revision_id = search_configuration_revision_id(changed)
        connection.execute(
            """
            CREATE FUNCTION fail_configuration_publication_receipt()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
              RAISE EXCEPTION 'publication receipt insertion failed';
            END;
            $$
            """
        )
        connection.execute(
            """
            CREATE TRIGGER fail_configuration_publication_receipt
            AFTER INSERT ON search_configuration_publication_receipts
            FOR EACH ROW EXECUTE FUNCTION fail_configuration_publication_receipt()
            """
        )
        with pytest.raises(psycopg.errors.RaiseException, match="receipt insertion failed"):
            publish_search_configuration(
                connection,
                _publication_command(saved, "publish:rollback", now),
            )

        assert load_search_configuration_draft(connection) == saved
        assert connection.execute(
            "SELECT count(*) FROM search_configuration_revisions WHERE id = %s", (revision_id,)
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM prompt_releases WHERE id = %s", (release.id,)
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM prompt_versions WHERE id = ANY(%s)",
            (list(new_prompt_version_ids),),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM search_configuration_publications WHERE revision_id = %s",
            (revision_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM search_configuration_publication_receipts"
        ).fetchone() == (0,)


def test_concurrent_configuration_publications_serialize_retries_and_drafts(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        draft = load_search_configuration_draft(connection)
    command = _publication_command(draft, "publish:concurrent", now)
    start = Barrier(2)

    def publish_same(_index: int) -> ConfigurationPublished:
        with _connection(authority_schema) as connection:
            _ = start.wait()
            result = publish_search_configuration(connection, command)
            assert isinstance(result, ConfigurationPublished)
            return result

    with ThreadPoolExecutor(max_workers=2) as executor:
        same_key = tuple(executor.map(publish_same, range(2)))
    assert sorted(result.replayed for result in same_key) == [False, True]
    assert same_key[0].rebased_draft == same_key[1].rebased_draft

    with _connection(authority_schema) as connection:
        current = load_search_configuration_draft(connection)
    start = Barrier(2)

    def publish_different(index: int) -> ConfigurationPublished | PublishDraftChanged:
        with _connection(authority_schema) as connection:
            _ = start.wait()
            result = publish_search_configuration(
                connection,
                _publication_command(current, f"publish:different:{index}", now),
            )
            assert isinstance(result, (ConfigurationPublished, PublishDraftChanged))
            return result

    with ThreadPoolExecutor(max_workers=2) as executor:
        different_keys = tuple(executor.map(publish_different, range(2)))
    assert sum(isinstance(result, ConfigurationPublished) for result in different_keys) == 1
    assert sum(isinstance(result, PublishDraftChanged) for result in different_keys) == 1


def test_configuration_activation_returns_published_state_and_strict_cas_results(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        initial = load_published_active_search_configuration(connection)
        draft = load_search_configuration_draft(connection)
        changed_draft = replace_search_configuration_draft(
            connection,
            expected_version=draft.version,
            base_revision_id=draft.base_revision_id,
            configuration=draft.configuration.model_copy(update={"search_keywords": ("activate",)}),
            updated_at=now,
            updated_by="owner",
        )
        assert changed_draft is not None
        published = publish_search_configuration(
            connection,
            _publication_command(changed_draft, "publish:activate", now),
        )
        assert isinstance(published, ConfigurationPublished)

        unpublished = activate_search_configuration(
            connection,
            ActivateConfigurationCommand(
                target_revision_id=SearchConfigurationRevisionId("f" * 64),
                expected_active_revision_id=initial.active.revision.id,
                expected_generation=initial.active.generation,
                actor="owner",
                timestamp=now,
            ),
        )
        assert isinstance(unpublished, ActivationTargetUnpublished)

        activated = activate_search_configuration(
            connection,
            ActivateConfigurationCommand(
                target_revision_id=published.publication.revision_id,
                expected_active_revision_id=initial.active.revision.id,
                expected_generation=initial.active.generation,
                actor="owner",
                timestamp=now,
            ),
        )
        assert isinstance(activated, ConfigurationActivated)
        assert activated.active_configuration.publication == published.publication

        stale = activate_search_configuration(
            connection,
            ActivateConfigurationCommand(
                target_revision_id=initial.active.revision.id,
                expected_active_revision_id=initial.active.revision.id,
                expected_generation=initial.active.generation,
                actor="owner",
                timestamp=now,
            ),
        )
        assert isinstance(stale, ActiveConfigurationChanged)
        assert stale.active_configuration == activated.active_configuration


def test_publications_are_immutable_and_pointers_require_publication(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        initial = load_active_search_configuration(connection)
        unpublished = build_search_configuration_revision(
            DEFAULT_SEARCH_CONFIGURATION.model_copy(update={"search_keywords": ("unpublished",)}),
            created_at=now,
            created_by="owner",
        )
        _ = store_search_configuration_revision(connection, unpublished)

        with pytest.raises(
            psycopg.errors.ForeignKeyViolation,
            match="search_configuration_drafts_base_revision_publication_fk",
        ):
            connection.execute(
                """
                UPDATE search_configuration_drafts
                SET base_revision_id = %s, version = version + 1
                WHERE singleton_id = 1
                """,
                (unpublished.id,),
            )
        with pytest.raises(
            psycopg.errors.ForeignKeyViolation,
            match="active_search_configuration_revision_publication_fk",
        ):
            connection.execute(
                """
                UPDATE active_search_configuration
                SET revision_id = %s, generation = generation + 1
                WHERE singleton_id = 1
                """,
                (unpublished.id,),
            )
        with pytest.raises(psycopg.errors.CheckViolation, match="immutable"):
            connection.execute(
                """
                UPDATE search_configuration_publications
                SET published_by = 'other'
                WHERE revision_id = %s
                """,
                (initial.revision.id,),
            )


def test_search_configuration_revision_and_pointer_invariants(authority_schema: str) -> None:
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        initial = load_active_search_configuration(connection)
        changed_configuration = DEFAULT_SEARCH_CONFIGURATION.model_copy(
            update={
                "search_keywords": (*DEFAULT_SEARCH_CONFIGURATION.search_keywords, "cto café"),
                "personal_criteria": (
                    DEFAULT_SEARCH_CONFIGURATION.personal_criteria[0].model_copy(
                        update={"name": "Éligibilité géographique"}
                    ),
                    *DEFAULT_SEARCH_CONFIGURATION.personal_criteria[1:],
                ),
            }
        )
        changed = build_search_configuration_revision(
            changed_configuration,
            created_at=now,
            created_by="owner",
        )
        assert store_search_configuration_revision(connection, changed) == changed
        assert store_search_configuration_revision(connection, changed) == changed
        _publish_configuration_revision(connection, changed, now)

        with pytest.raises(psycopg.errors.CheckViolation, match="immutable"):
            connection.execute(
                "UPDATE search_configuration_revisions SET created_by = 'other' WHERE id = %s",
                (changed.id,),
            )

        draft = replace_search_configuration_draft(
            connection,
            expected_version=0,
            base_revision_id=initial.revision.id,
            configuration=changed_configuration,
            updated_at=now,
            updated_by="owner",
        )
        assert draft is not None
        assert draft.version == 1
        assert (
            replace_search_configuration_draft(
                connection,
                expected_version=0,
                base_revision_id=initial.revision.id,
                configuration=DEFAULT_SEARCH_CONFIGURATION,
                updated_at=now,
                updated_by="stale-owner",
            )
            is None
        )

        activated = compare_and_swap_active_search_configuration(
            connection,
            expected_revision_id=initial.revision.id,
            expected_generation=0,
            revision_id=changed.id,
            activated_at=now,
            activated_by="owner",
        )
        assert activated is not None
        assert activated.generation == 1
        assert activated.revision == changed
        restored = compare_and_swap_active_search_configuration(
            connection,
            expected_revision_id=changed.id,
            expected_generation=1,
            revision_id=initial.revision.id,
            activated_at=now + timedelta(seconds=1),
            activated_by="owner",
        )
        assert restored is not None
        assert restored.generation == 2
        assert (
            compare_and_swap_active_search_configuration(
                connection,
                expected_revision_id=initial.revision.id,
                expected_generation=0,
                revision_id=changed.id,
                activated_at=now + timedelta(seconds=2),
                activated_by="stale-owner",
            )
            is None
        )


def test_search_configuration_database_rejects_malformed_content(
    authority_schema: str,
) -> None:
    with _connection(authority_schema) as connection:
        apply_migrations(connection)

        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                """
                INSERT INTO search_configuration_revisions (
                  id, content, created_at, created_by
                ) VALUES (%s, '{"schema_version": 1}'::jsonb, CURRENT_TIMESTAMP, 'owner')
                """,
                ("f" * 64,),
            )

        with pytest.raises(
            psycopg.errors.CheckViolation,
            match="search_configuration_revision_matches_content",
        ):
            connection.execute(
                """
                INSERT INTO search_configuration_revisions (
                  id, content, created_at, created_by
                )
                SELECT %s, content, CURRENT_TIMESTAMP, 'owner'
                FROM search_configuration_revisions
                LIMIT 1
                """,
                ("f" * 64,),
            )


def test_concurrent_search_configuration_activation_has_one_winner(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        initial = load_active_search_configuration(connection)
        revisions = tuple(
            build_search_configuration_revision(
                DEFAULT_SEARCH_CONFIGURATION.model_copy(
                    update={
                        "search_keywords": (
                            *DEFAULT_SEARCH_CONFIGURATION.search_keywords,
                            keyword,
                        )
                    }
                ),
                created_at=now,
                created_by="owner",
            )
            for keyword in ("cto", "vp engineering")
        )
        for revision in revisions:
            _ = store_search_configuration_revision(connection, revision)
            _publish_configuration_revision(connection, revision, now)

    activation_start = Barrier(2)

    def activate(revision_index: int) -> bool:
        with _connection(authority_schema) as connection:
            _ = activation_start.wait()
            return isinstance(
                activate_search_configuration(
                    connection,
                    ActivateConfigurationCommand(
                        target_revision_id=revisions[revision_index].id,
                        expected_active_revision_id=initial.revision.id,
                        expected_generation=0,
                        actor=f"owner-{revision_index}",
                        timestamp=now,
                    ),
                ),
                ConfigurationActivated,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(activate, range(2)))

    assert sorted(results) == [False, True]
    with _connection(authority_schema) as connection:
        active = load_active_search_configuration(connection)
    assert active.generation == 1
    assert active.revision.id in {revision.id for revision in revisions}


def test_transaction_rolls_back_receipt_when_projection_fails(authority_schema: str) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    run_id = uuid4()
    job_id = uuid4()
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        _insert_run_and_job(connection, run_id, job_id, now)

        with pytest.raises(psycopg.IntegrityError):
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO pipeline_receipts (
                      id, idempotency_key, pipeline_run_id, job_id, operation_key,
                      input_digest, output_digest, output, implementation_ref, completed_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, '{}'::jsonb, %s, %s)
                    """,
                    (
                        "a" * 64,
                        "process:one",
                        run_id,
                        job_id,
                        "process_job",
                        "b" * 64,
                        "c" * 64,
                        "test-ref",
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO langfuse_projection_items (
                      id, kind, source_id, payload_digest, payload, state, created_at
                    ) VALUES (%s, 'trace', %s, %s, '{}'::jsonb, 'invalid', %s)
                    """,
                    ("d" * 64, str(job_id), "e" * 64, now),
                )

        assert connection.execute("SELECT count(*) FROM pipeline_receipts").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM langfuse_projection_items").fetchone() == (
            0,
        )


def test_immutable_review_target_rejects_rewrites(authority_schema: str) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    run_id = uuid4()
    job_id = uuid4()
    snapshot_id = "1" * 64
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        _insert_run_and_job(connection, run_id, job_id, now)
        connection.execute(
            """
            INSERT INTO job_snapshots (
              id, job_id, content_digest, title, company, normalized_company,
              normalized_title, source, raw_url, description, location, keywords, observed_at
            ) VALUES (%s, %s, %s, 'Engineer', 'Example', 'example', 'engineer', 'other',
              'https://example.com/job', 'Description', 'Remote', '[]'::jsonb, %s)
            """,
            (snapshot_id, job_id, "2" * 64, now),
        )

        with pytest.raises(psycopg.errors.CheckViolation, match="immutable"):
            connection.execute(
                "UPDATE job_snapshots SET title = 'Changed' WHERE id = %s", (snapshot_id,)
            )


def test_run_attempt_identity_treats_a_missing_job_as_equal(authority_schema: str) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    run_id = uuid4()
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        _insert_run(connection, run_id, now)
        values = (uuid4(), run_id, "discover", "3" * 64, now, now)
        connection.execute(
            """
            INSERT INTO processing_attempts (
              id, pipeline_run_id, operation_key, attempt_number, input_digest,
              status, started_at, completed_at
            ) VALUES (%s, %s, %s, 0, %s, 'completed', %s, %s)
            """,
            values,
        )

        with pytest.raises(psycopg.errors.UniqueViolation):
            connection.execute(
                """
                INSERT INTO processing_attempts (
                  id, pipeline_run_id, operation_key, attempt_number, input_digest,
                  status, started_at, completed_at
                ) VALUES (%s, %s, %s, 0, %s, 'completed', %s, %s)
                """,
                (uuid4(), *values[1:]),
            )


def test_prompt_release_requires_the_declared_members(authority_schema: str) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        connection.execute(
            """
            INSERT INTO prompt_versions (
              id, prompt_name, criterion, phase, content_digest, messages,
              input_schema, output_schema, model, parameters, created_at
            ) VALUES (%s, 'profile', 'profile-criterion', 'profile', %s, '[]'::jsonb, '{}'::jsonb, '{}'::jsonb,
              'test-model', '{}'::jsonb, %s)
            """,
            ("4" * 64, "5" * 64, now),
        )
        connection.execute(
            """
            INSERT INTO prompt_versions (
              id, prompt_name, criterion, phase, content_digest, messages,
              input_schema, output_schema, model, parameters, created_at
            ) VALUES (%s, 'other', 'other-criterion', 'profile', %s, '[]'::jsonb, '{}'::jsonb, '{}'::jsonb,
              'test-model', '{}'::jsonb, %s)
            """,
            ("8" * 64, "9" * 64, now),
        )

        with pytest.raises(psycopg.errors.CheckViolation, match="requires 1 members"):
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO prompt_releases (
                      id, name, content_digest, expected_member_count, created_at, created_by
                    ) VALUES (%s, 'release-1', %s, 1, %s, 'test')
                    """,
                    ("6" * 64, "7" * 64, now),
                )

        with connection.transaction():
            connection.execute(
                """
                INSERT INTO prompt_releases (
                  id, name, content_digest, expected_member_count, created_at, created_by
                ) VALUES (%s, 'release-1', %s, 1, %s, 'test')
                """,
                ("6" * 64, "7" * 64, now),
            )
            connection.execute(
                """
                INSERT INTO prompt_release_members (
                  release_id, prompt_name, prompt_version_id, position
                ) VALUES (%s, 'profile', %s, 0)
                """,
                ("6" * 64, "4" * 64),
            )

        with pytest.raises(psycopg.errors.CheckViolation, match="requires 1 members"):
            connection.execute(
                """
                INSERT INTO prompt_release_members (
                  release_id, prompt_name, prompt_version_id, position
                ) VALUES (%s, 'other', %s, 1)
                """,
                ("6" * 64, "8" * 64),
            )


def test_bootstraps_the_complete_prompt_release_idempotently(
    authority_schema: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _connection(authority_schema) as connection:
        apply_migrations(connection)

        first = bootstrap_prompt_release(connection)
        second = bootstrap_prompt_release(connection)

        assert second == first
        monkeypatch.setattr("job_finder.evaluation.prompt_releases.PROMPTS", ())
        assert load_prompt_release(connection, first.id) == first
        assert connection.execute("SELECT count(*) FROM prompt_versions").fetchone() == (8,)
        assert connection.execute("SELECT count(*) FROM prompt_releases").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM prompt_release_members").fetchone() == (8,)


def test_stores_a_custom_prompt_release_exactly_and_idempotently(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    configuration = DEFAULT_SEARCH_CONFIGURATION.model_copy(
        update={
            "personal_criteria": (
                DEFAULT_SEARCH_CONFIGURATION.personal_criteria[0].model_copy(
                    update={"instructions": "Only roles open to candidates in Europe."}
                ),
                *DEFAULT_SEARCH_CONFIGURATION.personal_criteria[1:],
            )
        }
    )
    release = build_prompt_release(configuration)
    with _connection(authority_schema) as connection:
        apply_migrations(connection)

        first = store_prompt_release(connection, release, created_at=now, created_by="contract")
        second = store_prompt_release(
            connection, release, created_at=now + timedelta(minutes=1), created_by="retry"
        )

        assert first == release
        assert second == release
        assert load_prompt_release(connection, release.id) == release
        assert connection.execute(
            "SELECT count(*) FROM prompt_release_members WHERE release_id = %s",
            (release.id,),
        ).fetchone() == (len(release.versions),)
        assert connection.execute(
            "SELECT count(*) FROM prompt_releases WHERE id = %s", (release.id,)
        ).fetchone() == (1,)


def test_stores_an_immutable_relevance_release_exactly_and_idempotently(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 21, 12, tzinfo=UTC)
    prompt_release = build_prompt_release()
    release = build_relevance_release(build_jev_faithful_policy(prompt_release))
    with _connection(authority_schema) as connection:
        apply_migrations(connection)

        first = store_relevance_release(connection, release, created_at=now, created_by="contract")
        second = store_relevance_release(
            connection,
            release,
            created_at=now + timedelta(minutes=1),
            created_by="retry",
        )

        assert first == release
        assert second == release
        assert load_relevance_release(connection, release.id) == release
        assert connection.execute(
            "SELECT created_at, created_by FROM relevance_releases WHERE id = %s",
            (release.id,),
        ).fetchone() == (now, "contract")
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                """
                INSERT INTO relevance_releases (
                  id, content_digest, content, created_at, created_by
                ) VALUES (%s, %s, '{}'::jsonb, %s, 'contract')
                """,
                ("1" * 64, "1" * 64, now),
            )
        with pytest.raises(psycopg.errors.CheckViolation, match="immutable"):
            connection.execute(
                "UPDATE relevance_releases SET created_by = 'other' WHERE id = %s",
                (release.id,),
            )


def test_relevance_release_load_rejects_corrupt_content(authority_schema: str) -> None:
    now = datetime(2026, 9, 21, 12, tzinfo=UTC)
    release = build_relevance_release(build_gemini_policy(build_prompt_release()))
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        store_relevance_release(connection, release, created_at=now, created_by="contract")
        connection.execute(
            "ALTER TABLE relevance_releases DISABLE TRIGGER relevance_releases_are_immutable"
        )
        connection.execute(
            "ALTER TABLE relevance_releases DROP CONSTRAINT relevance_release_digest_matches_content"
        )
        connection.execute(
            'UPDATE relevance_releases SET content = content || \'{"model": "corrupt"}\'::jsonb WHERE id = %s',
            (release.id,),
        )

        with pytest.raises(RelevanceReleaseError, match="identity is corrupt"):
            load_relevance_release(connection, release.id)
