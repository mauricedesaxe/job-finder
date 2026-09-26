from __future__ import annotations

from typing import Literal

from contracts.test_postgres_authority import (
    AtsAvailable,
    AtsNotApplicable,
    CompletedEvaluationExecution,
    Decimal,
    EvaluateManifestCommand,
    EvaluationManifestCase,
    EvaluationMetrics,
    EvaluationResult,
    ExchangeRateSnapshot,
    FixtureCase,
    HttpResponse,
    INITIAL_SEARCH_CONFIGURATION_REVISION_ID,
    LangfuseProjection,
    Mapping,
    Path,
    PhaseFixtureSet,
    ProjectionDelivered,
    ProjectionFailed,
    ProjectionIdle,
    ProjectionLeaseLost,
    ProviderExperimentSettings,
    QualificationEvidence,
    Qualified,
    Rejected,
    ReleaseTarget,
    RelevanceExperimentInput,
    ReviewSaved,
    ReviewSubmission,
    UTC,
    UUID,
    _connection,
    _insert_accepted_model_call_attempt,
    _insert_prompt_run,
    _insert_review_decision,
    _seed_evaluation_execution_context,
    _store_default_qualification_target,
    apply_migrations,
    bind_qualification_prompt_release,
    bootstrap_prompt_release,
    build_jev_atomic_policy,
    build_jev_faithful_policy,
    build_relevance_release,
    datetime,
    deliver_next_projection,
    enqueue_model_call_projection,
    enqueue_qualified_review_item,
    execute_composition_fixture_set,
    execute_relevance_experiment,
    json,
    list_review_feedback,
    load_projection_queue_status,
    load_prompt_release,
    preview_run_comparison,
    pytest,
    qualification_target_id,
    rebuild_langfuse_projections,
    record_prompt_promotion_decision,
    record_review,
    run_manifest,
    store_fixture_set,
    store_relevance_experiment_input,
    store_relevance_release,
    timedelta,
    uuid4,
    write_implementation_artifact,
)

pytest_plugins = ("contracts.test_postgres_authority",)


def test_rebuilds_langfuse_projections_idempotently(authority_schema: str) -> None:
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    with _connection(authority_schema) as connection:
        manifest_id, target, rates = _seed_evaluation_execution_context(connection, now)
        prompt_release = load_prompt_release(connection, target.prompt_release_id)
        faithful_release = store_relevance_release(
            connection,
            build_relevance_release(build_jev_faithful_policy(prompt_release)),
            created_at=now,
            created_by="contract",
        )
        candidate_target = ReleaseTarget(
            prompt_release_id=target.prompt_release_id,
            relevance_release_id=faithful_release.id,
        )

        def evaluator(
            case: EvaluationManifestCase,
            _case_target: ReleaseTarget,
            _trial: int,
        ) -> EvaluationResult:
            if case.expected_outcome == "qualified":
                return Qualified(reason="Expected positive.", profile_name="profile")
            return Rejected(reason="Expected negative.")

        def execute(
            idempotency_key: str, run_target: ReleaseTarget, implementation_ref: str
        ) -> str:
            execution = run_manifest(
                connection,
                command=EvaluateManifestCommand(
                    manifest_id=manifest_id,
                    target=run_target,
                    implementation_ref=implementation_ref,
                    idempotency_key=idempotency_key,
                ),
                create_exchange_rates=lambda: rates,
                create_evaluator=lambda _rates, _record: evaluator,
                now=lambda: now,
            )
            assert isinstance(execution, CompletedEvaluationExecution)
            return execution.run.id

        baseline_run_id = execute("evaluation:baseline", target, "rebuild-baseline")
        candidate_run_id = execute("evaluation:candidate", candidate_target, "rebuild-candidate")
        comparison = preview_run_comparison(connection, baseline_run_id, candidate_run_id)
        promotion = record_prompt_promotion_decision(
            connection,
            baseline_run_id=baseline_run_id,
            candidate_run_id=candidate_run_id,
            expected_comparison_id=comparison.id,
            decision="rejected",
            reason="Owner declined promotion.",
            actor="owner",
            created_at=now,
            idempotency_key="promotion:rebuild",
        )
        assert promotion.decision == "rejected"
        _insert_accepted_model_call_attempt(connection, prompt_release, now)

        delivered = deliver_next_projection(
            connection,
            sender=lambda projection: {"remote_id": f"langfuse-{projection.idempotency_key}"},
            owner_token=uuid4(),
            now=now,
            lease_for=timedelta(minutes=1),
            retry_after=timedelta(minutes=5),
        )
        assert isinstance(delivered, ProjectionDelivered)

        first_counts = rebuild_langfuse_projections(connection)
        assert first_counts == {"model calls": 1, "manifests": 1, "runs": 2, "promotions": 1}
        rows_after_first = connection.execute(
            "SELECT id, state FROM langfuse_projection_items ORDER BY id"
        ).fetchall()

        second_counts = rebuild_langfuse_projections(connection)
        assert second_counts == first_counts
        assert (
            connection.execute(
                "SELECT id, state FROM langfuse_projection_items ORDER BY id"
            ).fetchall()
            == rows_after_first
        )
        assert connection.execute(
            """
            SELECT id, remote_id FROM langfuse_projection_items WHERE state = 'completed'
            """
        ).fetchall() == [(delivered.projection_id, f"langfuse-{delivered.projection_id}")]


def test_rebuilds_model_call_payloads_matching_live_enqueue(authority_schema: str) -> None:
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        release = bootstrap_prompt_release(connection)
        attempt = _insert_accepted_model_call_attempt(connection, release, now)
        enqueue_model_call_projection(connection, attempt)
        live_payload = connection.execute(
            """
            SELECT payload, payload_digest FROM langfuse_projection_items
            WHERE kind = 'model_call' AND source_id = %s
            """,
            (str(attempt.id),),
        ).fetchone()
        assert live_payload is not None
        connection.execute(
            "DELETE FROM langfuse_projection_items WHERE kind = 'model_call' AND source_id = %s",
            (str(attempt.id),),
        )

        counts = rebuild_langfuse_projections(connection)
        assert counts["model calls"] == 1
        rebuilt_payload = connection.execute(
            """
            SELECT payload, payload_digest FROM langfuse_projection_items
            WHERE kind = 'model_call' AND source_id = %s
            """,
            (str(attempt.id),),
        ).fetchone()
        assert rebuilt_payload == live_payload


def test_reclaims_expired_projection_leases_and_reports_loss(authority_schema: str) -> None:
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    with _connection(authority_schema) as connection:
        _seed_evaluation_execution_context(connection, now)
        stealing_owner = uuid4()

        def stealing_sender(projection: LangfuseProjection) -> object:
            connection.execute(
                """
                UPDATE langfuse_projection_items
                SET owner_token = %s, lease_expires_at = %s
                WHERE id = %s
                """,
                (stealing_owner, now + timedelta(minutes=5), projection.id),
            )
            return {"remote_id": "stolen-delivery"}

        lost = deliver_next_projection(
            connection,
            sender=stealing_sender,
            owner_token=uuid4(),
            now=now,
            lease_for=timedelta(minutes=1),
            retry_after=timedelta(minutes=5),
        )
        assert isinstance(lost, ProjectionLeaseLost)
        assert connection.execute(
            """
            SELECT state, owner_token, attempt_count, remote_id, completed_at
            FROM langfuse_projection_items WHERE id = %s
            """,
            (lost.projection_id,),
        ).fetchone() == ("leased", stealing_owner, 1, None, None)

        connection.execute(
            "UPDATE langfuse_projection_items SET lease_expires_at = %s WHERE id = %s",
            (now - timedelta(seconds=1), lost.projection_id),
        )
        reclaimed = deliver_next_projection(
            connection,
            sender=lambda projection: {"remote_id": f"langfuse-{projection.idempotency_key}"},
            owner_token=uuid4(),
            now=now,
            lease_for=timedelta(minutes=1),
            retry_after=timedelta(minutes=5),
        )
        assert isinstance(reclaimed, ProjectionDelivered)
        assert reclaimed.projection_id == lost.projection_id
        assert connection.execute(
            """
            SELECT state, attempt_count, remote_id FROM langfuse_projection_items
            WHERE id = %s
            """,
            (lost.projection_id,),
        ).fetchone() == (
            "completed",
            2,
            f"langfuse-{lost.projection_id}",
        )


def test_records_invalid_projection_responses_and_reports_idle_queue(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    with _connection(authority_schema) as connection:
        _seed_evaluation_execution_context(connection, now)

        def empty_sender(_projection: LangfuseProjection) -> object:
            return {}

        failed = deliver_next_projection(
            connection,
            sender=empty_sender,
            owner_token=uuid4(),
            now=now,
            lease_for=timedelta(minutes=1),
            retry_after=timedelta(hours=1),
        )
        assert isinstance(failed, ProjectionFailed)
        assert failed.error_code == "invalid_response"
        status = load_projection_queue_status(connection)
        assert status.pending_count == 0
        assert status.failed_count == 1
        assert [summary.error_code for summary in status.failures] == ["invalid_response"]
        reason = connection.execute(
            "SELECT last_error ->> 'reason' FROM langfuse_projection_items WHERE id = %s",
            (failed.projection_id,),
        ).fetchone()
        assert reason is not None
        assert "remote_id" in str(reason[0])
        assert "Field required" in str(reason[0])

        idle = deliver_next_projection(
            connection,
            sender=empty_sender,
            owner_token=uuid4(),
            now=now,
            lease_for=timedelta(minutes=1),
            retry_after=timedelta(hours=1),
        )
        assert isinstance(idle, ProjectionIdle)


def test_pages_review_feedback_across_the_default_limit(authority_schema: str) -> None:
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    run_id = uuid4()
    feedback_count = 55
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        release = bootstrap_prompt_release(connection)
        _insert_prompt_run(connection, run_id, release.id, now)
        moments: list[datetime] = []
        for index in range(feedback_count):
            evaluation_id = _insert_review_decision(
                connection, run_id, release.id, now, 400 + index, "qualified"
            )
            assert enqueue_qualified_review_item(connection, evaluation_id, now.date())
            review_item = connection.execute(
                "SELECT id FROM review_items WHERE evaluation_id = %s", (evaluation_id,)
            ).fetchone()
            assert review_item is not None
            moment = now + timedelta(seconds=index)
            feedback = record_review(
                connection,
                ReviewSubmission(
                    review_item_id=UUID(str(review_item[0])),
                    evaluation_id=evaluation_id,
                    snapshot_id=f"{500 + index:064x}",
                    decision="pursue",
                    target_profile="applied-ai-product-engineer",
                    primary_reason="technology-fit",
                    actor="owner",
                    created_at=moment,
                ),
            )
            assert isinstance(feedback, ReviewSaved)
            moments.append(moment)
        newest_first = sorted(moments, reverse=True)

        first_page = list_review_feedback(connection)
        assert len(first_page.items) == 50
        assert first_page.next_offset == 50
        assert [item.created_at for item in first_page.items] == newest_first[:50]

        second_page = list_review_feedback(connection, offset=50)
        assert len(second_page.items) == feedback_count - 50
        assert second_page.next_offset is None
        assert [item.created_at for item in second_page.items] == newest_first[50:]


def test_direct_relevance_experiment_records_a_failing_threshold_outcome(
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
                        "id": "generation-failed",
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
                                                    {"pass": False, "reason": "not a match"}
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
            assert evidence.outcome == "failed"
            metrics = EvaluationMetrics.model_validate(evidence.result["metrics"])
            assert metrics.false_negative_rate > 0
        finally:
            artifact_path.unlink(missing_ok=True)


def test_composition_fixture_guards_reject_tampered_fixture_sets(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 25, tzinfo=UTC)
    artifact_path = Path(__file__).resolve().parents[1] / "implementation-artifact.json"
    rates = ExchangeRateSnapshot(rates={"EUR": Decimal("1.10")}, source="fallback", observed_at=now)
    openrouter = ProviderExperimentSettings(provider="openrouter", temperature=0, retry_limit=0)
    typesafe = ProviderExperimentSettings(provider="typesafe", temperature=0, retry_limit=0)

    def fixture_case(
        *, raw_url: str, input_path: Literal["direct", "ats"], ats: AtsAvailable | AtsNotApplicable
    ) -> FixtureCase:
        return FixtureCase(
            input={
                "raw_url": raw_url,
                "keyword": "software engineer",
                "domain": "jobs.lever.co",
                "scrape": {"kind": "succeeded", "markdown": "# Engineer\nBuild tools. " * 20},
                "ats_evidence": ats.model_dump(mode="json"),
                "configuration_revision_id": INITIAL_SEARCH_CONFIGURATION_REVISION_ID,
                "exchange_rates": rates.model_dump(mode="json"),
                "openrouter_settings": openrouter.model_dump(mode="json"),
                "relevance_settings": typesafe.model_dump(mode="json"),
                "observed_at": now.isoformat(),
            },
            expected={
                "decision_outcome": "rejected",
                "decision_stage": "structural",
                "work_state": "completed",
                "review_enqueued": False,
            },
            input_path=input_path,
        )

    shared_url = "https://jobs.lever.co/acme/duplicate-role"
    duplicated = PhaseFixtureSet(
        phase="composition",
        cases=(
            fixture_case(raw_url=shared_url, input_path="direct", ats=AtsNotApplicable()),
            fixture_case(raw_url=shared_url, input_path="direct", ats=AtsNotApplicable()),
        ),
    )
    mismatched = PhaseFixtureSet(
        phase="composition",
        cases=(
            fixture_case(
                raw_url="https://jobs.lever.co/acme/ats-role",
                input_path="direct",
                ats=AtsAvailable(
                    source="ashby",
                    location="London",
                    locations=("London",),
                    workplace_type="OnSite",
                    country="GB",
                ),
            ),
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
        try:
            assert write_implementation_artifact(artifact_path.parent, artifact_path) == artifact
            _ = bind_qualification_prompt_release(
                connection, target_id, artifact_path, created_at=now, created_by="owner"
            )
            for fixture, message in (
                (duplicated, "Composition fixture URLs must be distinct"),
                (mismatched, "Composition input path differs from ATS evidence"),
            ):
                fixture_id = store_fixture_set(
                    connection, fixture, created_at=now, created_by="owner"
                )
                with pytest.raises(ValueError, match=message):
                    _ = execute_composition_fixture_set(
                        connection,
                        target_id,
                        fixture_id,
                        artifact_path,
                        openrouter_api_key="unused",
                        typesafe_api_key="unused",
                        completed_at=now,
                        created_by="owner",
                        model_sender=lambda *_args: pytest.fail(  # pyright: ignore[reportUnknownLambdaType]
                            "A rejected fixture set must not call a model"
                        ),
                        jev_sender=lambda *_args: pytest.fail(  # pyright: ignore[reportUnknownLambdaType]
                            "A rejected fixture set must not call JEV"
                        ),
                    )
                assert connection.execute(
                    "SELECT count(*) FROM qualification_phase_evidence WHERE target_id = %s",
                    (target_id,),
                ).fetchone() == (0,)
        finally:
            artifact_path.unlink(missing_ok=True)
