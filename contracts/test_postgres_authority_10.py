from __future__ import annotations

from contracts.test_postgres_authority import (
    Barrier,
    Callable,
    CompletedEvaluationExecution,
    Decimal,
    EvaluateManifestCommand,
    EvaluationManifestCase,
    EvaluationResult,
    Event,
    ExchangeRateSnapshot,
    FailedEvaluationExecution,
    Jsonb,
    ManifestPolicy,
    ProviderRequestObservation,
    Qualified,
    Rejected,
    ReleaseTarget,
    ReviewSaved,
    ReviewSubmission,
    ThreadPoolExecutor,
    UTC,
    UUID,
    _connection,
    _insert_prompt_run,
    _insert_review_decision,
    _seed_evaluation_execution_context,
    apply_migrations,
    bootstrap_prompt_release,
    create_manifest,
    datetime,
    enqueue_qualified_review_item,
    enqueue_rejected_audit_sample,
    exchange_rate_snapshot_digest,
    exclude_review_event,
    hashlib,
    include_review_event,
    list_manifests,
    list_review_feedback,
    load_projection_queue_status,
    load_review_feedback,
    load_review_queue,
    preview_manifest,
    psycopg,
    pytest,
    record_review,
    run_manifest,
    time,
    timedelta,
    uuid4,
)

pytest_plugins = ("contracts.test_postgres_authority",)


def test_curates_immutable_feedback_into_a_repeated_trial_manifest(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    run_id = uuid4()
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        release = bootstrap_prompt_release(connection)
        _insert_prompt_run(connection, run_id, release.id, now)
        qualified_decision = _insert_review_decision(
            connection, run_id, release.id, now, 21, "qualified"
        )
        _insert_review_decision(connection, run_id, release.id, now, 22, "rejected")
        assert enqueue_qualified_review_item(connection, qualified_decision, now.date())
        assert enqueue_rejected_audit_sample(connection, now.date()) == 1
        qualified, rejected = load_review_queue(connection).items[:2]
        rejected_feedback = record_review(
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
        qualified_feedback = record_review(
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
        assert isinstance(rejected_feedback, ReviewSaved)
        assert isinstance(qualified_feedback, ReviewSaved)
        uncurated = list_review_feedback(connection, curation="uncurated")
        assert {item.review_event_id for item in uncurated.items} == {
            rejected_feedback.review_event_id,
            qualified_feedback.review_event_id,
        }

        curation_start = Barrier(2)

        def include_negative(_attempt: int) -> UUID:
            with _connection(authority_schema) as concurrent_connection:
                _ = curation_start.wait()
                return include_review_event(
                    concurrent_connection,
                    review_event_id=rejected_feedback.review_event_id,
                    critical=True,
                    reason="False positives are costly.",
                    actor="owner",
                    created_at=now,
                    idempotency_key="curate:negative",
                ).id

        with ThreadPoolExecutor(max_workers=2) as executor:
            curation_ids = tuple(executor.map(include_negative, range(2)))
        assert curation_ids[0] == curation_ids[1]
        include_review_event(
            connection,
            review_event_id=qualified_feedback.review_event_id,
            critical=False,
            reason="Known positive control.",
            actor="owner",
            created_at=now,
            idempotency_key="curate:positive",
        )
        connection.execute(
            """
            INSERT INTO snapshot_corrections (
              snapshot_id, description, reason, created_at
            ) VALUES (%s, %s, %s, %s)
            """,
            (qualified.snapshot_id, "Corrected job description.", "ATS repair", now),
        )

        preview = preview_manifest(connection, ManifestPolicy())
        assert preview.id == "0" * 64
        assert preview.case_count == 2
        assert preview.critical_count == 1
        assert preview.trial_count == 4

        first = create_manifest(
            connection,
            policy=ManifestPolicy(),
            created_at=now,
            created_by="owner",
            idempotency_key="manifest:first",
        )
        assert (
            next(
                case
                for case in first.cases
                if case.review_event_id == rejected_feedback.review_event_id
            ).input.description
            == "Corrected job description."
        )
        revised_feedback = record_review(
            connection,
            ReviewSubmission(
                review_item_id=qualified.id,
                evaluation_id=qualified.evaluation_id,
                snapshot_id=qualified.snapshot_id,
                decision="pursue",
                target_profile="applied-ai-product-engineer",
                primary_reason="technology-fit",
                actor="owner",
                created_at=now + timedelta(seconds=1),
            ),
        )
        assert isinstance(revised_feedback, ReviewSaved)
        include_review_event(
            connection,
            review_event_id=revised_feedback.review_event_id,
            critical=False,
            reason="Revised positive control.",
            actor="owner",
            created_at=now + timedelta(seconds=1),
            idempotency_key="curate:revised",
        )
        retried_first = create_manifest(
            connection,
            policy=ManifestPolicy(),
            created_at=now + timedelta(seconds=1),
            created_by="owner",
            idempotency_key="manifest:first",
        )
        assert retried_first == first
        revised_manifest = create_manifest(
            connection,
            policy=ManifestPolicy(),
            created_at=now + timedelta(seconds=1),
            created_by="owner",
            idempotency_key="manifest:revised",
        )
        assert rejected_feedback.review_event_id not in {
            case.review_event_id for case in revised_manifest.cases
        }
        exclude_review_event(
            connection,
            review_event_id=qualified_feedback.review_event_id,
            reason="Temporarily disputed.",
            actor="owner",
            created_at=now + timedelta(seconds=2),
            idempotency_key="exclude:positive",
        )
        second = create_manifest(
            connection,
            policy=ManifestPolicy(),
            created_at=now + timedelta(seconds=2),
            created_by="owner",
            idempotency_key="manifest:second",
        )

        assert len(first.cases) == 2
        assert sorted(case.trial_count for case in first.cases) == [1, 3]
        assert len(second.cases) == 1
        manifests = list_manifests(connection)
        assert tuple(item.id for item in manifests.items) == (
            second.id,
            revised_manifest.id,
            first.id,
        )
        assert manifests.items[0].case_count == 1
        excluded = list_review_feedback(connection, curation="excluded")
        assert tuple(item.review_event_id for item in excluded.items) == (
            qualified_feedback.review_event_id,
        )
        frozen_feedback = load_review_feedback(connection, qualified_feedback.review_event_id)
        assert frozen_feedback.frozen_manifest_count == 2
        assert frozen_feedback.curation is not None
        assert frozen_feedback.curation.action == "exclude"
        projection_status = load_projection_queue_status(connection)
        assert projection_status.pending_count >= 3
        assert projection_status.failed_count == 0
        assert connection.execute("SELECT count(*) FROM review_events").fetchone() == (3,)
        assert connection.execute(
            "SELECT count(*) FROM langfuse_projection_items WHERE kind = 'evaluation_manifest'"
        ).fetchone() == (3,)
        include_review_event(
            connection,
            review_event_id=qualified_feedback.review_event_id,
            critical=False,
            reason="Dispute resolved.",
            actor="owner",
            created_at=now + timedelta(seconds=3),
            idempotency_key="reinclude:positive",
        )
        connection.execute(
            """
            CREATE FUNCTION reject_manifest_projection_for_contract() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'projection unavailable'; END; $$
            """
        )
        connection.execute(
            """
            CREATE TRIGGER reject_manifest_projection_for_contract
            BEFORE INSERT ON langfuse_projection_items
            FOR EACH ROW WHEN (NEW.kind = 'evaluation_manifest')
            EXECUTE FUNCTION reject_manifest_projection_for_contract()
            """
        )
        with pytest.raises(psycopg.Error, match="projection unavailable"):
            create_manifest(
                connection,
                policy=ManifestPolicy(),
                created_at=now + timedelta(seconds=3),
                created_by="owner",
                idempotency_key="manifest:third",
            )
        assert connection.execute("SELECT count(*) FROM evaluation_manifests").fetchone() == (3,)
        assert connection.execute("SELECT count(*) FROM evaluation_manifest_cases").fetchone() == (
            5,
        )
        with pytest.raises(psycopg.errors.CheckViolation, match="immutable"):
            connection.execute(
                "UPDATE evaluation_manifests SET created_by = 'other' WHERE id = %s",
                (first.id,),
            )


def test_concurrent_manifest_execution_calls_provider_once(authority_schema: str) -> None:
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    with _connection(authority_schema) as connection:
        manifest_id, target, rates = _seed_evaluation_execution_context(connection, now)
    command = EvaluateManifestCommand(
        idempotency_key="evaluation:concurrent",
        manifest_id=manifest_id,
        target=target,
        implementation_ref="concurrency-test",
    )
    evaluator_entered = Barrier(2)
    second_connected = Event()
    second_backend_pid: list[int] = []
    allow_first_to_finish = Event()
    factory_calls: list[int] = []
    evaluator_calls: list[int] = []

    def execute(worker: int) -> object:
        with _connection(authority_schema) as connection:
            if worker == 2:
                second_backend_pid.append(connection.info.backend_pid)
                second_connected.set()

            def create_evaluator(
                stored_rates: ExchangeRateSnapshot,
                _record: Callable[[ProviderRequestObservation], None],
            ) -> Callable[[EvaluationManifestCase, ReleaseTarget, int], EvaluationResult]:
                factory_calls.append(worker)
                assert stored_rates == rates

                def evaluate(
                    _case: EvaluationManifestCase,
                    case_target: ReleaseTarget,
                    trial: int,
                ) -> EvaluationResult:
                    evaluator_calls.append(worker)
                    assert case_target == target
                    assert trial == 0
                    if len(evaluator_calls) == 1:
                        evaluator_entered.wait(timeout=5)
                        assert allow_first_to_finish.wait(timeout=5)
                    return Qualified(reason="Expected positive.", profile_name="profile")

                return evaluate

            return run_manifest(
                connection,
                command=command,
                create_exchange_rates=lambda: rates,
                create_evaluator=create_evaluator,
                now=lambda: now,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(execute, 1)
        evaluator_entered.wait(timeout=5)
        second = executor.submit(execute, 2)
        assert second_connected.wait(timeout=5)
        deadline = time.monotonic() + 5
        with _connection(authority_schema) as observer:
            while True:
                wait_state = observer.execute(
                    "SELECT wait_event_type, wait_event FROM pg_stat_activity WHERE pid = %s",
                    (second_backend_pid[0],),
                ).fetchone()
                if wait_state == ("Lock", "advisory"):
                    break
                if time.monotonic() >= deadline:
                    pytest.fail("second evaluator did not contend on the advisory lock")
                time.sleep(0.01)
        allow_first_to_finish.set()
        first_execution = first.result(timeout=5)
        second_execution = second.result(timeout=5)

    assert isinstance(first_execution, CompletedEvaluationExecution)
    assert second_execution == first_execution
    assert factory_calls == [1]
    assert evaluator_calls == [1]


def test_same_connection_manifest_execution_is_rejected_before_database_work(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    with _connection(authority_schema) as connection:
        manifest_id, target, rates = _seed_evaluation_execution_context(connection, now)
        command = EvaluateManifestCommand(
            idempotency_key="evaluation:same-connection",
            manifest_id=manifest_id,
            target=target,
            implementation_ref="same-connection-test",
        )
        evaluator_entered = Event()
        allow_first_to_finish = Event()

        def create_evaluator(
            _rates: ExchangeRateSnapshot,
            _record: Callable[[ProviderRequestObservation], None],
        ) -> Callable[[EvaluationManifestCase, ReleaseTarget, int], EvaluationResult]:
            def evaluate(
                _case: EvaluationManifestCase,
                _target: ReleaseTarget,
                _trial: int,
            ) -> EvaluationResult:
                evaluator_entered.set()
                assert allow_first_to_finish.wait(timeout=5)
                return Qualified(reason="Expected positive.", profile_name="profile")

            return evaluate

        with ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(
                run_manifest,
                connection,
                command=command,
                create_exchange_rates=lambda: rates,
                create_evaluator=create_evaluator,
                now=lambda: now,
            )
            assert evaluator_entered.wait(timeout=5)
            side_effects: list[str] = []
            try:
                with pytest.raises(RuntimeError, match="already active on this connection"):
                    run_manifest(
                        connection,
                        command=command,
                        create_exchange_rates=lambda: side_effects.append("rates") or rates,
                        create_evaluator=lambda _rates, _record: side_effects.append("evaluator")
                        or (lambda _case, _target, _trial: Rejected(reason="unexpected")),
                        now=lambda: now,
                    )
            finally:
                allow_first_to_finish.set()
            assert isinstance(first.result(timeout=5), CompletedEvaluationExecution)
        assert side_effects == []


def test_terminal_execution_retries_and_mismatches_have_no_side_effects(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    with _connection(authority_schema) as connection:
        manifest_id, target, rates = _seed_evaluation_execution_context(connection, now)
        command = EvaluateManifestCommand(
            idempotency_key="evaluation:terminal",
            manifest_id=manifest_id,
            target=target,
            implementation_ref="terminal-test",
        )
        completed = run_manifest(
            connection,
            command=command,
            create_exchange_rates=lambda: rates,
            create_evaluator=lambda _rates, record: (
                lambda _case, _target, _trial: Qualified(
                    reason="Expected positive.", profile_name="profile"
                )
            ),
            now=lambda: now,
        )
        side_effects: list[str] = []
        repeated = run_manifest(
            connection,
            command=command,
            create_exchange_rates=lambda: side_effects.append("rates") or rates,
            create_evaluator=lambda _rates, _record: side_effects.append("evaluator")
            or (lambda _case, _target, _trial: Rejected(reason="unexpected")),
            now=lambda: now,
        )

        assert repeated == completed
        assert side_effects == []
        with pytest.raises(ValueError, match="different evaluation execution"):
            run_manifest(
                connection,
                command=command.model_copy(update={"implementation_ref": "different"}),
                create_exchange_rates=lambda: side_effects.append("rates") or rates,
                create_evaluator=lambda _rates, _record: side_effects.append("evaluator")
                or (lambda _case, _target, _trial: Rejected(reason="unexpected")),
                now=lambda: now,
            )
        assert side_effects == []


def test_interrupted_and_exceptional_executions_fail_without_replay(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    with _connection(authority_schema) as connection:
        manifest_id, target, rates = _seed_evaluation_execution_context(connection, now)
        interrupted_command = EvaluateManifestCommand(
            idempotency_key="evaluation:interrupted",
            manifest_id=manifest_id,
            target=target,
            implementation_ref="interrupted-test",
        )
        interrupted_id = hashlib.sha256(
            f"evaluation_execution:{interrupted_command.idempotency_key}".encode()
        ).hexdigest()
        connection.execute(
            """
            INSERT INTO evaluation_run_executions (
              id, idempotency_key, manifest_id, prompt_release_id,
              relevance_release_id, implementation_ref, state,
              exchange_rate_snapshot, exchange_rate_digest, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, 'running', %s, %s, %s)
            """,
            (
                interrupted_id,
                interrupted_command.idempotency_key,
                manifest_id,
                target.prompt_release_id,
                target.relevance_release_id,
                interrupted_command.implementation_ref,
                Jsonb(rates.model_dump(mode="json")),
                exchange_rate_snapshot_digest(rates),
                now,
            ),
        )
        side_effects: list[str] = []
        interrupted = run_manifest(
            connection,
            command=interrupted_command,
            create_exchange_rates=lambda: side_effects.append("rates") or rates,
            create_evaluator=lambda _rates, _record: side_effects.append("evaluator")
            or (lambda _case, _target, _trial: Rejected(reason="unexpected")),
            now=lambda: now,
        )
        assert isinstance(interrupted, FailedEvaluationExecution)
        assert interrupted.failure.code == "interrupted_execution"
        assert interrupted.telemetry is None
        assert side_effects == []

        exception_command = interrupted_command.model_copy(
            update={"idempotency_key": "evaluation:exception"}
        )

        def exceptional_factory(
            _rates: ExchangeRateSnapshot,
            record: Callable[[ProviderRequestObservation], None],
        ) -> Callable[[EvaluationManifestCase, ReleaseTarget, int], EvaluationResult]:
            def evaluate(
                _case: EvaluationManifestCase,
                _target: ReleaseTarget,
                _trial: int,
            ) -> EvaluationResult:
                record(
                    ProviderRequestObservation(
                        input_tokens=7,
                        output_tokens=2,
                        cost_usd=Decimal("0.03"),
                        latency_ms=50,
                    )
                )
                raise RuntimeError("provider response processing failed")

            return evaluate

        failed = run_manifest(
            connection,
            command=exception_command,
            create_exchange_rates=lambda: rates,
            create_evaluator=exceptional_factory,
            now=lambda: now,
        )
        assert isinstance(failed, FailedEvaluationExecution)
        assert failed.failure.code == "unexpected_exception"
        assert failed.failure.error_type == "RuntimeError"
        assert failed.telemetry is not None
        assert failed.telemetry.request_count == 1
        assert failed.telemetry.input_tokens == 7
        assert "provider response processing failed" not in failed.failure.message

        retry_side_effects: list[str] = []
        assert (
            run_manifest(
                connection,
                command=exception_command,
                create_exchange_rates=lambda: retry_side_effects.append("rates") or rates,
                create_evaluator=lambda _rates, _record: retry_side_effects.append("evaluator")
                or (lambda _case, _target, _trial: Rejected(reason="unexpected")),
                now=lambda: now,
            )
            == failed
        )
        assert retry_side_effects == []


def test_evaluation_execution_sql_rejects_invalid_digest_and_transitions(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    with _connection(authority_schema) as connection:
        manifest_id, target, rates = _seed_evaluation_execution_context(connection, now)
        values = (
            "d" * 64,
            "evaluation:invalid-sql",
            manifest_id,
            target.prompt_release_id,
            target.relevance_release_id,
            "sql-test",
            Jsonb(rates.model_dump(mode="json")),
            "0" * 64,
            now,
        )
        with pytest.raises(
            psycopg.errors.CheckViolation,
            match="evaluation_execution_rate_digest_matches",
        ):
            connection.execute(
                """
                INSERT INTO evaluation_run_executions (
                  id, idempotency_key, manifest_id, prompt_release_id,
                  relevance_release_id, implementation_ref, state,
                  exchange_rate_snapshot, exchange_rate_digest, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, 'running', %s, %s, %s)
                """,
                values,
            )

        valid_values = (*values[:-2], exchange_rate_snapshot_digest(rates), now)
        connection.execute(
            """
            INSERT INTO evaluation_run_executions (
              id, idempotency_key, manifest_id, prompt_release_id,
              relevance_release_id, implementation_ref, state,
              exchange_rate_snapshot, exchange_rate_digest, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, 'running', %s, %s, %s)
            """,
            valid_values,
        )
        with pytest.raises(psycopg.errors.IntegrityConstraintViolation, match="must become"):
            connection.execute(
                "UPDATE evaluation_run_executions SET created_at = created_at WHERE id = %s",
                (values[0],),
            )
        with pytest.raises(
            psycopg.errors.CheckViolation,
            match="evaluation_execution_latency_percentiles_ordered",
        ):
            connection.execute(
                """
                UPDATE evaluation_run_executions
                SET state = 'failed', request_count = 1, input_tokens = 0,
                    output_tokens = 0, cost_usd = 0, usage_complete = TRUE,
                    p50_latency_ms = 2, p95_latency_ms = 1,
                    failure = '{"code":"failed","message":"failed"}', terminal_at = %s
                WHERE id = %s
                """,
                (now, values[0]),
            )
        with pytest.raises(
            psycopg.errors.CheckViolation,
            match="evaluation_execution_terminal_time_ordered",
        ):
            connection.execute(
                """
                UPDATE evaluation_run_executions
                SET state = 'failed', request_count = 0, input_tokens = 0,
                    output_tokens = 0, cost_usd = 0, usage_complete = TRUE,
                    failure = '{"code":"failed","message":"failed"}', terminal_at = %s
                WHERE id = %s
                """,
                (now - timedelta(seconds=1), values[0]),
            )
