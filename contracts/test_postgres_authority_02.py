from __future__ import annotations

from contracts.test_postgres_authority import (
    ActivateQualificationTargetCommand,
    Decimal,
    FixtureCase,
    ImplementationArtifact,
    InputDigest,
    JsonValue,
    ModelCallAttempt,
    ModelCallContext,
    ModelRequestId,
    NamedTemporaryFile,
    Path,
    PhaseFixtureSet,
    PromotionEvidenceSelection,
    PromptReleaseId,
    ProviderExperimentSettings,
    QualificationActivationError,
    QualificationEvidence,
    QualificationEvidenceId,
    QualificationTargetId,
    Qualified,
    RelevanceExperimentInput,
    TypeAdapter,
    UTC,
    _connection,
    _seed_evaluation_execution_context,
    _store_default_qualification_target,
    activate_qualification_target,
    apply_migrations,
    bind_qualification_prompt_release,
    build_gemini_policy,
    build_jev_faithful_policy,
    build_qualification_target,
    build_relevance_release,
    component_release_id,
    datetime,
    get_active_qualification_target,
    implementation_artifact_id,
    load_manifest,
    load_prompt_release,
    load_qualification_target,
    preview_qualification_promotion,
    provider_attempt_evidence,
    psycopg,
    pytest,
    qualification_target_id,
    record_qualification_promotion_decision,
    record_relevance_comparison,
    replace,
    score_results,
    score_trial,
    store_component_release,
    store_fixture_set,
    store_implementation_artifact,
    store_provider_attempts,
    store_qualification_evidence,
    store_qualification_target,
    store_relevance_experiment_input,
    store_relevance_release,
    timedelta,
    uuid4,
    write_implementation_artifact,
)

pytest_plugins = ("contracts.test_postgres_authority",)


def test_composite_promotion_preview_requires_exact_canonical_evidence(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 25, tzinfo=UTC)
    root = Path(__file__).resolve().parents[1]
    with NamedTemporaryFile(
        dir=root, prefix=".qualification-preview-", suffix=".json"
    ) as temporary:
        artifact_path = Path(temporary.name)
        with _connection(authority_schema) as connection:
            _ = apply_migrations(connection)
            artifact, components, baseline = _store_default_qualification_target(connection, now)
            alternate_input = components[0].model_copy(
                update={"ats_sources": components[0].ats_sources[:-1]}
            )
            _ = store_component_release(
                connection, alternate_input, created_at=now, created_by="owner"
            )
            candidate = build_qualification_target(
                alternate_input, components[1], components[2], components[3]
            )
            candidate_id = store_qualification_target(
                connection, candidate, created_at=now, created_by="owner"
            )
            baseline_id = qualification_target_id(baseline)
            _ = connection.execute(
                """
                UPDATE active_qualification_target
                SET target_id = %s, generation = 1, activated_at = %s, activated_by = 'owner'
                WHERE singleton_id = 1
                """,
                (baseline_id, now),
            )
            fixture_case = FixtureCase(input={}, expected={}, input_path="direct")
            input_fixture_id = store_fixture_set(
                connection,
                PhaseFixtureSet(phase="input_preparation", cases=(fixture_case,)),
                created_at=now,
                created_by="owner",
            )
            composition_fixture_id = store_fixture_set(
                connection,
                PhaseFixtureSet(phase="composition", cases=(fixture_case,)),
                created_at=now,
                created_by="owner",
            )
            result: dict[str, JsonValue] = {
                "case_count": 1,
                "passed_count": 1,
                "cases": [{"passed": True, "observed": {}, "expected": {}}],
            }
            input_evidence = QualificationEvidence(
                target_id=candidate_id,
                phase="input_preparation",
                component_release_id=candidate.input_preparation_release_id,
                fixture_set_id=input_fixture_id,
                executor_artifact_id=artifact.id,
                origin="canonical",
                outcome="passed",
                result=result,
                completed_at=now,
            )
            input_evidence_id = store_qualification_evidence(
                connection, input_evidence, created_at=now, created_by="owner"
            )
            assert write_implementation_artifact(root, artifact_path) == artifact
            release_id = bind_qualification_prompt_release(
                connection, candidate_id, artifact_path, created_at=now, created_by="owner"
            )
            prompt = load_prompt_release(connection, release_id).version("job-finder-enrichment")
            attempt = ModelCallAttempt(
                id=uuid4(),
                context=ModelCallContext(
                    processing_attempt_id=uuid4(),
                    pipeline_run_id=uuid4(),
                    prompt_release_id=release_id,
                    operation_key="enrichment",
                    input_digest=InputDigest("a" * 64),
                ),
                request_id=ModelRequestId("b" * 64),
                attempt_number=0,
                prompt_name=prompt.definition.name,
                prompt_version_id=prompt.id,
                requested_model="google/gemini-2.5-flash-001",
                response_model="google/gemini-2.5-flash-001",
                provider_response_id="response-1",
                status="accepted",
                parsed_output={"title": "Backend Engineer"},
                raw_response={"id": "response-1"},
                input_tokens=12,
                output_tokens=4,
                cost_usd=Decimal("0.00012"),
                latency_ms=2,
                error=None,
                observed_at=now,
                request_messages=({"role": "user", "content": "source listing"},),
            )
            composition_evidence = QualificationEvidence(
                target_id=candidate_id,
                phase="composition",
                fixture_set_id=composition_fixture_id,
                executor_artifact_id=artifact.id,
                origin="canonical",
                outcome="passed",
                result=TypeAdapter(dict[str, JsonValue]).validate_python(
                    result
                    | {
                        "coverage": dict.fromkeys(
                            (
                                "direct",
                                "ats",
                                "qualified",
                                "rejected",
                                "retry",
                                "relevance",
                                "enrichment",
                                "deduplication",
                            ),
                            True,
                        )
                    }
                ),
                attempts=(provider_attempt_evidence(attempt),),
                completed_at=now,
            )
            composition_evidence_id = store_qualification_evidence(
                connection, composition_evidence, created_at=now, created_by="owner"
            )
            store_provider_attempts(
                connection,
                composition_evidence,
                (attempt,),
                provider="openrouter",
                created_at=now,
                created_by="owner",
            )
            selected = PromotionEvidenceSelection(
                input_preparation_evidence_id=input_evidence_id,
                composition_evidence_id=composition_evidence_id,
            )
            preview = preview_qualification_promotion(
                connection, baseline_id, candidate_id, selected, artifact_path
            )
            assert preview.eligible and preview.failures == ()
            approved = record_qualification_promotion_decision(
                connection,
                baseline_target_id=baseline_id,
                candidate_target_id=candidate_id,
                evidence=selected,
                artifact_path=artifact_path,
                decision="approved",
                reason="Frozen evidence passed",
                actor="owner",
                created_at=now,
                idempotency_key="approved-composite-preview",
            )
            assert approved.decision == "approved"
            initial = get_active_qualification_target(connection)
            assert initial.target_id == baseline_id and initial.generation == 1
            activation = ActivateQualificationTargetCommand(
                idempotency_key="activate-composite-preview",
                promotion_decision_id=approved.id,
                expected_target_id=baseline_id,
                expected_generation=1,
                actor="owner",
                timestamp=now,
            )
            activated = activate_qualification_target(connection, activation, artifact_path)
            assert activated.outcome == "activated"
            assert activated.observed == initial
            assert activated.resulting_generation == 2
            assert get_active_qualification_target(connection).target_id == candidate_id

            assert activate_qualification_target(connection, activation, artifact_path).replayed
            with pytest.raises(QualificationActivationError, match="another activation request"):
                _ = activate_qualification_target(
                    connection,
                    activation.model_copy(update={"actor": "different"}),
                    artifact_path,
                )
            stale = activate_qualification_target(
                connection,
                activation.model_copy(update={"idempotency_key": "stale-composite-preview"}),
                artifact_path,
            )
            assert stale.outcome == "active_changed"
            assert stale.observed.target_id == candidate_id
            assert stale.resulting_generation == 2
            assert (
                record_qualification_promotion_decision(
                    connection,
                    baseline_target_id=baseline_id,
                    candidate_target_id=candidate_id,
                    evidence=selected,
                    artifact_path=artifact_path,
                    decision="approved",
                    reason="Frozen evidence passed",
                    actor="owner",
                    created_at=now,
                    idempotency_key="approved-composite-preview",
                )
                == approved
            )
            with pytest.raises(ValueError, match="different promotion decision"):
                _ = record_qualification_promotion_decision(
                    connection,
                    baseline_target_id=baseline_id,
                    candidate_target_id=candidate_id,
                    evidence=selected,
                    artifact_path=artifact_path,
                    decision="rejected",
                    reason="Frozen evidence passed",
                    actor="owner",
                    created_at=now,
                    idempotency_key="approved-composite-preview",
                )
            with pytest.raises(ValueError, match="already has a promotion decision"):
                _ = record_qualification_promotion_decision(
                    connection,
                    baseline_target_id=baseline_id,
                    candidate_target_id=candidate_id,
                    evidence=selected,
                    artifact_path=artifact_path,
                    decision="approved",
                    reason="Frozen evidence passed",
                    actor="owner",
                    created_at=now,
                    idempotency_key="duplicate-composite-preview",
                )
            missing = preview_qualification_promotion(
                connection,
                baseline_id,
                candidate_id,
                selected.model_copy(update={"input_preparation_evidence_id": None}),
                artifact_path,
            )
            assert not missing.eligible
            assert "Changed input_preparation component has no evidence" in missing.failures
            synthetic = composition_evidence.model_copy(update={"origin": "synthetic"})
            synthetic_id = store_qualification_evidence(
                connection, synthetic, created_at=now, created_by="owner"
            )
            untrusted = preview_qualification_promotion(
                connection,
                baseline_id,
                candidate_id,
                selected.model_copy(update={"composition_evidence_id": synthetic_id}),
                artifact_path,
            )
            assert not untrusted.eligible
            assert "Composition evidence does not prove the candidate target" in untrusted.failures
            incomplete = composition_evidence.model_copy(
                update={
                    "result": result | {"coverage": {"direct": True}},
                    "completed_at": now + timedelta(seconds=1),
                }
            )
            incomplete_id = store_qualification_evidence(
                connection, incomplete, created_at=now, created_by="owner"
            )
            incomplete_preview = preview_qualification_promotion(
                connection,
                baseline_id,
                candidate_id,
                selected.model_copy(update={"composition_evidence_id": incomplete_id}),
                artifact_path,
            )
            assert not incomplete_preview.eligible
            assert (
                "Composition evidence lacks full production path coverage"
                in incomplete_preview.failures
            )
            missing_attempts = composition_evidence.model_copy(
                update={"completed_at": now + timedelta(seconds=2)}
            )
            missing_attempts_id = store_qualification_evidence(
                connection, missing_attempts, created_at=now, created_by="owner"
            )
            absent = preview_qualification_promotion(
                connection,
                baseline_id,
                candidate_id,
                selected.model_copy(update={"composition_evidence_id": missing_attempts_id}),
                artifact_path,
            )
            assert not absent.eligible
            assert "composition provider attempts are missing or differ" in absent.failures
            alternative_relevance = store_relevance_release(
                connection,
                build_relevance_release(
                    build_jev_faithful_policy(load_prompt_release(connection, release_id))
                ),
                created_at=now,
                created_by="owner",
            )
            changed_relevance = components[1].model_copy(
                update={"relevance_release_id": alternative_relevance.id}
            )
            _ = store_component_release(
                connection, changed_relevance, created_at=now, created_by="owner"
            )
            relevance_candidate = build_qualification_target(
                components[0], changed_relevance, components[2], components[3]
            )
            relevance_candidate_id = store_qualification_target(
                connection, relevance_candidate, created_at=now, created_by="owner"
            )
            incomparable = preview_qualification_promotion(
                connection, baseline_id, relevance_candidate_id, selected, artifact_path
            )
            assert not incomparable.eligible
            assert "Changed relevance requires a frozen comparison" in incomparable.failures
            with pytest.raises(ValueError, match="Ineligible qualification target"):
                _ = record_qualification_promotion_decision(
                    connection,
                    baseline_target_id=baseline_id,
                    candidate_target_id=relevance_candidate_id,
                    evidence=selected,
                    artifact_path=artifact_path,
                    decision="approved",
                    reason="Cannot pass",
                    actor="owner",
                    created_at=now,
                    idempotency_key="ineligible-composite-preview",
                )
            rejected = record_qualification_promotion_decision(
                connection,
                baseline_target_id=baseline_id,
                candidate_target_id=relevance_candidate_id,
                evidence=selected,
                artifact_path=artifact_path,
                decision="rejected",
                reason="Missing frozen comparison",
                actor="owner",
                created_at=now,
                idempotency_key="rejected-composite-preview",
            )
            assert rejected.decision == "rejected"
            with pytest.raises(
                QualificationActivationError, match="Approved qualification promotion"
            ):
                _ = activate_qualification_target(
                    connection,
                    activation.model_copy(
                        update={
                            "idempotency_key": "activate-rejected-composite-preview",
                            "promotion_decision_id": rejected.id,
                            "expected_target_id": candidate_id,
                            "expected_generation": 2,
                        }
                    ),
                    artifact_path,
                )
            forged_decision_id = "f" * 64
            _ = connection.execute(
                """
                INSERT INTO qualification_promotion_decisions (
                  id, idempotency_key, baseline_target_id, candidate_target_id,
                  composition_evidence_id, decision, reason, actor, created_at
                ) VALUES (%s, %s, %s, %s, %s, 'approved', %s, %s, %s)
                """,
                (
                    forged_decision_id,
                    "direct-sql-approved",
                    candidate_id,
                    relevance_candidate_id,
                    synthetic_id,
                    "Unverified evidence",
                    "owner",
                    now,
                ),
            )
            with pytest.raises(QualificationActivationError, match="evidence is incomplete"):
                _ = activate_qualification_target(
                    connection,
                    activation.model_copy(
                        update={
                            "idempotency_key": "activate-direct-sql-composite-preview",
                            "promotion_decision_id": forged_decision_id,
                            "expected_target_id": candidate_id,
                            "expected_generation": 2,
                        }
                    ),
                    artifact_path,
                )
            assert get_active_qualification_target(connection).target_id == candidate_id

            final_manifest_id, _, final_rates = _seed_evaluation_execution_context(connection, now)
            frozen = RelevanceExperimentInput(
                manifest_id=final_manifest_id,
                exchange_rates=final_rates,
                provider_settings=ProviderExperimentSettings(
                    provider="openrouter", temperature=0, retry_limit=0
                ),
                input_path="direct",
            )
            experiment_id = store_relevance_experiment_input(
                connection, frozen, created_at=now, created_by="owner"
            )
            final_manifest = load_manifest(connection, final_manifest_id)
            trials = tuple(
                score_trial(
                    "0" * 64, case, trial_index, Qualified(reason="matched", profile_name="profile")
                )
                for case in final_manifest.cases
                for trial_index in range(case.trial_count)
            )
            metrics = score_results(final_manifest, trials)
            comparison_relevance = store_relevance_release(
                connection,
                build_relevance_release(
                    build_gemini_policy(load_prompt_release(connection, release_id))
                ),
                created_at=now + timedelta(seconds=1),
                created_by="owner",
            )
            second_changed = components[1].model_copy(
                update={"relevance_release_id": comparison_relevance.id}
            )
            _ = store_component_release(
                connection,
                second_changed,
                created_at=now + timedelta(seconds=1),
                created_by="owner",
            )
            comparison_candidate = build_qualification_target(
                components[0], second_changed, components[2], components[3]
            )
            comparison_candidate_id = store_qualification_target(
                connection,
                comparison_candidate,
                created_at=now + timedelta(seconds=1),
                created_by="owner",
            )
            baseline_release_id = bind_qualification_prompt_release(
                connection, baseline_id, artifact_path, created_at=now, created_by="owner"
            )
            candidate_release_id = bind_qualification_prompt_release(
                connection,
                comparison_candidate_id,
                artifact_path,
                created_at=now + timedelta(seconds=1),
                created_by="owner",
            )

            def relevance_attempt_for(compiled_release_id: str) -> ModelCallAttempt:
                relevance_prompt = next(
                    version
                    for version in load_prompt_release(
                        connection, PromptReleaseId(compiled_release_id)
                    ).versions
                    if version.definition.phase == "filter"
                )
                return replace(
                    attempt,
                    id=uuid4(),
                    context=ModelCallContext(
                        processing_attempt_id=uuid4(),
                        pipeline_run_id=uuid4(),
                        prompt_release_id=PromptReleaseId(compiled_release_id),
                        operation_key="relevance",
                        input_digest=InputDigest("c" * 64),
                    ),
                    prompt_name=relevance_prompt.definition.name,
                    prompt_version_id=relevance_prompt.id,
                )

            baseline_attempt = relevance_attempt_for(baseline_release_id)
            candidate_attempt = relevance_attempt_for(candidate_release_id)

            def relevance_evidence(
                target: QualificationTargetId, attempt_item: ModelCallAttempt
            ) -> QualificationEvidence:
                component = components[1] if target == baseline_id else second_changed
                return QualificationEvidence(
                    target_id=target,
                    phase="relevance",
                    component_release_id=component_release_id(component),
                    experiment_input_id=experiment_id,
                    executor_artifact_id=artifact.id,
                    origin="canonical",
                    outcome="passed",
                    result={
                        "metrics": metrics.model_dump(mode="json"),
                        "trials": [trial.model_dump(mode="json") for trial in trials],
                    },
                    attempts=(provider_attempt_evidence(attempt_item),),
                    completed_at=now,
                )

            baseline_relevance = relevance_evidence(baseline_id, baseline_attempt)
            candidate_relevance = relevance_evidence(comparison_candidate_id, candidate_attempt)
            baseline_relevance_id = store_qualification_evidence(
                connection, baseline_relevance, created_at=now, created_by="owner"
            )
            candidate_relevance_id = store_qualification_evidence(
                connection, candidate_relevance, created_at=now, created_by="owner"
            )
            store_provider_attempts(
                connection,
                baseline_relevance,
                (baseline_attempt,),
                provider="openrouter",
                created_at=now,
                created_by="owner",
            )
            store_provider_attempts(
                connection,
                candidate_relevance,
                (candidate_attempt,),
                provider="openrouter",
                created_at=now,
                created_by="owner",
            )
            comparison_id = record_relevance_comparison(
                connection,
                baseline_relevance,
                candidate_relevance,
                created_at=now,
                created_by="owner",
            )
            assert baseline_relevance_id != candidate_relevance_id
            relevance_candidate_input_evidence = input_evidence.model_copy(
                update={
                    "target_id": comparison_candidate_id,
                    "component_release_id": component_release_id(components[0]),
                }
            )
            relevance_candidate_attempt = replace(attempt, id=uuid4())
            relevance_candidate_composition_evidence = composition_evidence.model_copy(
                update={
                    "target_id": comparison_candidate_id,
                    "attempts": (provider_attempt_evidence(relevance_candidate_attempt),),
                    "completed_at": now + timedelta(seconds=2),
                }
            )
            relevance_candidate_input_id = store_qualification_evidence(
                connection,
                relevance_candidate_input_evidence,
                created_at=now,
                created_by="owner",
            )
            relevance_candidate_composition_id = store_qualification_evidence(
                connection,
                relevance_candidate_composition_evidence,
                created_at=now,
                created_by="owner",
            )
            store_provider_attempts(
                connection,
                relevance_candidate_composition_evidence,
                (relevance_candidate_attempt,),
                provider="openrouter",
                created_at=now,
                created_by="owner",
            )
            with_comparison = preview_qualification_promotion(
                connection,
                baseline_id,
                comparison_candidate_id,
                PromotionEvidenceSelection(
                    input_preparation_evidence_id=QualificationEvidenceId(
                        relevance_candidate_input_id
                    ),
                    composition_evidence_id=QualificationEvidenceId(
                        relevance_candidate_composition_id
                    ),
                    relevance_evidence_id=QualificationEvidenceId(candidate_relevance_id),
                    relevance_comparison_id=comparison_id,
                ),
                artifact_path,
            )
            assert with_comparison.eligible, with_comparison.failures
            approved_comparison = record_qualification_promotion_decision(
                connection,
                baseline_target_id=baseline_id,
                candidate_target_id=comparison_candidate_id,
                evidence=PromotionEvidenceSelection(
                    input_preparation_evidence_id=QualificationEvidenceId(
                        relevance_candidate_input_id
                    ),
                    composition_evidence_id=QualificationEvidenceId(
                        relevance_candidate_composition_id
                    ),
                    relevance_evidence_id=QualificationEvidenceId(candidate_relevance_id),
                    relevance_comparison_id=comparison_id,
                ),
                artifact_path=artifact_path,
                decision="approved",
                reason="Frozen comparison passed",
                actor="owner",
                created_at=now,
                idempotency_key="approved-changed-relevance",
            )
            assert approved_comparison.decision == "approved"


def test_qualification_target_requires_four_components_from_one_artifact(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 25, tzinfo=UTC)
    with _connection(authority_schema) as connection:
        _ = apply_migrations(connection)
        artifact, components, target = _store_default_qualification_target(connection, now)
        input_preparation, relevance, enrichment, deduplication = components
        target_id = qualification_target_id(target)
        resolved = load_qualification_target(connection, target_id)
        assert resolved.content == target
        assert resolved.artifact == artifact
        assert (
            resolved.input_preparation,
            resolved.relevance,
            resolved.enrichment,
            resolved.deduplication,
        ) == components
        assert connection.execute(
            "SELECT count(*) FROM qualification_targets WHERE id = %s", (target_id,)
        ).fetchone() == (1,)
        assert (
            store_qualification_target(connection, target, created_at=now, created_by="owner")
            == target_id
        )

        wrong_kind = target.model_copy(
            update={"enrichment_release_id": target.deduplication_release_id}
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            _ = store_qualification_target(
                connection, wrong_kind, created_at=now, created_by="owner"
            )

        alternate_manifest = artifact.manifest.model_copy(
            update={"runtime": artifact.manifest.runtime + " alternate"}
        )
        alternate_artifact = ImplementationArtifact(
            id=implementation_artifact_id(alternate_manifest), manifest=alternate_manifest
        )
        _ = store_implementation_artifact(
            connection, alternate_artifact, created_at=now, created_by="build"
        )
        alternate_input = input_preparation.model_copy(
            update={"artifact_id": alternate_artifact.id}
        )
        alternate_input_id = store_component_release(
            connection, alternate_input, created_at=now, created_by="owner"
        )
        with pytest.raises(ValueError, match="one implementation artifact"):
            _ = build_qualification_target(alternate_input, relevance, enrichment, deduplication)
        mixed_artifacts = target.model_copy(
            update={"input_preparation_release_id": alternate_input_id}
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            _ = store_qualification_target(
                connection, mixed_artifacts, created_at=now, created_by="owner"
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            _ = connection.execute(
                "UPDATE qualification_component_releases SET created_by = 'changed'"
            )
