from __future__ import annotations

from contracts.test_postgres_authority import (
    AtsAvailable,
    AtsNotApplicable,
    Decimal,
    FixtureCase,
    HttpResponse,
    InputDigest,
    JsonValue,
    Mapping,
    ModelCallAttempt,
    ModelCallContext,
    ModelRequestId,
    Path,
    PhaseFixtureSet,
    PromptRelease,
    PromptReleaseId,
    ProviderExperimentSettings,
    QualificationEvidence,
    RelevanceExperimentInput,
    TypeAdapter,
    UTC,
    _connection,
    _decision_listing,
    _seed_evaluation_execution_context,
    _store_default_qualification_target,
    apply_migrations,
    bind_qualification_prompt_release,
    datetime,
    execute_enrichment_fixture_set,
    execute_input_preparation_fixture_set,
    execute_relevance_experiment,
    hashlib,
    json,
    load_compiled_qualification_target,
    provider_attempt_evidence,
    psycopg,
    pytest,
    qualification_target_id,
    replace,
    store_fixture_set,
    store_prompt_release,
    store_provider_attempts,
    store_qualification_evidence,
    store_relevance_experiment_input,
    uuid4,
    write_implementation_artifact,
)

pytest_plugins = ("contracts.test_postgres_authority",)


def test_qualification_prompt_compilation_requires_exact_published_release(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 25, tzinfo=UTC)
    artifact_path = Path(__file__).resolve().parents[1] / "implementation-artifact.json"
    with _connection(authority_schema) as connection:
        _ = apply_migrations(connection)
        artifact, _, target = _store_default_qualification_target(connection, now)
        target_id = qualification_target_id(target)
        try:
            assert write_implementation_artifact(artifact_path.parent, artifact_path) == artifact
            release_id = bind_qualification_prompt_release(
                connection, target_id, artifact_path, created_at=now, created_by="owner"
            )
            compiled = load_compiled_qualification_target(connection, target_id, artifact_path)
            assert compiled.target.id == target_id
            assert compiled.prompt_release.id == release_id
            assert (
                bind_qualification_prompt_release(
                    connection, target_id, artifact_path, created_at=now, created_by="owner"
                )
                == release_id
            )
            partial_versions = compiled.prompt_release.versions[:-1]
            partial_digest = hashlib.sha256(
                json.dumps(
                    [[version.definition.name, version.id] for version in partial_versions],
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode()
            ).hexdigest()
            partial_release = PromptRelease(
                id=PromptReleaseId(partial_digest),
                name=f"partial-{partial_digest}",
                content_digest=partial_digest,
                versions=partial_versions,
            )
            _ = store_prompt_release(
                connection, partial_release, created_at=now, created_by="owner"
            )
            with pytest.raises(psycopg.errors.CheckViolation, match="exact ordered"):
                _ = connection.execute(
                    """
                    INSERT INTO qualification_prompt_compilations (
                      target_id, artifact_id, qualification_definition_revision_id,
                      prompt_release_id, created_at, created_by
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        target_id,
                        artifact.id,
                        target.qualification_definition_revision_id,
                        partial_release.id,
                        now,
                        "owner",
                    ),
                )
            with pytest.raises(psycopg.errors.CheckViolation):
                _ = connection.execute(
                    """
                    INSERT INTO qualification_prompt_compilations (
                      target_id, artifact_id, qualification_definition_revision_id,
                      prompt_release_id, created_at, created_by
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        target_id,
                        "0" * 64,
                        target.qualification_definition_revision_id,
                        release_id,
                        now,
                        "owner",
                    ),
                )
        finally:
            artifact_path.unlink(missing_ok=True)


def test_input_preparation_fixtures_execute_shared_production_path(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 25, tzinfo=UTC)
    artifact_path = Path(__file__).resolve().parents[1] / "implementation-artifact.json"
    markdown = "Title: Backend engineer\nBuild services."
    url = "https://jobs.lever.co/acme/role-1"
    direct_evidence = AtsNotApplicable().model_dump(mode="json")
    ats_evidence = AtsAvailable(
        source="lever",
        location="Berlin",
        locations=("Berlin",),
        workplace_type="OnSite",
        country="DE",
        description="A detailed ATS description",
    ).model_dump(mode="json")
    listing: dict[str, JsonValue] = {
        "title": "Backend engineer",
        "company": "acme",
        "url": url,
        "source": "lever",
        "keywords_matched": ["backend"],
        "date_posted": None,
        "date_scraped": "2026-09-25",
        "description": markdown,
        "location": "",
        "profile": "",
    }
    ats_description = (
        "## ATS Structured Data (from lever API)\n"
        "- Primary location: Berlin\n"
        "- All listed locations: Berlin\n"
        "- Workplace type: OnSite\n"
        "- Country fallback when locations are non-geographic: DE\n"
        "---\n\nA detailed ATS description"
    )
    fixture = PhaseFixtureSet(
        phase="input_preparation",
        cases=(
            FixtureCase(
                input={
                    "markdown": markdown,
                    "url": url,
                    "keyword": "backend",
                    "scraped_on": "2026-09-25",
                    "ats_evidence": direct_evidence,
                },
                expected={
                    "listing": listing,
                    "ats_evidence": direct_evidence,
                    "body": markdown,
                    "structural_decision": {"kind": "pass"},
                },
                input_path="direct",
            ),
            FixtureCase(
                input={
                    "markdown": markdown,
                    "url": url,
                    "keyword": "backend",
                    "scraped_on": "2026-09-25",
                    "ats_evidence": ats_evidence,
                },
                expected={
                    "listing": {**listing, "description": ats_description, "location": "Berlin"},
                    "ats_evidence": ats_evidence,
                    "body": "A detailed ATS description",
                    "structural_decision": {
                        "kind": "rejected",
                        "reason": "ATS workplaceType=OnSite (Berlin)",
                    },
                },
                input_path="ats",
            ),
        ),
    )
    with _connection(authority_schema) as connection:
        _ = apply_migrations(connection)
        artifact, _, target = _store_default_qualification_target(connection, now)
        target_id = qualification_target_id(target)
        fixture_id = store_fixture_set(connection, fixture, created_at=now, created_by="owner")
        try:
            assert write_implementation_artifact(artifact_path.parent, artifact_path) == artifact
            _ = bind_qualification_prompt_release(
                connection, target_id, artifact_path, created_at=now, created_by="owner"
            )
            evidence_id = execute_input_preparation_fixture_set(
                connection,
                target_id,
                fixture_id,
                artifact_path,
                completed_at=now,
                created_by="owner",
            )
            row = connection.execute(
                "SELECT content FROM qualification_phase_evidence WHERE id = %s", (evidence_id,)
            ).fetchone()
            assert row is not None
            evidence = QualificationEvidence.model_validate(row[0])
            assert evidence.origin == "canonical"
            assert evidence.outcome == "passed"
            assert evidence.result["case_count"] == 2
            assert evidence.result["passed_count"] == 2
        finally:
            artifact_path.unlink(missing_ok=True)


def test_qualification_provider_attempts_retain_raw_model_provenance(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 25, tzinfo=UTC)
    artifact_path = Path(__file__).resolve().parents[1] / "implementation-artifact.json"
    with _connection(authority_schema) as connection:
        _ = apply_migrations(connection)
        artifact, _, target = _store_default_qualification_target(connection, now)
        target_id = qualification_target_id(target)
        fixture_id = store_fixture_set(
            connection,
            PhaseFixtureSet(
                phase="enrichment",
                cases=(FixtureCase(input={}, expected={}, input_path="direct"),),
            ),
            created_at=now,
            created_by="owner",
        )
        try:
            assert write_implementation_artifact(artifact_path.parent, artifact_path) == artifact
            release_id = bind_qualification_prompt_release(
                connection, target_id, artifact_path, created_at=now, created_by="owner"
            )
            compiled = load_compiled_qualification_target(connection, target_id, artifact_path)
            prompt = next(
                version
                for version in compiled.prompt_release.versions
                if version.definition.phase == "enrichment"
            )
            attempt = ModelCallAttempt(
                id=uuid4(),
                context=ModelCallContext(
                    processing_attempt_id=uuid4(),
                    pipeline_run_id=uuid4(),
                    prompt_release_id=release_id,
                    operation_key="benchmark:enrichment",
                    input_digest=InputDigest("a" * 64),
                ),
                request_id=ModelRequestId("b" * 64),
                attempt_number=0,
                prompt_name=prompt.definition.name,
                prompt_version_id=prompt.id,
                requested_model="fixture-model",
                response_model="observed-model",
                provider_response_id="response-1",
                status="accepted",
                parsed_output={"company": "Acme"},
                raw_response={"choices": [{"message": {"content": "Acme"}}]},
                input_tokens=12,
                output_tokens=4,
                cost_usd=Decimal("0.000001"),
                latency_ms=27,
                error=None,
                observed_at=now,
                request_messages=({"role": "user", "content": "source listing"},),
            )
            evidence = QualificationEvidence(
                target_id=target_id,
                phase="enrichment",
                component_release_id=target.enrichment_release_id,
                fixture_set_id=fixture_id,
                executor_artifact_id=artifact.id,
                origin="canonical",
                outcome="passed",
                result={"case_count": 1},
                attempts=(provider_attempt_evidence(attempt),),
                completed_at=now,
            )
            evidence_id = store_qualification_evidence(
                connection, evidence, created_at=now, created_by="owner"
            )
            store_provider_attempts(
                connection,
                evidence,
                (attempt,),
                provider="openrouter",
                created_at=now,
                created_by="owner",
            )
            row = connection.execute(
                "SELECT content FROM qualification_provider_attempts WHERE evidence_id = %s",
                (evidence_id,),
            ).fetchone()
            assert row is not None
            content = TypeAdapter(dict[str, JsonValue]).validate_python(row[0])
            assert content["request_messages"] == [{"role": "user", "content": "source listing"}]
            assert content["raw_response"] == attempt.raw_response
            assert content["input_tokens"] == 12
            synthetic_evidence = evidence.model_copy(update={"origin": "synthetic"})
            _ = store_qualification_evidence(
                connection, synthetic_evidence, created_at=now, created_by="owner"
            )
            synthetic_attempt = replace(attempt, id=uuid4(), request_id=ModelRequestId("c" * 64))
            store_provider_attempts(
                connection,
                synthetic_evidence,
                (synthetic_attempt,),
                provider="openrouter",
                created_at=now,
                created_by="owner",
            )
            with pytest.raises(ValueError, match="summaries differ"):
                store_provider_attempts(
                    connection,
                    evidence.model_copy(update={"attempts": ()}),
                    (attempt,),
                    provider="openrouter",
                    created_at=now,
                    created_by="owner",
                )
        finally:
            artifact_path.unlink(missing_ok=True)


def test_direct_relevance_experiment_resolves_target_and_marks_injected_sender_synthetic(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 25, tzinfo=UTC)
    artifact_path = Path(__file__).resolve().parents[1] / "implementation-artifact.json"
    with _connection(authority_schema) as connection:
        manifest_id, legacy_target, rates = _seed_evaluation_execution_context(connection, now)
        artifact, _, target = _store_default_qualification_target(
            connection, now, relevance_release_id=legacy_target.relevance_release_id
        )
        target_id = qualification_target_id(target)
        frozen = RelevanceExperimentInput(
            manifest_id=manifest_id,
            exchange_rates=rates,
            provider_settings=ProviderExperimentSettings(
                provider="openrouter", temperature=0, retry_limit=0
            ),
            input_path="direct",
        )
        input_id = store_relevance_experiment_input(
            connection, frozen, created_at=now, created_by="owner"
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
                        "id": f"generation-{sent}",
                        "model": "google/gemini-2.5-flash-001",
                        "choices": [
                            {
                                "message": {
                                    "tool_calls": [
                                        {
                                            "type": "function",
                                            "function": {
                                                "name": "evaluate_job",
                                                "arguments": json.dumps(
                                                    {"pass": True, "reason": "matched"}
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

        try:
            assert write_implementation_artifact(artifact_path.parent, artifact_path) == artifact
            _ = bind_qualification_prompt_release(
                connection, target_id, artifact_path, created_at=now, created_by="owner"
            )
            evidence_id = execute_relevance_experiment(
                connection,
                target_id,
                input_id,
                artifact_path,
                api_key="fixture-key",
                completed_at=now,
                created_by="owner",
                openrouter_sender=send,
            )
            row = connection.execute(
                "SELECT content FROM qualification_phase_evidence WHERE id = %s",
                (evidence_id,),
            ).fetchone()
            assert row is not None
            evidence = QualificationEvidence.model_validate(row[0])
            assert evidence.origin == "synthetic"
            assert evidence.outcome == "passed"
            assert sent > 0
            assert len(evidence.attempts) == sent
            assert connection.execute(
                "SELECT count(*) FROM qualification_provider_attempts WHERE evidence_id = %s",
                (evidence_id,),
            ).fetchone() == (sent,)
            ats_input_id = store_relevance_experiment_input(
                connection,
                frozen.model_copy(update={"input_path": "ats"}),
                created_at=now,
                created_by="owner",
            )
            with pytest.raises(ValueError, match="ATS-available source evidence"):
                _ = execute_relevance_experiment(
                    connection,
                    target_id,
                    ats_input_id,
                    artifact_path,
                    api_key="fixture-key",
                    completed_at=now,
                    created_by="owner",
                    openrouter_sender=send,
                )
            wrong_settings = frozen.model_copy(
                update={
                    "provider_settings": frozen.provider_settings.model_copy(
                        update={"temperature": 1.0}
                    )
                }
            )
            wrong_input_id = store_relevance_experiment_input(
                connection, wrong_settings, created_at=now, created_by="owner"
            )
            with pytest.raises(ValueError, match="Frozen temperature"):
                _ = execute_relevance_experiment(
                    connection,
                    target_id,
                    wrong_input_id,
                    artifact_path,
                    api_key="fixture-key",
                    completed_at=now,
                    created_by="owner",
                    openrouter_sender=send,
                )
        finally:
            artifact_path.unlink(missing_ok=True)


def test_ats_relevance_experiment_requires_prepared_source_evidence(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 25, tzinfo=UTC)
    artifact_path = Path(__file__).resolve().parents[1] / "implementation-artifact.json"
    ats_evidence = AtsAvailable(
        source="lever",
        location="Berlin",
        locations=("Berlin",),
        workplace_type="Remote",
        country="DE",
        description="Build useful tools from anywhere in Europe.",
    )
    with _connection(authority_schema) as connection:
        manifest_id, legacy_target, rates = _seed_evaluation_execution_context(
            connection, now, ats_evidence=ats_evidence
        )
        artifact, _, target = _store_default_qualification_target(
            connection, now, relevance_release_id=legacy_target.relevance_release_id
        )
        target_id = qualification_target_id(target)
        frozen = RelevanceExperimentInput(
            manifest_id=manifest_id,
            exchange_rates=rates,
            provider_settings=ProviderExperimentSettings(
                provider="openrouter", temperature=0, retry_limit=0
            ),
            input_path="ats",
        )
        input_id = store_relevance_experiment_input(
            connection, frozen, created_at=now, created_by="owner"
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
                        "id": f"ats-generation-{sent}",
                        "model": "google/gemini-2.5-flash-001",
                        "choices": [
                            {
                                "message": {
                                    "tool_calls": [
                                        {
                                            "type": "function",
                                            "function": {
                                                "name": "evaluate_job",
                                                "arguments": json.dumps(
                                                    {"pass": True, "reason": "matched"}
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

        try:
            assert write_implementation_artifact(artifact_path.parent, artifact_path) == artifact
            _ = bind_qualification_prompt_release(
                connection, target_id, artifact_path, created_at=now, created_by="owner"
            )
            evidence_id = execute_relevance_experiment(
                connection,
                target_id,
                input_id,
                artifact_path,
                api_key="fixture-key",
                completed_at=now,
                created_by="owner",
                openrouter_sender=send,
            )
            row = connection.execute(
                "SELECT content FROM qualification_phase_evidence WHERE id = %s",
                (evidence_id,),
            ).fetchone()
            assert row is not None
            evidence = QualificationEvidence.model_validate(row[0])
            assert evidence.origin == "synthetic"
            assert evidence.outcome == "passed"
            assert evidence.experiment_input_id == input_id
            assert len(evidence.attempts) == sent > 0
        finally:
            artifact_path.unlink(missing_ok=True)


def test_ats_relevance_rejects_snapshot_without_prepared_description(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 25, tzinfo=UTC)
    artifact_path = Path(__file__).resolve().parents[1] / "implementation-artifact.json"
    ats_evidence = AtsAvailable(
        source="lever",
        location="Berlin",
        locations=("Berlin",),
        workplace_type="Remote",
        country="DE",
        description="ATS description",
    )
    with _connection(authority_schema) as connection:
        manifest_id, legacy_target, rates = _seed_evaluation_execution_context(
            connection,
            now,
            ats_evidence=ats_evidence,
            prepare_ats_description=False,
        )
        artifact, _, target = _store_default_qualification_target(
            connection, now, relevance_release_id=legacy_target.relevance_release_id
        )
        target_id = qualification_target_id(target)
        input_id = store_relevance_experiment_input(
            connection,
            RelevanceExperimentInput(
                manifest_id=manifest_id,
                exchange_rates=rates,
                provider_settings=ProviderExperimentSettings(
                    provider="openrouter", temperature=0, retry_limit=0
                ),
                input_path="ats",
            ),
            created_at=now,
            created_by="owner",
        )
        try:
            assert write_implementation_artifact(artifact_path.parent, artifact_path) == artifact
            _ = bind_qualification_prompt_release(
                connection, target_id, artifact_path, created_at=now, created_by="owner"
            )
            with pytest.raises(ValueError, match="lacks the prepared ATS description"):
                _ = execute_relevance_experiment(
                    connection,
                    target_id,
                    input_id,
                    artifact_path,
                    api_key="fixture-key",
                    completed_at=now,
                    created_by="owner",
                )
        finally:
            artifact_path.unlink(missing_ok=True)


def test_enrichment_fixtures_use_compiled_prompt_and_record_synthetic_attempts(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 25, tzinfo=UTC)
    artifact_path = Path(__file__).resolve().parents[1] / "implementation-artifact.json"
    expected: dict[str, JsonValue] = {
        "title": "Backend Engineer",
        "company": "Acme",
        "description": "## Overview\nBuild useful tools.",
        "location": "Remote",
        "compensation": None,
    }
    fixture = PhaseFixtureSet(
        phase="enrichment",
        cases=(
            FixtureCase(
                input={
                    "job": _decision_listing().model_dump(mode="json"),
                    "provider_settings": ProviderExperimentSettings(
                        provider="openrouter", temperature=0, retry_limit=0
                    ).model_dump(mode="json"),
                },
                expected=expected,
                input_path="direct",
            ),
        ),
    )

    def send(
        _url: str,
        _headers: Mapping[str, str],
        _body: dict[str, object],
        _timeout: float,
    ) -> HttpResponse:
        return HttpResponse(
            status_code=200,
            body=json.dumps(
                {
                    "id": "enrichment-generation-1",
                    "model": "google/gemini-2.5-flash-lite",
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "type": "function",
                                        "function": {
                                            "name": "enrich_job",
                                            "arguments": json.dumps(expected),
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
        try:
            assert write_implementation_artifact(artifact_path.parent, artifact_path) == artifact
            _ = bind_qualification_prompt_release(
                connection, target_id, artifact_path, created_at=now, created_by="owner"
            )
            evidence_id = execute_enrichment_fixture_set(
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
            assert evidence.result["passed_count"] == 1
            assert len(evidence.attempts) == 1
            assert connection.execute(
                "SELECT count(*) FROM qualification_provider_attempts WHERE evidence_id = %s",
                (evidence_id,),
            ).fetchone() == (1,)
        finally:
            artifact_path.unlink(missing_ok=True)
