from __future__ import annotations

from contracts.test_postgres_authority import (
    AtsAvailable,
    AtsNotApplicable,
    Barrier,
    Decimal,
    ExchangeRateSnapshot,
    ExecutionAdmitted,
    FixtureCase,
    HttpResponse,
    INITIAL_SEARCH_CONFIGURATION_REVISION_ID,
    JEV_MODEL,
    JevHttpResponse,
    Mapping,
    OnboardingStage,
    OwnerBootstrapped,
    Path,
    PhaseFixtureSet,
    PostgresContractSettings,
    ProviderExperimentSettings,
    QualificationEvidence,
    RelevanceExperimentInput,
    ThreadPoolExecutor,
    UTC,
    _apply_migrations_through,
    _connection,
    _seed_evaluation_execution_context,
    _seed_initial_configuration_publication,
    _store_default_qualification_target,
    admit_scheduled_execution,
    apply_migrations,
    bind_qualification_prompt_release,
    build_jev_atomic_policy,
    build_relevance_release,
    cast,
    create_review_server,
    datetime,
    execute_composition_fixture_set,
    execute_deduplication_fixture_set,
    hashlib,
    import_legacy_owner_password,
    json,
    postgres_owner_access_service,
    psycopg,
    pytest,
    qualification_evidence_id,
    qualification_target_id,
    record_relevance_comparison,
    store_fixture_set,
    store_qualification_evidence,
    store_relevance_experiment_input,
    store_relevance_release,
    write_implementation_artifact,
)

pytest_plugins = ("contracts.test_postgres_authority",)


def test_deduplication_fixtures_cover_ledger_paths_and_model_attempt(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 25, tzinfo=UTC)
    artifact_path = Path(__file__).resolve().parents[1] / "implementation-artifact.json"
    settings = ProviderExperimentSettings(provider="openrouter", temperature=0, retry_limit=0)
    fixture = PhaseFixtureSet(
        phase="deduplication",
        cases=(
            FixtureCase(
                input={
                    "new_title": "Backend Engineer",
                    "existing_titles": [],
                    "provider_settings": settings.model_dump(mode="json"),
                },
                expected={"isDuplicate": False, "matchedTitle": None},
                input_path="direct",
            ),
            FixtureCase(
                input={
                    "new_title": "Backend Engineer",
                    "existing_titles": ["backend engineer"],
                    "provider_settings": settings.model_dump(mode="json"),
                },
                expected={"isDuplicate": True, "matchedTitle": "backend engineer"},
                input_path="direct",
            ),
            FixtureCase(
                input={
                    "new_title": "Backend Engineer",
                    "existing_titles": ["Software Engineer"],
                    "provider_settings": settings.model_dump(mode="json"),
                },
                expected={"isDuplicate": False, "matchedTitle": None},
                input_path="direct",
            ),
        ),
    )
    sent = 0

    def send(
        _url: str,
        _headers: Mapping[str, str],
        _body: dict[str, object],
        _timeout: float,
    ) -> HttpResponse:
        nonlocal sent
        sent += 1
        return HttpResponse(
            status_code=200,
            body=json.dumps(
                {
                    "id": f"dedup-generation-{sent}",
                    "model": "google/gemini-2.5-flash-lite",
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "type": "function",
                                        "function": {
                                            "name": "check_duplicate",
                                            "arguments": json.dumps(
                                                {"isDuplicate": False, "matchedTitle": None}
                                            ),
                                        },
                                    }
                                ]
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 4, "cost": 0.00012},
                }
            ),
        )

    with _connection(authority_schema) as connection:
        _ = apply_migrations(connection)
        artifact, _, target = _store_default_qualification_target(connection, now)
        target_id = qualification_target_id(target)
        fixture_id = store_fixture_set(connection, fixture, created_at=now, created_by="owner")
        incomplete_id = store_fixture_set(
            connection,
            fixture.model_copy(update={"cases": fixture.cases[:2]}),
            created_at=now,
            created_by="owner",
        )
        try:
            assert write_implementation_artifact(artifact_path.parent, artifact_path) == artifact
            _ = bind_qualification_prompt_release(
                connection, target_id, artifact_path, created_at=now, created_by="owner"
            )
            with pytest.raises(ValueError, match="empty, exact, and ambiguous"):
                _ = execute_deduplication_fixture_set(
                    connection,
                    target_id,
                    incomplete_id,
                    artifact_path,
                    api_key="fixture-key",
                    completed_at=now,
                    created_by="owner",
                    sender=send,
                )
            evidence_id = execute_deduplication_fixture_set(
                connection,
                target_id,
                fixture_id,
                artifact_path,
                api_key="fixture-key",
                completed_at=now,
                created_by="owner",
                sender=send,
            )
            row = connection.execute(
                "SELECT content FROM qualification_phase_evidence WHERE id = %s",
                (evidence_id,),
            ).fetchone()
            assert row is not None
            evidence = QualificationEvidence.model_validate(row[0])
            assert evidence.origin == "synthetic"
            assert evidence.outcome == "passed"
            assert evidence.result["passed_count"] == 3
            assert sent == len(evidence.attempts) == 1
        finally:
            artifact_path.unlink(missing_ok=True)


def test_composition_fixtures_run_production_claims_without_persisting_job_state(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 25, tzinfo=UTC)
    artifact_path = Path(__file__).resolve().parents[1] / "implementation-artifact.json"
    rates = ExchangeRateSnapshot(rates={"EUR": Decimal("1.10")}, source="fallback", observed_at=now)
    openrouter = ProviderExperimentSettings(provider="openrouter", temperature=0, retry_limit=0)
    typesafe = ProviderExperimentSettings(provider="typesafe", temperature=0, retry_limit=0)

    def fixture_case(
        *,
        raw_url: str,
        markdown: str,
        ats: AtsAvailable | AtsNotApplicable,
        outcome: str,
        stage: str,
        review_enqueued: bool,
    ) -> FixtureCase:
        return FixtureCase(
            input={
                "raw_url": raw_url,
                "keyword": "software engineer",
                "domain": "jobs.ashbyhq.com" if "ashbyhq" in raw_url else "jobs.lever.co",
                "scrape": {"kind": "succeeded", "markdown": markdown},
                "ats_evidence": ats.model_dump(mode="json"),
                "configuration_revision_id": INITIAL_SEARCH_CONFIGURATION_REVISION_ID,
                "exchange_rates": rates.model_dump(mode="json"),
                "openrouter_settings": openrouter.model_dump(mode="json"),
                "relevance_settings": typesafe.model_dump(mode="json"),
                "observed_at": now.isoformat(),
            },
            expected={
                "decision_outcome": outcome,
                "decision_stage": stage,
                "work_state": "completed",
                "review_enqueued": review_enqueued,
            },
            input_path="ats" if isinstance(ats, AtsAvailable) else "direct",
        )

    fixture = PhaseFixtureSet(
        phase="composition",
        cases=(
            fixture_case(
                raw_url="https://jobs.lever.co/acme/talent-pool",
                markdown="# Talent Pool\nJoin our team.",
                ats=AtsNotApplicable(),
                outcome="rejected",
                stage="structural",
                review_enqueued=False,
            ),
            fixture_case(
                raw_url="https://jobs.ashbyhq.com/acme/onsite-role",
                markdown="# Senior Software Engineer\nBuild useful tools. " * 20,
                ats=AtsAvailable(
                    source="ashby",
                    location="London",
                    locations=("London",),
                    workplace_type="OnSite",
                    country="GB",
                ),
                outcome="rejected",
                stage="ats_structural",
                review_enqueued=False,
            ),
            fixture_case(
                raw_url="https://jobs.lever.co/acme/product-engineer",
                markdown="# Senior Product Engineer\nBuild useful tools remotely. " * 20,
                ats=AtsNotApplicable(),
                outcome="qualified",
                stage="qualified",
                review_enqueued=True,
            ),
            fixture_case(
                raw_url="https://jobs.lever.co/acme/backend-engineer",
                markdown="# Senior Backend Engineer\nBuild useful tools remotely. " * 20,
                ats=AtsNotApplicable(),
                outcome="qualified",
                stage="qualified",
                review_enqueued=True,
            ),
            FixtureCase(
                input={
                    "raw_url": "https://jobs.lever.co/acme/unavailable-role",
                    "keyword": "software engineer",
                    "domain": "jobs.lever.co",
                    "scrape": {
                        "kind": "unavailable",
                        "operation": "scrape",
                        "error_code": "upstream_unavailable",
                        "reason": "Reader unavailable",
                    },
                    "ats_evidence": AtsNotApplicable().model_dump(mode="json"),
                    "configuration_revision_id": INITIAL_SEARCH_CONFIGURATION_REVISION_ID,
                    "exchange_rates": rates.model_dump(mode="json"),
                    "openrouter_settings": openrouter.model_dump(mode="json"),
                    "relevance_settings": typesafe.model_dump(mode="json"),
                    "observed_at": now.isoformat(),
                },
                expected={
                    "decision_outcome": None,
                    "decision_stage": None,
                    "work_state": "failed",
                    "review_enqueued": False,
                },
                input_path="direct",
            ),
        ),
    )
    enrich_calls = 0

    def model_sender(
        _url: str,
        _headers: Mapping[str, str],
        _body: dict[str, object],
        _timeout: float,
    ) -> HttpResponse:
        nonlocal enrich_calls
        choice = cast(dict[str, object], _body["tool_choice"])
        function = cast(dict[str, str], choice["function"])
        tool_name = function["name"]
        if tool_name == "enrich_job":
            title = "Senior Product Engineer" if enrich_calls == 0 else "Senior Backend Engineer"
            enrich_calls += 1
            output: Mapping[str, object] = {
                "title": title,
                "company": "Acme",
                "description": "Build useful tools.",
                "location": "Remote",
            }
        else:
            assert tool_name == "check_duplicate"
            output = {"isDuplicate": False}
        return HttpResponse(
            status_code=200,
            body=json.dumps(
                {
                    "id": f"generation-{tool_name}",
                    "model": "google/gemini-2.5-flash-001",
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "type": "function",
                                        "function": {
                                            "name": tool_name,
                                            "arguments": json.dumps(output),
                                        },
                                    }
                                ]
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 4, "cost": 0.00012},
                }
            ),
        )

    def jev_sender(
        _url: str,
        _headers: Mapping[str, str],
        body: dict[str, object],
        _timeout: float,
    ) -> JevHttpResponse:
        questions = cast(dict[str, object], body["questions"])
        return JevHttpResponse(
            status_code=200,
            body=json.dumps(
                {
                    "model": JEV_MODEL,
                    "answers": {
                        name: {
                            "type": "noul",
                            "noul": (1.0 if name == "owns_product_delivery" else 0.0),
                        }
                        for name in questions
                    },
                    "usage": {"input_tokens": 12, "output_tokens": len(questions)},
                }
            ),
        )

    with _connection(authority_schema) as connection:
        _ = apply_migrations(connection)
        jev_release_id = store_relevance_release(
            connection,
            build_relevance_release(build_jev_atomic_policy()),
            created_at=now,
            created_by="contract",
        )
        artifact, _, target = _store_default_qualification_target(
            connection, now, relevance_release_id=jev_release_id.id
        )
        target_id = qualification_target_id(target)
        fixture_id = store_fixture_set(connection, fixture, created_at=now, created_by="owner")
        try:
            assert write_implementation_artifact(artifact_path.parent, artifact_path) == artifact
            _ = bind_qualification_prompt_release(
                connection, target_id, artifact_path, created_at=now, created_by="owner"
            )
            evidence_id = execute_composition_fixture_set(
                connection,
                target_id,
                fixture_id,
                artifact_path,
                openrouter_api_key="unused",
                typesafe_api_key="unused",
                completed_at=now,
                created_by="owner",
                model_sender=model_sender,
                jev_sender=jev_sender,
            )
            row = connection.execute(
                "SELECT content FROM qualification_phase_evidence WHERE id = %s",
                (evidence_id,),
            ).fetchone()
            assert row is not None
            evidence = QualificationEvidence.model_validate(row[0])
            assert evidence.outcome == "passed", evidence.result
            assert evidence.origin == "synthetic"
            assert evidence.result["passed_count"] == 5
            assert len(evidence.attempts) >= 15
            assert connection.execute(
                "SELECT DISTINCT provider FROM qualification_provider_attempts WHERE evidence_id = %s",
                (evidence_id,),
            ).fetchall() == [("openrouter",), ("typesafe",)]
            assert connection.execute("SELECT count(*) FROM jobs").fetchone() == (0,)
            assert connection.execute("SELECT count(*) FROM pipeline_runs").fetchone() == (0,)
            assert connection.execute("SELECT count(*) FROM model_call_attempts").fetchone() == (0,)
        finally:
            artifact_path.unlink(missing_ok=True)


def test_qualification_evidence_pins_target_components_and_experiment_inputs(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 25, tzinfo=UTC)
    with _connection(authority_schema) as connection:
        manifest_id, _, rates = _seed_evaluation_execution_context(connection, now)
        artifact, _, target = _store_default_qualification_target(connection, now)
        experiment = RelevanceExperimentInput(
            manifest_id=manifest_id,
            exchange_rates=rates,
            provider_settings=ProviderExperimentSettings(
                provider="openrouter", temperature=0, seed=1, retry_limit=2
            ),
            input_path="direct",
        )
        experiment_id = store_relevance_experiment_input(
            connection, experiment, created_at=now, created_by="owner"
        )
        baseline = QualificationEvidence(
            target_id=qualification_target_id(target),
            phase="relevance",
            component_release_id=target.relevance_release_id,
            experiment_input_id=experiment_id,
            executor_artifact_id=artifact.id,
            origin="synthetic",
            outcome="passed",
            result={"qualified": 1},
            completed_at=now,
        )
        candidate = baseline.model_copy(update={"result": {"qualified": 2}})
        _ = store_qualification_evidence(connection, baseline, created_at=now, created_by="owner")
        _ = store_qualification_evidence(connection, candidate, created_at=now, created_by="owner")
        comparison_id = record_relevance_comparison(
            connection, baseline, candidate, created_at=now, created_by="owner"
        )
        assert connection.execute(
            "SELECT experiment_input_id FROM qualification_relevance_comparisons WHERE id = %s",
            (comparison_id,),
        ).fetchone() == (experiment_id,)

        changed_experiment = experiment.model_copy(update={"input_path": "ats"})
        changed_id = store_relevance_experiment_input(
            connection, changed_experiment, created_at=now, created_by="owner"
        )
        changed_candidate = candidate.model_copy(update={"experiment_input_id": changed_id})
        changed_evidence_id = store_qualification_evidence(
            connection, changed_candidate, created_at=now, created_by="owner"
        )
        with pytest.raises(ValueError, match="same frozen experiment input"):
            _ = record_relevance_comparison(
                connection, baseline, changed_candidate, created_at=now, created_by="owner"
            )
        forged_comparison = {
            "experiment_input_id": experiment_id,
            "baseline_evidence_id": qualification_evidence_id(baseline),
            "candidate_evidence_id": changed_evidence_id,
        }
        forged_id = hashlib.sha256(
            json.dumps(forged_comparison, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        with pytest.raises(psycopg.errors.CheckViolation):
            _ = connection.execute(
                """
                INSERT INTO qualification_relevance_comparisons (
                  id, experiment_input_id, baseline_evidence_id,
                  candidate_evidence_id, created_at, created_by
                ) VALUES (%s, %s, %s, %s, %s, 'owner')
                """,
                (
                    forged_id,
                    experiment_id,
                    forged_comparison["baseline_evidence_id"],
                    changed_evidence_id,
                    now,
                ),
            )

        fixtures = PhaseFixtureSet(
            phase="enrichment",
            cases=(
                FixtureCase(
                    input={"title": "Engineer"},
                    expected={"title": "Engineer"},
                    input_path="direct",
                ),
            ),
        )
        fixture_id = store_fixture_set(connection, fixtures, created_at=now, created_by="owner")
        enrichment_evidence = QualificationEvidence(
            target_id=qualification_target_id(target),
            phase="enrichment",
            component_release_id=target.enrichment_release_id,
            fixture_set_id=fixture_id,
            executor_artifact_id=artifact.id,
            origin="synthetic",
            outcome="passed",
            result={"matched": 1},
            completed_at=now,
        )
        _ = store_qualification_evidence(
            connection, enrichment_evidence, created_at=now, created_by="owner"
        )
        wrong_component = enrichment_evidence.model_copy(
            update={"component_release_id": target.deduplication_release_id}
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            _ = store_qualification_evidence(
                connection, wrong_component, created_at=now, created_by="owner"
            )


def test_owner_bootstrap_is_single_winner_and_authenticates_from_postgres(
    authority_schema: str,
) -> None:
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
    service = postgres_owner_access_service(lambda: _connection(authority_schema))
    barrier = Barrier(2)

    def bootstrap(password: str) -> object:
        _ = barrier.wait()
        return service.bootstrap(password)

    passwords = ("first secure owner password", "second secure owner password")
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(bootstrap, passwords))

    assert sum(isinstance(result, OwnerBootstrapped) for result in results) == 1
    assert service.load_state().stage is OnboardingStage.PROVIDERS
    assert sum(service.authenticate(password) for password in passwords) == 1
    with _connection(authority_schema) as connection:
        with pytest.raises(psycopg.errors.CheckViolation, match="cannot be replaced or removed"):
            connection.execute(
                """
                UPDATE owner_onboarding
                SET stage = 'owner_account', password_hash = NULL
                WHERE singleton_id = 1
                """
            )
        with pytest.raises(psycopg.errors.CheckViolation, match="cannot be deleted"):
            connection.execute("DELETE FROM owner_onboarding WHERE singleton_id = 1")


def test_existing_installation_requires_and_idempotently_imports_legacy_owner(
    authority_schema: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _connection(authority_schema) as connection:
        _apply_migrations_through(connection, "0025_release_target_promotion_decisions.sql")
        _ = _seed_initial_configuration_publication(connection, datetime(2026, 9, 22, tzinfo=UTC))
        _ = connection.execute(
            """
            INSERT INTO active_search_configuration (
              singleton_id, revision_id, generation, activated_at, activated_by
            ) VALUES (1, %s, 0, %s, 'test')
            """,
            (
                INITIAL_SEARCH_CONFIGURATION_REVISION_ID,
                datetime(2026, 9, 22, tzinfo=UTC),
            ),
        )

    settings = PostgresContractSettings.from_environment()
    monkeypatch.setenv(
        "JOB_FINDER_POSTGRES_DSN",
        f"{settings.postgres_dsn}?options=-csearch_path%3D{authority_schema}",
    )
    monkeypatch.setenv("JOB_FINDER_REVIEW_PASSWORD", "legacy secure owner password")
    monkeypatch.delenv("JOB_FINDER_BOOTSTRAP_TOKEN", raising=False)
    monkeypatch.setenv("JOB_FINDER_REVIEW_SESSION_SECRET", "s" * 32)
    monkeypatch.setenv("JOB_FINDER_ENABLE_SPLIT_EXECUTION", "true")
    monkeypatch.setenv("JOB_FINDER_IMPLEMENTATION_ARTIFACT", "/unused/artifact.json")
    monkeypatch.delenv("JOB_FINDER_DAGSTER_GRAPHQL_URL", raising=False)

    _ = create_review_server()

    with _connection(authority_schema) as connection:
        imported = import_legacy_owner_password(connection, "different owner password")
        replayed = import_legacy_owner_password(connection, "different owner password")
        stored = connection.execute(
            "SELECT password_hash FROM owner_onboarding WHERE singleton_id = 1"
        ).fetchone()

    service = postgres_owner_access_service(lambda: _connection(authority_schema))
    assert imported.stage is OnboardingStage.COMPLETE
    assert replayed == imported
    assert stored is not None
    assert "legacy secure owner password" not in str(stored[0])
    assert service.authenticate("legacy secure owner password") is True
    assert service.authenticate("different owner password") is False
    with _connection(authority_schema) as connection:
        admission = admit_scheduled_execution(
            connection,
            idempotency_key="legacy-without-owner-budget",
            requested_at=datetime(2026, 9, 22, tzinfo=UTC),
        )
        assert isinstance(admission, ExecutionAdmitted)
        assert admission.max_jobs == 100
