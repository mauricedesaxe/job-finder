from __future__ import annotations

from contracts.test_postgres_authority import (
    CompletedEvaluationExecution,
    Decimal,
    EvaluateManifestCommand,
    EvaluationManifestCase,
    EvaluationResult,
    ExchangeRateSnapshot,
    Jsonb,
    LangfuseProjection,
    LangfuseUnavailable,
    ManifestPolicy,
    ProjectionDelivered,
    ProjectionFailed,
    Qualified,
    Rejected,
    ReleaseTarget,
    RetryableOperationalError,
    ReviewSaved,
    ReviewSubmission,
    UTC,
    _apply_migrations_through,
    _connection,
    _insert_prompt_run,
    _insert_review_decision,
    _seed_evaluation_execution_context,
    apply_migrations,
    bootstrap_prompt_release,
    build_gemini_policy,
    build_jev_faithful_policy,
    build_relevance_release,
    create_manifest,
    datetime,
    deliver_next_projection,
    enqueue_qualified_review_item,
    enqueue_rejected_audit_sample,
    exchange_rate_snapshot_digest,
    include_review_event,
    load_evaluation_execution_by_key,
    load_review_queue,
    load_run,
    preview_run_comparison,
    psycopg,
    pytest,
    record_prompt_promotion_decision,
    record_review,
    run_manifest,
    store_relevance_release,
    timedelta,
    uuid4,
)

pytest_plugins = ("contracts.test_postgres_authority",)


def test_evaluation_execution_sql_enforces_insert_linkage_and_json_shapes(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    with _connection(authority_schema) as connection:
        manifest_id, target, rates = _seed_evaluation_execution_context(connection, now)
        command = EvaluateManifestCommand(
            idempotency_key="evaluation:link-source",
            manifest_id=manifest_id,
            target=target,
            implementation_ref="link-source-ref",
        )
        completed = run_manifest(
            connection,
            command=command,
            create_exchange_rates=lambda: rates,
            create_evaluator=lambda _rates, _record: (
                lambda _case, _target, _trial: Qualified(
                    reason="Expected positive.", profile_name="profile"
                )
            ),
            now=lambda: now,
        )
        assert isinstance(completed, CompletedEvaluationExecution)
        unlinked_run_id = "9" * 64
        with connection.transaction():
            connection.execute(
                """
                INSERT INTO evaluation_runs (
                  id, idempotency_key, manifest_id, prompt_release_id,
                  relevance_release_id, expected_result_count, result_count,
                  false_positive_count, false_negative_count,
                  operational_failure_count, critical_false_positive_count,
                  false_positive_rate, false_negative_rate, implementation_ref,
                  completed_at
                )
                SELECT %s, 'evaluation:unlinked-run', manifest_id, prompt_release_id,
                       relevance_release_id, expected_result_count, result_count,
                       false_positive_count, false_negative_count,
                       operational_failure_count, critical_false_positive_count,
                       false_positive_rate, false_negative_rate, 'unlinked-ref', completed_at
                FROM evaluation_runs WHERE id = %s
                """,
                (unlinked_run_id, completed.run.id),
            )
            connection.execute(
                """
                INSERT INTO evaluation_case_results (
                  id, run_id, manifest_id, prompt_release_id,
                  relevance_release_id, case_position, trial_index,
                  expected_outcome, actual_outcome, failure_kind, reason
                )
                SELECT encode(sha256(convert_to(%s || ':' || id, 'UTF8')), 'hex'),
                       %s, manifest_id, prompt_release_id, relevance_release_id,
                       case_position, trial_index, expected_outcome, actual_outcome,
                       failure_kind, reason
                FROM evaluation_case_results WHERE run_id = %s
                """,
                (unlinked_run_id, unlinked_run_id, completed.run.id),
            )

        running_values = (
            "e" * 64,
            "evaluation:forged-link",
            manifest_id,
            target.prompt_release_id,
            target.relevance_release_id,
            "forged-ref",
            Jsonb(rates.model_dump(mode="json")),
            exchange_rate_snapshot_digest(rates),
            now,
        )
        connection.execute(
            """
            INSERT INTO evaluation_run_executions (
              id, idempotency_key, manifest_id, prompt_release_id,
              relevance_release_id, implementation_ref, state,
              exchange_rate_snapshot, exchange_rate_digest, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, 'running', %s, %s, %s)
            """,
            running_values,
        )
        with pytest.raises(
            psycopg.errors.ForeignKeyViolation,
            match="evaluation_execution_exact_completed_run",
        ):
            connection.execute(
                """
                UPDATE evaluation_run_executions
                SET state = 'completed', request_count = 0, input_tokens = 0,
                    output_tokens = 0, cost_usd = 0, usage_complete = TRUE,
                    run_id = %s, terminal_at = %s
                WHERE id = %s
                """,
                (unlinked_run_id, now, running_values[0]),
            )

        for index, state in enumerate(("completed", "failed")):
            with pytest.raises(
                psycopg.errors.IntegrityConstraintViolation,
                match="must start running",
            ):
                connection.execute(
                    """
                    INSERT INTO evaluation_run_executions (
                      id, idempotency_key, manifest_id, prompt_release_id,
                      relevance_release_id, implementation_ref, state, created_at
                    ) VALUES (%s, %s, %s, %s, %s, 'direct-ref', %s, %s)
                    """,
                    (
                        f"{index + 30:064x}",
                        f"evaluation:direct-{state}",
                        manifest_id,
                        target.prompt_release_id,
                        target.relevance_release_id,
                        state,
                        now,
                    ),
                )

        invalid_snapshots: tuple[dict[str, object], ...] = (
            {"rates": [], "source": "fallback", "observed_at": now.isoformat()},
            {"rates": {"EUR": True}, "source": "fallback", "observed_at": now.isoformat()},
            {"rates": {"EUR": "1.1"}, "source": "other", "observed_at": now.isoformat()},
            {"rates": {"EUR": "1.1"}, "source": "fallback", "observed_at": "nope"},
            {
                "rates": {"EUR": "1.1"},
                "source": "fallback",
                "observed_at": now.isoformat(),
                "extra": True,
            },
        )
        for index, snapshot in enumerate(invalid_snapshots):
            with pytest.raises(
                psycopg.errors.CheckViolation,
                match="evaluation_execution_rate_snapshot_shape",
            ):
                connection.execute(
                    """
                    INSERT INTO evaluation_run_executions (
                      id, idempotency_key, manifest_id, prompt_release_id,
                      relevance_release_id, implementation_ref, state,
                      exchange_rate_snapshot, exchange_rate_digest, created_at
                    ) VALUES (%s, %s, %s, %s, %s, 'shape-test', 'running', %s,
                      encode(sha256(convert_to(canonical_job_finder_json(%s), 'UTF8')), 'hex'), %s)
                    """,
                    (
                        f"{index + 1:064x}",
                        f"evaluation:invalid-shape:{index}",
                        manifest_id,
                        target.prompt_release_id,
                        target.relevance_release_id,
                        Jsonb(snapshot),
                        Jsonb(snapshot),
                        now,
                    ),
                )

        invalid_failures: tuple[dict[str, object], ...] = (
            {},
            {"code": "", "message": "message"},
            {"code": "code", "message": ""},
            {"code": "code", "message": "message", "error_type": 1},
            {"code": "code", "message": "message", "extra": True},
        )
        for index, failure in enumerate(invalid_failures):
            execution_id = f"{index + 10:064x}"
            idempotency_key = f"evaluation:invalid-failure:{index}"
            connection.execute(
                """
                INSERT INTO evaluation_run_executions (
                  id, idempotency_key, manifest_id, prompt_release_id,
                  relevance_release_id, implementation_ref, state,
                  exchange_rate_snapshot, exchange_rate_digest, created_at
                ) VALUES (%s, %s, %s, %s, %s, 'failure-test', 'running', %s, %s, %s)
                """,
                (
                    execution_id,
                    idempotency_key,
                    manifest_id,
                    target.prompt_release_id,
                    target.relevance_release_id,
                    Jsonb(rates.model_dump(mode="json")),
                    exchange_rate_snapshot_digest(rates),
                    now,
                ),
            )
            with pytest.raises(
                psycopg.errors.CheckViolation,
                match="evaluation_execution_failure_shape",
            ):
                connection.execute(
                    """
                    UPDATE evaluation_run_executions
                    SET state = 'failed', request_count = 0, input_tokens = 0,
                        output_tokens = 0, cost_usd = 0, usage_complete = TRUE,
                        failure = %s, terminal_at = %s
                    WHERE id = %s
                    """,
                    (Jsonb(failure), now, execution_id),
                )


def test_exchange_rate_snapshot_digest_matches_postgres_for_unicode_keys(
    authority_schema: str,
) -> None:
    snapshot = ExchangeRateSnapshot(
        rates={"EURO-€": Decimal("1.10")},
        source="fallback",
        observed_at=datetime(2026, 9, 21, 12, 0, tzinfo=UTC),
    )
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        row = connection.execute(
            """
            SELECT encode(
              sha256(convert_to(canonical_job_finder_json(%s), 'UTF8')),
              'hex'
            )
            """,
            (Jsonb(snapshot.model_dump(mode="json")),),
        ).fetchone()
    assert row == (exchange_rate_snapshot_digest(snapshot),)


def test_runs_trials_rejects_operational_failures_and_retries_projection(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    run_id = uuid4()
    with _connection(authority_schema) as connection:
        _apply_migrations_through(connection, "0021_typesafe_model_provider.sql")
        baseline_release = bootstrap_prompt_release(connection)
        _insert_prompt_run(connection, run_id, baseline_release.id, now)
        qualified_decision = _insert_review_decision(
            connection, run_id, baseline_release.id, now, 31, "qualified"
        )
        _insert_review_decision(connection, run_id, baseline_release.id, now, 32, "rejected")
        assert enqueue_qualified_review_item(connection, qualified_decision, now.date())
        assert enqueue_rejected_audit_sample(connection, now.date()) == 1
        qualified, rejected = load_review_queue(connection).items[:2]
        negative_feedback = record_review(
            connection,
            ReviewSubmission(
                review_item_id=qualified.id,
                evaluation_id=qualified.evaluation_id,
                snapshot_id=qualified.snapshot_id,
                decision="reject",
                target_profile="neither",
                primary_reason="role-scope",
                actor="owner",
                created_at=now,
            ),
        )
        positive_feedback = record_review(
            connection,
            ReviewSubmission(
                review_item_id=rejected.id,
                evaluation_id=rejected.evaluation_id,
                snapshot_id=rejected.snapshot_id,
                decision="pursue",
                target_profile="applied-ai-product-engineer",
                primary_reason="technology-fit",
                actor="owner",
                created_at=now,
            ),
        )
        assert isinstance(negative_feedback, ReviewSaved)
        assert isinstance(positive_feedback, ReviewSaved)
        include_review_event(
            connection,
            review_event_id=negative_feedback.review_event_id,
            critical=True,
            reason="Critical negative control.",
            actor="owner",
            created_at=now,
            idempotency_key="run:negative",
        )
        include_review_event(
            connection,
            review_event_id=positive_feedback.review_event_id,
            critical=False,
            reason="Positive control.",
            actor="owner",
            created_at=now,
            idempotency_key="run:positive",
        )
        manifest = create_manifest(
            connection,
            policy=ManifestPolicy(),
            created_at=now,
            created_by="owner",
            idempotency_key="manifest:run",
        )
        historical_run_id = "f" * 64
        historical_result_count = sum(case.trial_count for case in manifest.cases)
        with connection.transaction():
            connection.execute(
                """
                INSERT INTO evaluation_runs (
                  id, idempotency_key, manifest_id, prompt_release_id,
                  expected_result_count, result_count, false_positive_count,
                  false_negative_count, operational_failure_count,
                  critical_false_positive_count, false_positive_rate,
                  false_negative_rate, implementation_ref, completed_at
                ) VALUES (%s, 'evaluation:historical', %s, %s, %s, %s, 0, 0, 0, 0, 0, 0,
                          'historical-ref', %s)
                """,
                (
                    historical_run_id,
                    manifest.id,
                    baseline_release.id,
                    historical_result_count,
                    historical_result_count,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO evaluation_case_results (
                  id, run_id, manifest_id, prompt_release_id, case_position,
                  trial_index, expected_outcome, actual_outcome, failure_kind, reason
                )
                SELECT encode(sha256(convert_to(
                         %s::TEXT || ':' || c.position || ':' || trial.index, 'UTF8'
                       )), 'hex'),
                       %s, c.manifest_id, %s, c.position, trial.index,
                       c.expected_outcome, c.expected_outcome, NULL, 'Historical result.'
                FROM evaluation_manifest_cases c
                CROSS JOIN LATERAL generate_series(0, c.trial_count - 1) AS trial(index)
                WHERE c.manifest_id = %s
                """,
                (
                    historical_run_id,
                    historical_run_id,
                    baseline_release.id,
                    manifest.id,
                ),
            )

        apply_migrations(connection)
        historical = load_run(connection, historical_run_id)
        assert historical.target is None
        historical_execution = load_evaluation_execution_by_key(connection, "evaluation:historical")
        assert isinstance(historical_execution, CompletedEvaluationExecution)
        assert historical_execution.state == "completed"
        assert historical_execution.exchange_rates is None
        assert historical_execution.telemetry is None
        assert historical_execution.run == historical
        assert connection.execute(
            "SELECT bool_and(relevance_release_id IS NULL) FROM evaluation_case_results WHERE run_id = %s",
            (historical_run_id,),
        ).fetchone() == (True,)
        with pytest.raises(
            psycopg.errors.CheckViolation,
            match="evaluation_runs_require_relevance_release",
        ):
            connection.execute(
                """
                INSERT INTO evaluation_runs (
                  id, idempotency_key, manifest_id, prompt_release_id,
                  relevance_release_id, expected_result_count, result_count,
                  false_positive_count, false_negative_count,
                  operational_failure_count, critical_false_positive_count,
                  false_positive_rate, false_negative_rate, implementation_ref,
                  completed_at
                )
                SELECT %s, 'evaluation:new-null', manifest_id, prompt_release_id,
                       NULL, expected_result_count, result_count,
                       false_positive_count, false_negative_count,
                       operational_failure_count, critical_false_positive_count,
                       false_positive_rate, false_negative_rate, implementation_ref,
                       completed_at
                FROM evaluation_runs WHERE id = %s
                """,
                ("0" * 64, historical_run_id),
            )
        with pytest.raises(
            psycopg.errors.CheckViolation,
            match="evaluation_case_results_require_relevance_release",
        ):
            connection.execute(
                """
                INSERT INTO evaluation_case_results (
                  id, run_id, manifest_id, prompt_release_id,
                  relevance_release_id, case_position, trial_index,
                  expected_outcome, actual_outcome, failure_kind, reason
                )
                SELECT %s, run_id, manifest_id, prompt_release_id, NULL,
                       case_position, trial_index + 100, expected_outcome,
                       actual_outcome, failure_kind, reason
                FROM evaluation_case_results WHERE run_id = %s LIMIT 1
                """,
                ("1" * 64, historical_run_id),
            )

        relevance_release = store_relevance_release(
            connection,
            build_relevance_release(build_gemini_policy(baseline_release)),
            created_at=now,
            created_by="contract",
        )
        faithful_release = store_relevance_release(
            connection,
            build_relevance_release(build_jev_faithful_policy(baseline_release)),
            created_at=now,
            created_by="contract",
        )
        baseline_target = ReleaseTarget(
            prompt_release_id=baseline_release.id,
            relevance_release_id=relevance_release.id,
        )
        candidate_target = ReleaseTarget(
            prompt_release_id=baseline_release.id,
            relevance_release_id=faithful_release.id,
        )
        baseline_calls = 0

        def baseline_evaluator(
            case: EvaluationManifestCase, target: ReleaseTarget, trial: int
        ) -> EvaluationResult:
            nonlocal baseline_calls
            baseline_calls += 1
            assert target == baseline_target
            assert trial >= 0
            if case.expected_outcome == "qualified":
                return Qualified(reason="Expected positive.", profile_name="profile")
            return Rejected(reason="Expected negative.")

        rates = ExchangeRateSnapshot(
            rates={"EUR": Decimal("1.10")}, source="fallback", observed_at=now
        )
        baseline_execution = run_manifest(
            connection,
            command=EvaluateManifestCommand(
                manifest_id=manifest.id,
                target=baseline_target,
                implementation_ref="baseline-ref",
                idempotency_key="evaluation:baseline",
            ),
            create_exchange_rates=lambda: rates,
            create_evaluator=lambda _rates, _record: baseline_evaluator,
            now=lambda: now,
        )
        repeated = run_manifest(
            connection,
            command=EvaluateManifestCommand(
                manifest_id=manifest.id,
                target=baseline_target,
                implementation_ref="baseline-ref",
                idempotency_key="evaluation:baseline",
            ),
            create_exchange_rates=lambda: rates,
            create_evaluator=lambda _rates, _record: baseline_evaluator,
            now=lambda: now,
        )
        assert isinstance(baseline_execution, CompletedEvaluationExecution)
        baseline = baseline_execution.run
        assert repeated == baseline_execution
        assert baseline.target == baseline_target
        assert baseline_calls == 4
        assert connection.execute(
            """
            SELECT r.relevance_release_id,
                   bool_and(c.relevance_release_id = r.relevance_release_id)
            FROM evaluation_runs r
            JOIN evaluation_case_results c ON c.run_id = r.id
            WHERE r.id = %s
            GROUP BY r.relevance_release_id
            """,
            (baseline.id,),
        ).fetchone() == (relevance_release.id, True)
        with pytest.raises(
            psycopg.errors.ForeignKeyViolation,
            match="evaluation_case_results_exact_release_target",
        ):
            connection.execute(
                """
                INSERT INTO evaluation_case_results (
                  id, run_id, manifest_id, prompt_release_id,
                  relevance_release_id, case_position, trial_index,
                  expected_outcome, actual_outcome, failure_kind, reason
                )
                SELECT %s, run_id, manifest_id, prompt_release_id, %s,
                       case_position, trial_index + 100, expected_outcome,
                       actual_outcome, failure_kind, reason
                FROM evaluation_case_results WHERE run_id = %s LIMIT 1
                """,
                ("2" * 64, faithful_release.id, baseline.id),
            )

        with pytest.raises(ValueError, match="different evaluation execution"):
            run_manifest(
                connection,
                command=EvaluateManifestCommand(
                    manifest_id=manifest.id,
                    target=ReleaseTarget(
                        prompt_release_id=baseline_release.id,
                        relevance_release_id=faithful_release.id,
                    ),
                    implementation_ref="baseline-ref",
                    idempotency_key="evaluation:baseline",
                ),
                create_exchange_rates=lambda: rates,
                create_evaluator=lambda _rates, _record: baseline_evaluator,
                now=lambda: now,
            )

        def candidate_evaluator(
            case: EvaluationManifestCase, target: ReleaseTarget, trial: int
        ) -> EvaluationResult:
            assert target == candidate_target
            if case.expected_outcome == "qualified":
                return RetryableOperationalError(
                    prompt_name="profile",
                    error_code="timeout",
                    reason="Provider timed out.",
                )
            if trial == 0:
                return Qualified(reason="Incorrect pass.", profile_name="profile")
            return Rejected(reason="Expected negative.")

        candidate_execution = run_manifest(
            connection,
            command=EvaluateManifestCommand(
                manifest_id=manifest.id,
                target=candidate_target,
                implementation_ref="candidate-ref",
                idempotency_key="evaluation:candidate",
            ),
            create_exchange_rates=lambda: rates,
            create_evaluator=lambda _rates, _record: candidate_evaluator,
            now=lambda: now,
        )
        assert isinstance(candidate_execution, CompletedEvaluationExecution)
        candidate = candidate_execution.run
        comparison = preview_run_comparison(connection, baseline.id, candidate.id)
        promotion = record_prompt_promotion_decision(
            connection,
            baseline_run_id=baseline.id,
            candidate_run_id=candidate.id,
            expected_comparison_id=comparison.id,
            decision="rejected",
            reason="Operational and critical regressions require rejection.",
            actor="owner",
            created_at=now,
            idempotency_key="promotion:candidate",
        )

        assert candidate.metrics.false_positive_count == 1
        assert candidate.metrics.false_negative_count == 0
        assert candidate.metrics.operational_failure_count == 1
        assert candidate.metrics.critical_false_positive_count == 1
        assert not comparison.eligible
        assert comparison.regression_count == 2
        assert promotion.decision == "rejected"
        assert promotion.baseline_target == baseline_target
        assert promotion.candidate_target == candidate_target
        assert promotion.comparison_id == comparison.id
        assert (
            record_prompt_promotion_decision(
                connection,
                baseline_run_id=baseline.id,
                candidate_run_id=candidate.id,
                expected_comparison_id=comparison.id,
                decision="rejected",
                reason="Operational and critical regressions require rejection.",
                actor="owner",
                created_at=now,
                idempotency_key="promotion:candidate",
            )
            == promotion
        )
        with pytest.raises(ValueError, match="stale"):
            record_prompt_promotion_decision(
                connection,
                baseline_run_id=baseline.id,
                candidate_run_id=candidate.id,
                expected_comparison_id="0" * 64,
                decision="rejected",
                reason="Different evidence.",
                actor="owner",
                created_at=now,
                idempotency_key="promotion:stale",
            )
        with pytest.raises(ValueError, match="cannot be approved"):
            record_prompt_promotion_decision(
                connection,
                baseline_run_id=baseline.id,
                candidate_run_id=candidate.id,
                expected_comparison_id=comparison.id,
                decision="approved",
                reason="Approve anyway.",
                actor="owner",
                created_at=now,
                idempotency_key="promotion:invalid-approval",
            )
        authoritative_counts = connection.execute(
            """
            SELECT (SELECT count(*) FROM evaluation_manifests),
                   (SELECT count(*) FROM evaluation_runs),
                   (SELECT count(*) FROM prompt_promotion_decisions)
            """
        ).fetchone()

        attempted_ids: list[str] = []

        def unavailable_sender(projection: LangfuseProjection) -> object:
            attempted_ids.append(projection.idempotency_key)
            raise LangfuseUnavailable("Langfuse is down")

        failed = deliver_next_projection(
            connection,
            sender=unavailable_sender,
            owner_token=uuid4(),
            now=now,
            lease_for=timedelta(minutes=1),
            retry_after=timedelta(minutes=5),
        )
        assert isinstance(failed, ProjectionFailed)
        assert (
            connection.execute(
                """
            SELECT (SELECT count(*) FROM evaluation_manifests),
                   (SELECT count(*) FROM evaluation_runs),
                   (SELECT count(*) FROM prompt_promotion_decisions)
            """
            ).fetchone()
            == authoritative_counts
        )

        delivered = deliver_next_projection(
            connection,
            sender=lambda projection: {"remote_id": f"langfuse-{projection.idempotency_key}"},
            owner_token=uuid4(),
            now=now + timedelta(minutes=5),
            lease_for=timedelta(minutes=1),
            retry_after=timedelta(minutes=5),
        )
        assert isinstance(delivered, ProjectionDelivered)
        assert delivered.projection_id == attempted_ids[0]
        assert connection.execute(
            "SELECT attempt_count FROM langfuse_projection_items WHERE id = %s",
            (delivered.projection_id,),
        ).fetchone() == (2,)
