from __future__ import annotations

from contracts.test_postgres_authority import (
    ConfigurationPublished,
    ConfigurationRevisionCursor,
    ConfigurationRevisionNotFound,
    DEFAULT_SEARCH_CONFIGURATION,
    DraftChanged,
    DraftSaved,
    INITIAL_SEARCH_CONFIGURATION_REVISION_ID,
    Jsonb,
    ManifestPolicy,
    PublicationIdempotencyKeyConflict,
    PublishConfigurationCommand,
    PublishDraftChanged,
    ReviewSaved,
    ReviewSubmission,
    SaveDraftCommand,
    SearchConfigurationDraft,
    SearchConfigurationRevisionId,
    UTC,
    UUID,
    _apply_migrations_through,
    _connection,
    _insert_legacy_orchestration_run,
    _insert_prompt_run,
    _insert_review_decision,
    _public_tables,
    _publication_command,
    _publish_configuration_revision,
    _seed_initial_configuration_publication,
    _table_contents,
    apply_migrations,
    bootstrap_prompt_release,
    build_prompt_release,
    build_search_configuration_revision,
    cast,
    configuration_service_module,
    create_manifest,
    datetime,
    enqueue_qualified_review_item,
    get_active_search_configuration,
    get_search_configuration_draft,
    get_search_configuration_revision,
    include_review_event,
    list_search_configuration_revisions,
    load_active_search_configuration,
    load_prompt_release,
    load_published_active_search_configuration,
    load_review_queue,
    load_search_configuration_draft,
    load_search_configuration_publication,
    load_search_configuration_revision,
    psycopg,
    publish_search_configuration,
    pytest,
    record_review,
    replace_search_configuration_draft,
    save_search_configuration_draft,
    search_configuration_revision_id,
    store_prompt_release,
    store_search_configuration_revision,
    timedelta,
    token_hex,
    uuid4,
)

pytest_plugins = ("contracts.test_postgres_authority",)


def test_search_configuration_migration_preserves_every_legacy_row(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    run_id = uuid4()
    with _connection(authority_schema) as connection:
        _apply_migrations_through(connection, "0015_manifest_idempotency.sql")
        release = bootstrap_prompt_release(connection)
        _insert_prompt_run(connection, run_id, release.id, now)
        decision_id = _insert_review_decision(connection, run_id, release.id, now, 91, "qualified")
        assert enqueue_qualified_review_item(connection, decision_id, now.date())
        review_item = load_review_queue(connection).items[0]
        saved = record_review(
            connection,
            ReviewSubmission(
                review_item_id=review_item.id,
                evaluation_id=review_item.evaluation_id,
                snapshot_id=review_item.snapshot_id,
                decision="pursue",
                target_profile="applied-ai-product-engineer",
                actor="owner",
                created_at=now,
            ),
        )
        assert isinstance(saved, ReviewSaved)
        include_review_event(
            connection,
            review_event_id=saved.review_event_id,
            critical=True,
            reason="Preserve this evidence.",
            actor="owner",
            created_at=now,
            idempotency_key="configuration-migration-preservation",
        )
        _ = create_manifest(
            connection,
            policy=ManifestPolicy(),
            created_at=now,
            created_by="owner",
            idempotency_key="configuration-migration-preservation",
        )
        legacy_tables = _public_tables(connection)
        before = _table_contents(connection, legacy_tables)

        apply_migrations(connection)

        expected = dict(before)
        expected["pipeline_runs"] = [
            {
                **row,
                "configuration_revision_id": None,
                "relevance_release_id": None,
                "execution_authority_kind": "legacy",
                "acquisition_policy_revision_id": None,
                "qualification_target_id": None,
            }
            for row in cast(list[dict[str, object]], before["pipeline_runs"])
        ]
        expected["evaluation_decisions"] = [
            {
                **row,
                "relevance_release_id": None,
                "source_snapshot_id": None,
                "predecessor_decision_id": None,
                "reevaluation_request_key": None,
            }
            for row in cast(list[dict[str, object]], before["evaluation_decisions"])
        ]
        assert _table_contents(connection, legacy_tables) == expected


def test_configuration_revision_migration_backfills_only_orchestration_runs(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    orchestration_run_id = uuid4()
    processing_run_id = uuid4()
    with _connection(authority_schema) as connection:
        _apply_migrations_through(connection, "0019_configuration_publication_receipts.sql")
        release_id = _seed_initial_configuration_publication(connection, now)
        _insert_legacy_orchestration_run(connection, orchestration_run_id, release_id, now)
        _insert_prompt_run(connection, processing_run_id, release_id, now)

        apply_migrations(connection)

        rows = connection.execute(
            """
            SELECT id, configuration_revision_id
            FROM pipeline_runs
            ORDER BY id
            """
        ).fetchall()
        revisions = {UUID(str(row[0])): row[1] for row in rows}
        assert revisions == {
            orchestration_run_id: INITIAL_SEARCH_CONFIGURATION_REVISION_ID,
            processing_run_id: None,
        }


def test_configuration_revision_migration_preserves_unmapped_historical_release(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    historical_run_id = uuid4()
    with _connection(authority_schema) as connection:
        _apply_migrations_through(connection, "0019_configuration_publication_receipts.sql")
        _seed_initial_configuration_publication(connection, now)
        changed_configuration = DEFAULT_SEARCH_CONFIGURATION.model_copy(
            update={
                "personal_criteria": (
                    DEFAULT_SEARCH_CONFIGURATION.personal_criteria[0].model_copy(
                        update={"instructions": "A deliberately different criterion."}
                    ),
                    *DEFAULT_SEARCH_CONFIGURATION.personal_criteria[1:],
                )
            }
        )
        unexpected_release = store_prompt_release(
            connection,
            build_prompt_release(changed_configuration),
            created_at=now,
            created_by="test",
        )
        _insert_legacy_orchestration_run(connection, historical_run_id, unexpected_release.id, now)

        apply_migrations(connection)

        assert connection.execute(
            "SELECT configuration_revision_id FROM pipeline_runs WHERE id = %s",
            (historical_run_id,),
        ).fetchone() == (None,)
        with pytest.raises(psycopg.Error, match="cannot infer configuration revision"):
            _insert_legacy_orchestration_run(connection, uuid4(), unexpected_release.id, now)


def test_configuration_revision_migration_repairs_missing_initial_publication(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    initial_revision = build_search_configuration_revision(
        DEFAULT_SEARCH_CONFIGURATION,
        created_at=now,
        created_by="migration:0016_search_configuration_revisions.sql",
    )
    custom_revision = build_search_configuration_revision(
        DEFAULT_SEARCH_CONFIGURATION.model_copy(
            update={"search_keywords": ("custom active search",)}
        ),
        created_at=now,
        created_by="owner",
    )
    with _connection(authority_schema) as connection:
        _apply_migrations_through(connection, "0019_configuration_publication_receipts.sql")
        _ = store_search_configuration_revision(connection, initial_revision)
        _ = store_search_configuration_revision(connection, custom_revision)
        release = store_prompt_release(
            connection,
            build_prompt_release(custom_revision.configuration),
            created_at=now,
            created_by="owner",
        )
        connection.execute(
            """
            INSERT INTO search_configuration_publications (
              revision_id, prompt_release_id, published_at, published_by
            ) VALUES (%s, %s, %s, 'owner')
            """,
            (custom_revision.id, release.id, now),
        )
        connection.execute(
            """
            INSERT INTO search_configuration_drafts (
              singleton_id, base_revision_id, version, content, updated_at, updated_by
            ) VALUES (1, %s, 0, %s, %s, 'owner')
            """,
            (
                custom_revision.id,
                Jsonb(custom_revision.configuration.model_dump(mode="json")),
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO active_search_configuration (
              singleton_id, revision_id, generation, activated_at, activated_by
            ) VALUES (1, %s, 0, %s, 'owner')
            """,
            (custom_revision.id, now),
        )

        apply_migrations(connection)

        repaired = load_search_configuration_publication(connection, initial_revision.id)
        assert repaired.prompt_release_id == release.id


def test_pipeline_run_rejects_incomplete_release_target(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        publication = load_published_active_search_configuration(connection).publication
        changed_configuration = DEFAULT_SEARCH_CONFIGURATION.model_copy(
            update={
                "personal_criteria": (
                    DEFAULT_SEARCH_CONFIGURATION.personal_criteria[0].model_copy(
                        update={"instructions": "Another deliberately different criterion."}
                    ),
                    *DEFAULT_SEARCH_CONFIGURATION.personal_criteria[1:],
                )
            }
        )
        other_release = store_prompt_release(
            connection,
            build_prompt_release(changed_configuration),
            created_at=now,
            created_by="test",
        )

        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                """
                INSERT INTO pipeline_runs (
                  id, idempotency_key, kind, implementation_ref,
                  configuration_revision_id, prompt_release_id, parameters,
                  status, started_at
                ) VALUES (%s, %s, 'orchestration', 'test-ref', %s, %s,
                  '{}'::jsonb, 'running', %s)
                """,
                (
                    uuid4(),
                    "mismatched-configuration-publication",
                    publication.revision_id,
                    other_release.id,
                    now,
                ),
            )


def test_pipeline_run_configuration_shape_rejects_legacy_orchestration_writers(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    run_id = uuid4()
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        publication = load_search_configuration_publication(
            connection,
            SearchConfigurationRevisionId(INITIAL_SEARCH_CONFIGURATION_REVISION_ID),
        )

        with pytest.raises(psycopg.errors.CheckViolation):
            _insert_legacy_orchestration_run(connection, run_id, publication.prompt_release_id, now)
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                """
                INSERT INTO pipeline_runs (
                  id, idempotency_key, kind, implementation_ref,
                  configuration_revision_id, prompt_release_id, parameters,
                  status, started_at
                ) VALUES (%s, %s, 'processing', 'test-ref', %s, %s,
                  '{}'::jsonb, 'running', %s)
                """,
                (
                    uuid4(),
                    "processing-with-configuration",
                    SearchConfigurationRevisionId(INITIAL_SEARCH_CONFIGURATION_REVISION_ID),
                    publication.prompt_release_id,
                    now,
                ),
            )


def test_initial_search_configuration_is_seeded_as_draft_revision_and_active(
    authority_schema: str,
) -> None:
    with _connection(authority_schema) as connection:
        apply_migrations(connection)

        active = load_active_search_configuration(connection)
        draft = load_search_configuration_draft(connection)
        stored = load_search_configuration_revision(connection, active.revision.id)
        publication = load_search_configuration_publication(connection, active.revision.id)
        release = load_prompt_release(connection, publication.prompt_release_id)

        assert active.generation == 0
        assert active.revision.configuration == DEFAULT_SEARCH_CONFIGURATION
        assert stored == active.revision
        assert draft.base_revision_id == active.revision.id
        assert draft.version == 0
        assert draft.configuration == DEFAULT_SEARCH_CONFIGURATION
        assert publication.revision_id == active.revision.id
        assert publication.published_by == "migration:0017_search_configuration_publications.sql"
        assert release == build_prompt_release(DEFAULT_SEARCH_CONFIGURATION)


def test_configuration_revision_reads_use_stable_compound_keyset_pagination(
    authority_schema: str,
) -> None:
    base_time = datetime(2030, 1, 1, tzinfo=UTC)
    configurations = tuple(
        DEFAULT_SEARCH_CONFIGURATION.model_copy(update={"search_keywords": (f"revision-{index}",)})
        for index in range(4)
    )
    revisions = (
        build_search_configuration_revision(
            configurations[0], created_at=base_time + timedelta(hours=2), created_by="owner-0"
        ),
        build_search_configuration_revision(
            configurations[1], created_at=base_time + timedelta(hours=1), created_by="owner-1"
        ),
        build_search_configuration_revision(
            configurations[2], created_at=base_time + timedelta(hours=1), created_by="owner-2"
        ),
        build_search_configuration_revision(
            configurations[3], created_at=base_time, created_by="owner-3"
        ),
    )
    expected = sorted(revisions, key=lambda item: (item.created_at, item.id), reverse=True)

    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        for revision in revisions:
            _ = store_search_configuration_revision(connection, revision)
        _publish_configuration_revision(connection, expected[1], base_time + timedelta(hours=3))

        first = list_search_configuration_revisions(connection, limit=2)
        assert [item.revision_id for item in first.items] == [item.id for item in expected[:2]]
        assert first.next_cursor == ConfigurationRevisionCursor(
            created_at=expected[1].created_at,
            revision_id=expected[1].id,
        )
        publications = {item.revision_id: item.publication for item in first.items}
        assert publications[expected[1].id] is not None
        unpublished = next(item for item in first.items if item.revision_id != expected[1].id)
        assert unpublished.publication is None

        inserted = build_search_configuration_revision(
            DEFAULT_SEARCH_CONFIGURATION.model_copy(
                update={"search_keywords": ("inserted-between-pages",)}
            ),
            created_at=base_time + timedelta(hours=4),
            created_by="later-owner",
        )
        _ = store_search_configuration_revision(connection, inserted)

        second = list_search_configuration_revisions(
            connection,
            limit=2,
            cursor=first.next_cursor,
        )
        assert [item.revision_id for item in second.items] == [item.id for item in expected[2:]]
        assert not (
            {item.revision_id for item in first.items} & {item.revision_id for item in second.items}
        )
        assert inserted.id not in {item.revision_id for item in second.items}

        published_details = get_search_configuration_revision(connection, expected[1].id)
        assert published_details.revision == expected[1]
        assert published_details.publication is not None
        assert published_details.publication.revision_id == expected[1].id
        unpublished_details = get_search_configuration_revision(connection, revisions[3].id)
        assert unpublished_details.revision == revisions[3]
        assert unpublished_details.publication is None
        assert get_active_search_configuration(connection).active.revision.id == (
            SearchConfigurationRevisionId(INITIAL_SEARCH_CONFIGURATION_REVISION_ID)
        )
        assert get_search_configuration_draft(connection).base_revision_id == (
            SearchConfigurationRevisionId(INITIAL_SEARCH_CONFIGURATION_REVISION_ID)
        )


def test_get_search_configuration_revision_reports_a_missing_revision(
    authority_schema: str,
) -> None:
    missing_revision_id = SearchConfigurationRevisionId(token_hex(32))
    with _connection(authority_schema) as connection:
        apply_migrations(connection)

        with pytest.raises(ConfigurationRevisionNotFound):
            get_search_configuration_revision(connection, missing_revision_id)


def test_configuration_service_saves_a_draft_and_preserves_its_base(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    changed = DEFAULT_SEARCH_CONFIGURATION.model_copy(update={"search_keywords": ("changed",)})
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        initial = load_search_configuration_draft(connection)

        result = save_search_configuration_draft(
            connection,
            SaveDraftCommand(
                expected_version=initial.version,
                configuration=changed,
                actor="owner",
                timestamp=now,
            ),
        )

        assert isinstance(result, DraftSaved)
        assert result.draft.version == initial.version + 1
        assert result.draft.base_revision_id == initial.base_revision_id
        assert result.draft.configuration == changed
        assert result.draft.updated_by == "owner"
        assert result.draft.updated_at == now


def test_configuration_service_returns_current_draft_for_a_stale_save(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    first_change = DEFAULT_SEARCH_CONFIGURATION.model_copy(update={"search_keywords": ("first",)})
    stale_change = DEFAULT_SEARCH_CONFIGURATION.model_copy(update={"search_keywords": ("stale",)})
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        saved = save_search_configuration_draft(
            connection,
            SaveDraftCommand(
                expected_version=0,
                configuration=first_change,
                actor="first-owner",
                timestamp=now,
            ),
        )
        assert isinstance(saved, DraftSaved)

        result = save_search_configuration_draft(
            connection,
            SaveDraftCommand(
                expected_version=0,
                configuration=stale_change,
                actor="stale-owner",
                timestamp=now + timedelta(seconds=1),
            ),
        )

        assert isinstance(result, DraftChanged)
        assert result.current_draft == saved.draft


def test_configuration_service_loses_to_a_publication_style_draft_rebase(
    authority_schema: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    published_configuration = DEFAULT_SEARCH_CONFIGURATION.model_copy(
        update={"search_keywords": ("published",)}
    )
    requested_configuration = DEFAULT_SEARCH_CONFIGURATION.model_copy(
        update={"search_keywords": ("requested",)}
    )
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        published_revision = build_search_configuration_revision(
            published_configuration,
            created_at=now,
            created_by="publisher",
        )
        _ = store_search_configuration_revision(connection, published_revision)
        _publish_configuration_revision(connection, published_revision, now)
        load_count = 0

        def load_then_rebase(
            service_connection: psycopg.Connection[tuple[object, ...]],
        ) -> SearchConfigurationDraft:
            nonlocal load_count
            draft = load_search_configuration_draft(service_connection)
            load_count += 1
            if load_count == 1:
                service_connection.execute(
                    """
                    UPDATE search_configuration_drafts
                    SET base_revision_id = %s, content = %s, version = version + 1,
                        updated_at = %s, updated_by = 'publisher'
                    WHERE singleton_id = 1
                    """,
                    (
                        published_revision.id,
                        Jsonb(published_configuration.model_dump(mode="json")),
                        now,
                    ),
                )
            return draft

        monkeypatch.setattr(
            configuration_service_module,
            "load_search_configuration_draft",
            load_then_rebase,
        )

        result = save_search_configuration_draft(
            connection,
            SaveDraftCommand(
                expected_version=0,
                configuration=requested_configuration,
                actor="owner",
                timestamp=now + timedelta(seconds=1),
            ),
        )

        assert isinstance(result, DraftChanged)
        assert result.current_draft.version == 1
        assert result.current_draft.base_revision_id == published_revision.id
        assert result.current_draft.configuration == published_configuration


def test_publication_migration_backfills_custom_active_and_draft_revisions(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    draft_revision = build_search_configuration_revision(
        DEFAULT_SEARCH_CONFIGURATION.model_copy(update={"search_keywords": ("draft-only",)}),
        created_at=now,
        created_by="owner",
    )
    active_revision = build_search_configuration_revision(
        DEFAULT_SEARCH_CONFIGURATION.model_copy(update={"search_keywords": ("active-only",)}),
        created_at=now,
        created_by="owner",
    )
    with _connection(authority_schema) as connection:
        _apply_migrations_through(connection, "0016_search_configuration_revisions.sql")
        initial_revision = build_search_configuration_revision(
            DEFAULT_SEARCH_CONFIGURATION,
            created_at=now,
            created_by="migration:0016_search_configuration_revisions.sql",
        )
        _ = store_search_configuration_revision(connection, initial_revision)
        _ = store_search_configuration_revision(connection, draft_revision)
        _ = store_search_configuration_revision(connection, active_revision)
        connection.execute(
            """
            INSERT INTO search_configuration_drafts (
              singleton_id, base_revision_id, version, content, updated_at, updated_by
            ) VALUES (1, %s, 0, %s, %s, 'owner')
            """,
            (draft_revision.id, Jsonb(draft_revision.configuration.model_dump(mode="json")), now),
        )
        connection.execute(
            """
            INSERT INTO active_search_configuration (
              singleton_id, revision_id, generation, activated_at, activated_by
            ) VALUES (1, %s, 0, %s, 'owner')
            """,
            (active_revision.id, now),
        )

        apply_migrations(connection)

        draft_publication = load_search_configuration_publication(connection, draft_revision.id)
        active_publication = load_search_configuration_publication(connection, active_revision.id)
        initial_publication = load_search_configuration_publication(
            connection,
            SearchConfigurationRevisionId(INITIAL_SEARCH_CONFIGURATION_REVISION_ID),
        )
        assert draft_publication.prompt_release_id == active_publication.prompt_release_id
        assert initial_publication.prompt_release_id == active_publication.prompt_release_id
        assert connection.execute(
            "SELECT count(*) FROM search_configuration_publications"
        ).fetchone() == (3,)
        assert connection.execute("SELECT count(*) FROM prompt_releases").fetchone() == (1,)
        assert load_search_configuration_draft(connection).base_revision_id == draft_revision.id
        assert load_active_search_configuration(connection).revision.id == active_revision.id


def test_configuration_publication_is_atomic_rebases_once_and_replays(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    changed = DEFAULT_SEARCH_CONFIGURATION.model_copy(update={"search_keywords": ("principal",)})
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        initial_active = load_active_search_configuration(connection)
        saved = replace_search_configuration_draft(
            connection,
            expected_version=0,
            base_revision_id=initial_active.revision.id,
            configuration=changed,
            updated_at=now,
            updated_by="owner",
        )
        assert saved is not None
        command = _publication_command(saved, "publish:first", now)

        first = publish_search_configuration(connection, command)
        retry = publish_search_configuration(
            connection,
            command.model_copy(update={"timestamp": now + timedelta(hours=1)}),
        )

        assert isinstance(first, ConfigurationPublished)
        assert isinstance(retry, ConfigurationPublished)
        assert not first.replayed
        assert retry.replayed
        assert retry.publication == first.publication
        assert retry.rebased_draft == first.rebased_draft
        assert first.rebased_draft.version == 2
        assert load_search_configuration_draft(connection) == first.rebased_draft
        assert load_active_search_configuration(connection) == initial_active
        assert (
            first.publication.prompt_release_id
            == load_search_configuration_publication(
                connection, initial_active.revision.id
            ).prompt_release_id
        )
        assert load_prompt_release(
            connection, first.publication.prompt_release_id
        ) == build_prompt_release(changed)
        assert connection.execute(
            "SELECT count(*) FROM search_configuration_publication_receipts"
        ).fetchone() == (1,)

        conflict = publish_search_configuration(
            connection,
            command.model_copy(update={"actor": "other"}),
        )
        assert isinstance(conflict, PublicationIdempotencyKeyConflict)
        assert load_search_configuration_draft(connection).version == 2

        repeated_content = publish_search_configuration(
            connection,
            PublishConfigurationCommand(
                idempotency_key="publish:second",
                expected_draft_version=2,
                expected_configuration_revision_id=first.publication.revision_id,
                actor="owner",
                timestamp=now + timedelta(hours=2),
            ),
        )
        assert isinstance(repeated_content, ConfigurationPublished)
        assert repeated_content.publication == first.publication
        assert repeated_content.rebased_draft.version == 3
        assert connection.execute("SELECT count(*) FROM prompt_releases").fetchone() == (1,)


def test_configuration_publication_durably_replays_stale_revision_and_version(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        draft = load_search_configuration_draft(connection)
        wrong_revision = SearchConfigurationRevisionId("f" * 64)
        command = PublishConfigurationCommand(
            idempotency_key="publish:stale",
            expected_draft_version=draft.version,
            expected_configuration_revision_id=wrong_revision,
            actor="owner",
            timestamp=now,
        )

        first = publish_search_configuration(connection, command)
        assert isinstance(first, PublishDraftChanged)
        assert first.expected_configuration_revision_id == wrong_revision
        assert first.observed_configuration_revision_id == search_configuration_revision_id(
            draft.configuration
        )
        assert not first.replayed

        changed = replace_search_configuration_draft(
            connection,
            expected_version=draft.version,
            base_revision_id=draft.base_revision_id,
            configuration=draft.configuration.model_copy(update={"search_keywords": ("later",)}),
            updated_at=now + timedelta(minutes=1),
            updated_by="owner",
        )
        assert changed is not None
        replay = publish_search_configuration(connection, command)
        assert isinstance(replay, PublishDraftChanged)
        assert replay.replayed
        assert replay.model_copy(update={"replayed": False}) == first
