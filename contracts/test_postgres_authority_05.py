from __future__ import annotations

from contracts.test_postgres_authority import (
    ActivateReleaseTargetCommand,
    ActiveReleaseTargetChanged,
    Barrier,
    BudgetSaved,
    CodeArtifactIdentity,
    CompletedEvaluationExecution,
    DEFAULT_SEARCH_CONFIGURATION,
    Decimal,
    EXPECTED_MIGRATIONS,
    EvaluateManifestCommand,
    EvaluationManifestCase,
    EvaluationResult,
    ExchangeRateSnapshot,
    ExecutionAdmitted,
    ExecutionBlocked,
    FailedEvaluationExecution,
    JevFaithfulExecutionPolicy,
    OnboardingStage,
    Path,
    ProviderCapability,
    ProviderCredentialChanged,
    ProviderCredentialStored,
    ProviderKind,
    ProviderValidation,
    Qualified,
    Rejected,
    ReleaseTarget,
    ReleaseTargetActivated,
    ReleaseTargetLifecycleError,
    ReviewSaved,
    ReviewSubmission,
    SecretStr,
    ThreadPoolExecutor,
    UTC,
    _apply_migrations_through,
    _connection,
    _insert_prompt_run,
    _insert_review_decision,
    _seed_evaluation_execution_context,
    activate_release_target,
    admit_scheduled_execution,
    apply_migrations,
    base64,
    bootstrap_prompt_release,
    build_gemini_policy,
    build_jev_faithful_policy,
    build_prompt_release,
    build_relevance_release,
    build_search_configuration_revision,
    cast,
    credential_cipher,
    datetime,
    enqueue_qualified_review_item,
    get_active_release_target,
    load_prompt_release,
    load_published_active_search_configuration,
    load_relevance_release,
    load_review_queue,
    manifest_execution_module,
    postgres_budget_setup_service,
    postgres_owner_access_service,
    postgres_provider_setup_service,
    prepare_orchestration_run,
    preview_run_comparison,
    psycopg,
    pytest,
    record_prompt_promotion_decision,
    record_review,
    relevance_releases_module,
    reserve_discovery,
    reserve_job_capacity,
    resolve_execution_provider_credentials,
    run_manifest,
    run_stored_manifest,
    store_prompt_release,
    store_relevance_release,
    store_search_configuration_revision,
    timedelta,
    uuid4,
    validate_release_target,
)

pytest_plugins = ("contracts.test_postgres_authority",)


def test_provider_credentials_are_encrypted_versioned_and_gate_onboarding(
    authority_schema: str,
) -> None:
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
    owner = postgres_owner_access_service(lambda: _connection(authority_schema))
    _ = owner.bootstrap("secure owner password")
    cipher = credential_cipher(SecretStr(base64.urlsafe_b64encode(b"k" * 32).decode()))
    service = postgres_provider_setup_service(
        lambda: _connection(authority_schema),
        cipher,
        {
            provider: lambda _secret, provider=provider: ProviderValidation(
                capabilities={
                    ProviderKind.JINA: (
                        ProviderCapability.SEARCH,
                        ProviderCapability.SCRAPE,
                    ),
                    ProviderKind.OPENROUTER: (
                        ProviderCapability.STRUCTURED_GENERATION,
                        ProviderCapability.USAGE_COST,
                    ),
                    ProviderKind.TYPESAFE: (
                        ProviderCapability.RELEVANCE_EVALUATION,
                        ProviderCapability.USAGE_COST,
                    ),
                }[provider]
            )
            for provider in ProviderKind
        },
    )

    def replace_jina(_index: int) -> object:
        return service.replace(
            ProviderKind.JINA,
            SecretStr("jina-secret-value"),
            0,
            "owner",
            datetime(2026, 9, 22, tzinfo=UTC),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(replace_jina, range(2)))

    assert sum(isinstance(result, ProviderCredentialStored) for result in results) == 1
    assert sum(isinstance(result, ProviderCredentialChanged) for result in results) == 1
    for provider in (ProviderKind.OPENROUTER, ProviderKind.TYPESAFE):
        result = service.replace(
            provider,
            SecretStr(f"{provider.value}-secret-value"),
            0,
            "owner",
            datetime(2026, 9, 22, tzinfo=UTC),
        )
        assert isinstance(result, ProviderCredentialStored)

    advanced = service.advance()

    assert advanced.state.stage is OnboardingStage.PREFERENCES
    assert service.inspect().ready
    assert service.resolve(ProviderKind.JINA).get_secret_value() == "jina-secret-value"
    with _connection(authority_schema) as connection:
        runtime_credentials = resolve_execution_provider_credentials(
            connection,
            cipher=cipher,
            jina_fallback=None,
            openrouter_fallback=None,
            typesafe_fallback=None,
        )
        assert runtime_credentials.jina.get_secret_value() == "jina-secret-value"
        rows = connection.execute(
            "SELECT nonce, ciphertext FROM provider_credentials ORDER BY provider"
        ).fetchall()
        assert len(rows) == 3
        assert all(b"secret-value" not in cast(bytes, row[1]) for row in rows)
        with pytest.raises(psycopg.errors.CheckViolation, match="cannot be deleted"):
            connection.execute("DELETE FROM provider_credentials WHERE provider = 'jina'")

    with _connection(authority_schema) as connection:
        connection.execute(
            "UPDATE owner_onboarding SET stage = 'budget', updated_at = CURRENT_TIMESTAMP WHERE singleton_id = 1"
        )
    assert owner.load_state().stage is OnboardingStage.BUDGET

    budget = postgres_budget_setup_service(lambda: _connection(authority_schema))
    budget_result = budget.save(
        0,
        Decimal("4"),
        Decimal("2"),
        10,
        "owner",
        datetime(2026, 9, 22, tzinfo=UTC),
    )
    assert isinstance(budget_result, BudgetSaved)
    assert owner.load_state().stage is OnboardingStage.TEST_SEARCH
    with _connection(authority_schema) as connection:
        _ = connection.execute(
            """
            UPDATE owner_onboarding
            SET stage = 'complete', updated_at = CURRENT_TIMESTAMP
            WHERE singleton_id = 1
            """
        )

        _ = connection.execute(
            """
            INSERT INTO execution_budget_reservations (
              idempotency_key, policy_version, period_start, reserved_usd,
              status, max_jobs, authority_kind, created_at
            ) VALUES (
              'legacy-in-flight', 1, DATE '2026-09-01', 0.5,
              'reserved', 3, 'legacy', %s
            )
            """,
            (datetime(2026, 9, 22, tzinfo=UTC),),
        )
        legacy = admit_scheduled_execution(
            connection,
            idempotency_key="legacy-in-flight",
            requested_at=datetime(2026, 9, 22, tzinfo=UTC),
        )
        replayed_legacy = admit_scheduled_execution(
            connection,
            idempotency_key="legacy-in-flight",
            requested_at=datetime(2026, 9, 22, tzinfo=UTC),
        )
        legacy_row = connection.execute(
            """
            SELECT authority_kind, configuration_revision_id,
                   prompt_release_id, relevance_release_id
            FROM execution_budget_reservations
            WHERE idempotency_key = 'legacy-in-flight'
            """
        ).fetchone()

    assert isinstance(legacy, ExecutionAdmitted)
    assert replayed_legacy == legacy
    assert legacy.max_jobs == 3
    assert legacy.estimate.jobs_per_run == 3
    assert legacy_row == (
        "pinned",
        legacy.configuration_revision_id,
        legacy.target.prompt_release_id,
        legacy.target.relevance_release_id,
    )
    with _connection(authority_schema) as connection:
        changed_configuration = store_search_configuration_revision(
            connection,
            build_search_configuration_revision(
                DEFAULT_SEARCH_CONFIGURATION.model_copy(
                    update={"search_keywords": ("different authority",)}
                ),
                created_at=datetime(2026, 9, 22, tzinfo=UTC),
                created_by="test",
            ),
        )
        with pytest.raises(psycopg.errors.CheckViolation, match="reserved execution authority"):
            prepare_orchestration_run(
                connection,
                idempotency_key="legacy-in-flight",
                implementation_ref="test",
                configuration_revision_id=changed_configuration.id,
                target=legacy.target,
                started_at=datetime(2026, 9, 22, tzinfo=UTC),
                fetch_rates=lambda: ExchangeRateSnapshot(
                    rates={"EUR": Decimal("1.11")},
                    source="fallback",
                    observed_at=datetime(2026, 9, 22, tzinfo=UTC),
                ),
            )

    barrier = Barrier(2)

    def admit(index: int) -> object:
        _ = barrier.wait()
        with _connection(authority_schema) as connection:
            return admit_scheduled_execution(
                connection,
                idempotency_key=f"scheduled-run-{index}",
                requested_at=datetime(2026, 9, 22, tzinfo=UTC),
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        admissions = tuple(executor.map(admit, range(2)))

    assert sum(isinstance(result, ExecutionAdmitted) for result in admissions) == 1
    assert sum(isinstance(result, ExecutionBlocked) for result in admissions) == 1
    admitted_index, admitted = next(
        (index, result)
        for index, result in enumerate(admissions)
        if isinstance(result, ExecutionAdmitted)
    )
    with _connection(authority_schema) as connection:
        reservation_row = connection.execute(
            """
            SELECT idempotency_key, authority_kind, configuration_revision_id, prompt_release_id,
                   relevance_release_id, release_generation, search_queries,
                   logical_model_calls_per_job, maximum_provider_attempts
            FROM execution_budget_reservations
            WHERE idempotency_key = %s
            """,
            (f"scheduled-run-{admitted_index}",),
        ).fetchone()
        assert reservation_row is not None
        reservation_key = cast(str, reservation_row[0])
        assert reservation_row[1:] == (
            "pinned",
            admitted.configuration_revision_id,
            admitted.target.prompt_release_id,
            admitted.target.relevance_release_id,
            admitted.release_generation,
            admitted.estimate.search_queries,
            admitted.estimate.logical_model_calls_per_job,
            admitted.estimate.maximum_provider_attempts,
        )
        with pytest.raises(psycopg.errors.CheckViolation, match="authority is immutable"):
            connection.execute(
                """
                UPDATE execution_budget_reservations
                SET maximum_provider_attempts = maximum_provider_attempts + 1
                WHERE idempotency_key = %s
                """,
                (reservation_key,),
            )
        assert reserve_discovery(connection, reservation_key)
        assert not reserve_discovery(connection, reservation_key)
        assert reserve_job_capacity(connection, reservation_key) == 10
        assert reserve_job_capacity(connection, reservation_key) == 0
        _ = connection.execute(
            """
            UPDATE execution_budget_policy
            SET max_search_queries_per_run = 1, version = version + 1
            WHERE singleton_id = 1
            """
        )
        assert admit_scheduled_execution(
            connection,
            idempotency_key="configuration-over-policy",
            requested_at=datetime(2026, 9, 22, tzinfo=UTC),
        ) == ExecutionBlocked(reason="configuration_exceeds_policy")


def test_approved_release_target_activation_is_exact_cas_and_replay_safe(
    authority_schema: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    with _connection(authority_schema) as connection:
        manifest_id, _, rates = _seed_evaluation_execution_context(connection, now)
        active = get_active_release_target(connection)
        prompt_release = load_prompt_release(connection, active.target.prompt_release_id)
        candidate_relevance = store_relevance_release(
            connection,
            build_relevance_release(build_gemini_policy(prompt_release)),
            created_at=now,
            created_by="contract",
        )
        candidate_target = ReleaseTarget(
            prompt_release_id=prompt_release.id,
            relevance_release_id=candidate_relevance.id,
        )

        def expected_result(
            case: EvaluationManifestCase, _target: ReleaseTarget, _trial: int
        ) -> EvaluationResult:
            if case.expected_outcome == "qualified":
                return Qualified(reason="Expected positive.", profile_name="profile")
            return Rejected(reason="Expected negative.")

        baseline_execution = run_manifest(
            connection,
            command=EvaluateManifestCommand(
                idempotency_key="release-target:baseline",
                manifest_id=manifest_id,
                target=active.target,
                implementation_ref="baseline",
            ),
            create_exchange_rates=lambda: rates,
            create_evaluator=lambda _rates, _record: expected_result,
            now=lambda: now,
        )
        candidate_execution = run_manifest(
            connection,
            command=EvaluateManifestCommand(
                idempotency_key="release-target:candidate",
                manifest_id=manifest_id,
                target=candidate_target,
                implementation_ref="candidate",
            ),
            create_exchange_rates=lambda: rates,
            create_evaluator=lambda _rates, _record: expected_result,
            now=lambda: now,
        )
        assert isinstance(baseline_execution, CompletedEvaluationExecution)
        assert isinstance(candidate_execution, CompletedEvaluationExecution)
        comparison = preview_run_comparison(
            connection, baseline_execution.run.id, candidate_execution.run.id
        )
        decision = record_prompt_promotion_decision(
            connection,
            baseline_run_id=baseline_execution.run.id,
            candidate_run_id=candidate_execution.run.id,
            expected_comparison_id=comparison.id,
            decision="approved",
            reason="Exact target passed the frozen manifest.",
            actor="owner",
            created_at=now,
            idempotency_key="release-target:decision",
        )
        command = ActivateReleaseTargetCommand(
            idempotency_key="release-target:activate",
            promotion_decision_id=decision.id,
            expected_active_target=active.target,
            expected_generation=active.generation,
            actor="owner",
            timestamp=now + timedelta(minutes=1),
        )
        budget = postgres_budget_setup_service(lambda: _connection(authority_schema))
        estimate_before_activation = budget.inspect(10).estimate
        source_artifact_identity = relevance_releases_module.source_artifact_identity

        def drifted_source_artifact_identity(
            entrypoint: str, source_path: Path
        ) -> CodeArtifactIdentity:
            return source_artifact_identity(entrypoint, source_path).model_copy(
                update={"content_digest": "0" * 64}
            )

        with monkeypatch.context() as patch:
            patch.setattr(
                relevance_releases_module,
                "source_artifact_identity",
                drifted_source_artifact_identity,
            )
            with pytest.raises(
                ReleaseTargetLifecycleError,
                match="implementation artifacts do not match",
            ):
                activate_release_target(
                    connection,
                    command.model_copy(update={"idempotency_key": "release-target:drifted"}),
                )
        assert get_active_release_target(connection) == active
        activated = activate_release_target(connection, command)
        estimate_after_activation = budget.inspect(10).estimate
        replayed = activate_release_target(connection, command)
        with monkeypatch.context() as patch:
            patch.setattr(
                relevance_releases_module,
                "source_artifact_identity",
                drifted_source_artifact_identity,
            )
            stale = activate_release_target(
                connection,
                command.model_copy(
                    update={
                        "idempotency_key": "release-target:stale",
                        "timestamp": now + timedelta(minutes=2),
                    }
                ),
            )
        orchestration_run = prepare_orchestration_run(
            connection,
            idempotency_key="release-target:orchestration",
            implementation_ref="candidate",
            configuration_revision_id=load_published_active_search_configuration(
                connection
            ).publication.revision_id,
            target=candidate_target,
            started_at=now + timedelta(minutes=3),
            fetch_rates=lambda: rates,
        )

        assert isinstance(activated, ReleaseTargetActivated)
        assert activated.replayed is False
        assert activated.active.target == candidate_target
        assert activated.active.generation == active.generation + 1
        assert estimate_before_activation.maximum_provider_attempts == 400
        assert estimate_after_activation.maximum_provider_attempts == 640
        assert isinstance(replayed, ReleaseTargetActivated)
        assert replayed.replayed is True
        assert replayed.active == activated.active
        assert isinstance(stale, ActiveReleaseTargetChanged)
        assert stale.active == activated.active
        assert orchestration_run.target == candidate_target
        assert connection.execute(
            "SELECT count(*) FROM release_target_activation_receipts"
        ).fetchone() == (2,)
        with pytest.raises(psycopg.errors.CheckViolation, match="immutable"):
            connection.execute("UPDATE release_target_activation_receipts SET actor = 'tampered'")
        with pytest.raises(psycopg.errors.CheckViolation, match="approved activation receipt"):
            connection.execute(
                "UPDATE active_release_target SET generation = generation + 1 WHERE singleton_id = 1"
            )
        with pytest.raises(psycopg.errors.CheckViolation, match="immutable"):
            connection.execute("DELETE FROM active_release_target")


def test_run_stored_manifest_fails_sanitized_without_provider_keys_and_replays(
    authority_schema: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    rates = ExchangeRateSnapshot(rates={"EUR": Decimal("1.11")}, source="fallback", observed_at=now)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    def fixed_rates(*, observed_at: datetime) -> ExchangeRateSnapshot:
        assert observed_at.tzinfo is not None
        return rates

    monkeypatch.setattr(manifest_execution_module, "fetch_exchange_rates", fixed_rates)
    with _connection(authority_schema) as connection:
        manifest_id, target, _seeded_rates = _seed_evaluation_execution_context(connection, now)
        command = EvaluateManifestCommand(
            idempotency_key="evaluate-manifest:missing-key",
            manifest_id=manifest_id,
            target=target,
            implementation_ref="stored-manifest",
        )

        failed = run_stored_manifest(connection, command)
        replayed = run_stored_manifest(connection, command)

    assert isinstance(failed, FailedEvaluationExecution)
    assert failed.failure.code == "unexpected_exception"
    assert failed.failure.error_type == "ValueError"
    assert "TYPESAFE_API_KEY" not in failed.failure.message
    assert replayed == failed


def test_release_target_lifecycle_rejects_unapproved_and_mismatched_commands(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    with _connection(authority_schema) as connection:
        manifest_id, _, rates = _seed_evaluation_execution_context(connection, now)
        active = get_active_release_target(connection)
        prompt_release = load_prompt_release(connection, active.target.prompt_release_id)
        candidate_relevance = store_relevance_release(
            connection,
            build_relevance_release(build_jev_faithful_policy(prompt_release)),
            created_at=now,
            created_by="contract",
        )
        candidate_target = ReleaseTarget(
            prompt_release_id=prompt_release.id,
            relevance_release_id=candidate_relevance.id,
        )

        def expected_result(
            case: EvaluationManifestCase, _target: ReleaseTarget, _trial: int
        ) -> EvaluationResult:
            if case.expected_outcome == "qualified":
                return Qualified(reason="Expected positive.", profile_name="profile")
            return Rejected(reason="Expected negative.")

        baseline_execution = run_manifest(
            connection,
            command=EvaluateManifestCommand(
                idempotency_key="lifecycle-guard:baseline",
                manifest_id=manifest_id,
                target=active.target,
                implementation_ref="baseline",
            ),
            create_exchange_rates=lambda: rates,
            create_evaluator=lambda _rates, _record: expected_result,
            now=lambda: now,
        )
        candidate_execution = run_manifest(
            connection,
            command=EvaluateManifestCommand(
                idempotency_key="lifecycle-guard:candidate",
                manifest_id=manifest_id,
                target=candidate_target,
                implementation_ref="candidate",
            ),
            create_exchange_rates=lambda: rates,
            create_evaluator=lambda _rates, _record: expected_result,
            now=lambda: now,
        )
        assert isinstance(baseline_execution, CompletedEvaluationExecution)
        assert isinstance(candidate_execution, CompletedEvaluationExecution)
        comparison = preview_run_comparison(
            connection, baseline_execution.run.id, candidate_execution.run.id
        )
        approved = record_prompt_promotion_decision(
            connection,
            baseline_run_id=baseline_execution.run.id,
            candidate_run_id=candidate_execution.run.id,
            expected_comparison_id=comparison.id,
            decision="approved",
            reason="Exact target passed the frozen manifest.",
            actor="owner",
            created_at=now,
            idempotency_key="lifecycle-guard:decision-approved",
        )
        activated = activate_release_target(
            connection,
            ActivateReleaseTargetCommand(
                idempotency_key="lifecycle-guard:activate",
                promotion_decision_id=approved.id,
                expected_active_target=active.target,
                expected_generation=active.generation,
                actor="owner",
                timestamp=now + timedelta(minutes=1),
            ),
        )
        with pytest.raises(
            ReleaseTargetLifecycleError,
            match="different release-target activation",
        ):
            activate_release_target(
                connection,
                ActivateReleaseTargetCommand(
                    idempotency_key="lifecycle-guard:activate",
                    promotion_decision_id=approved.id,
                    expected_active_target=active.target,
                    expected_generation=active.generation + 5,
                    actor="owner",
                    timestamp=now + timedelta(minutes=2),
                ),
            )
        with pytest.raises(ValueError, match="already has a promotion decision"):
            record_prompt_promotion_decision(
                connection,
                baseline_run_id=baseline_execution.run.id,
                candidate_run_id=candidate_execution.run.id,
                expected_comparison_id=comparison.id,
                decision="rejected",
                reason="Second opinion.",
                actor="owner",
                created_at=now + timedelta(minutes=3),
                idempotency_key="lifecycle-guard:decision-duplicate",
            )

        second_candidate_execution = run_manifest(
            connection,
            command=EvaluateManifestCommand(
                idempotency_key="lifecycle-guard:candidate-2",
                manifest_id=manifest_id,
                target=candidate_target,
                implementation_ref="candidate",
            ),
            create_exchange_rates=lambda: rates,
            create_evaluator=lambda _rates, _record: expected_result,
            now=lambda: now,
        )
        assert isinstance(second_candidate_execution, CompletedEvaluationExecution)
        second_comparison = preview_run_comparison(
            connection, baseline_execution.run.id, second_candidate_execution.run.id
        )
        rejected = record_prompt_promotion_decision(
            connection,
            baseline_run_id=baseline_execution.run.id,
            candidate_run_id=second_candidate_execution.run.id,
            expected_comparison_id=second_comparison.id,
            decision="rejected",
            reason="Not taking this target.",
            actor="owner",
            created_at=now,
            idempotency_key="lifecycle-guard:decision-rejected",
        )
        with pytest.raises(
            ReleaseTargetLifecycleError,
            match="Approved promotion decision does not exist",
        ):
            activate_release_target(
                connection,
                ActivateReleaseTargetCommand(
                    idempotency_key="lifecycle-guard:activate-rejected",
                    promotion_decision_id=rejected.id,
                    expected_active_target=active.target,
                    expected_generation=active.generation,
                    actor="owner",
                    timestamp=now + timedelta(minutes=4),
                ),
            )

        assert isinstance(activated, ReleaseTargetActivated)
        assert get_active_release_target(connection) == activated.active


def test_release_target_migration_bootstraps_configurable_prompt_criteria(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 21, 12, tzinfo=UTC)
    criterion = DEFAULT_SEARCH_CONFIGURATION.personal_criteria[0].model_copy(
        update={"key": "custom-criterion"}
    )
    configuration = DEFAULT_SEARCH_CONFIGURATION.model_copy(
        update={
            "personal_criteria": (
                criterion,
                *DEFAULT_SEARCH_CONFIGURATION.personal_criteria[1:],
            )
        }
    )
    with _connection(authority_schema) as connection:
        _apply_migrations_through(connection, "0025_release_target_promotion_decisions.sql")
        revision = store_search_configuration_revision(
            connection,
            build_search_configuration_revision(
                configuration, created_at=now, created_by="contract"
            ),
        )
        prompt_release = store_prompt_release(
            connection,
            build_prompt_release(configuration),
            created_at=now,
            created_by="contract",
        )
        _ = connection.execute(
            """
            INSERT INTO search_configuration_publications (
              revision_id, prompt_release_id, published_at, published_by
            ) VALUES (%s, %s, %s, %s)
            """,
            (revision.id, prompt_release.id, now, "contract"),
        )
        _ = connection.execute(
            """
            INSERT INTO active_search_configuration (
              singleton_id, revision_id, generation, activated_at, activated_by
            ) VALUES (1, %s, 0, %s, %s)
            """,
            (revision.id, now, "contract"),
        )

        apply_migrations(connection)

        active = get_active_release_target(connection)
        relevance_release = load_relevance_release(connection, active.target.relevance_release_id)
        validate_release_target(active.target, prompt_release, relevance_release)
        assert isinstance(relevance_release.policy, JevFaithfulExecutionPolicy)


def test_unbounded_review_note_migration_preserves_feedback_and_accepts_long_notes(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 21, 12, tzinfo=UTC)
    run_id = uuid4()
    note = " " + "x" * 1999 + " "
    with _connection(authority_schema) as connection:
        _apply_migrations_through(connection, "0022_relevance_releases.sql")
        release = bootstrap_prompt_release(connection)
        _insert_prompt_run(connection, run_id, release.id, now)
        evaluation_id = _insert_review_decision(connection, run_id, release.id, now, 1, "qualified")
        assert enqueue_qualified_review_item(connection, evaluation_id, now.date())
        item = load_review_queue(connection).items[0]
        ordinary = ReviewSubmission(
            review_item_id=item.id,
            evaluation_id=item.evaluation_id,
            snapshot_id=item.snapshot_id,
            decision="unsure",
            note="Need more detail.",
            actor="owner",
            created_at=now,
        )
        saved_ordinary = record_review(connection, ordinary)
        assert isinstance(saved_ordinary, ReviewSaved)

        long_note = ordinary.model_copy(
            update={"note": note, "created_at": now + timedelta(seconds=1)}
        )
        with pytest.raises(psycopg.errors.CheckViolation, match="review_events_note_check"):
            record_review(connection, long_note)

        assert apply_migrations(connection)[-10:] == EXPECTED_MIGRATIONS[-10:]
        saved_long = record_review(connection, long_note)
        assert isinstance(saved_long, ReviewSaved)
        assert len(note) == 2001
        assert connection.execute(
            "SELECT id, note FROM review_events ORDER BY created_at"
        ).fetchall() == [
            (saved_ordinary.review_event_id, ordinary.note),
            (saved_long.review_event_id, note),
        ]


def test_concurrent_migration_startup_serializes_schema_writes(
    authority_schema: str,
) -> None:
    barrier = Barrier(2)

    def migrate() -> tuple[str, ...]:
        with _connection(authority_schema) as connection:
            _ = barrier.wait()
            return apply_migrations(connection)

    def migrate_for_index(_index: int) -> tuple[str, ...]:
        return migrate()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(migrate_for_index, range(2)))

    assert results[0] == results[1] == EXPECTED_MIGRATIONS
