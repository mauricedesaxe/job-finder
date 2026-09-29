from __future__ import annotations

from contracts.test_postgres_authority import (
    CriterionAccepted,
    DEFAULT_SEARCH_CONFIGURATION,
    Decimal,
    DecisionContext,
    ENRICHMENT,
    EnrichedJob,
    HttpResponse,
    JobListing,
    Jsonb,
    Mapping,
    ModelCallContext,
    PROMPTS,
    PersistedDecision,
    PromptAccepted,
    PromptReleaseError,
    Qualified,
    RetryPolicy,
    RetryableOperationalError,
    TerminalOperationalError,
    TitleDuplicate,
    UTC,
    _connection,
    _decision_enrichment,
    _decision_listing,
    _insert_prompt_run,
    apply_migrations,
    bootstrap_prompt_release,
    build_prompt_release,
    datetime,
    evaluate_prompt,
    json,
    load_prompt_release,
    postgres_decision_store,
    postgres_model_call_persistence,
    process_qualified_job,
    prompt_input_digest,
    psycopg,
    pytest,
    replace,
    store_prompt_release,
    token_hex,
    uuid4,
    UUID,
)

from job_finder.pipeline.processing_attempts import (
    complete_model_call_context,
    run_model_call_attempt,
)

pytest_plugins = ("contracts.test_postgres_authority",)


def test_stores_a_prompt_release_inside_a_committed_outer_transaction(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    configuration = DEFAULT_SEARCH_CONFIGURATION.model_copy(
        update={
            "target_profiles": (
                DEFAULT_SEARCH_CONFIGURATION.target_profiles[0].model_copy(
                    update={"instructions": "A profile committed by publication."}
                ),
                *DEFAULT_SEARCH_CONFIGURATION.target_profiles[1:],
            )
        }
    )
    release = build_prompt_release(configuration)
    with _connection(authority_schema) as connection:
        apply_migrations(connection)

        with connection.transaction():
            stored = store_prompt_release(
                connection,
                release,
                created_at=now,
                created_by="contract",
            )

        assert stored == release
        assert load_prompt_release(connection, release.id) == release


def test_outer_transaction_rollback_removes_a_stored_prompt_release(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    configuration = DEFAULT_SEARCH_CONFIGURATION.model_copy(
        update={
            "target_profiles": (
                DEFAULT_SEARCH_CONFIGURATION.target_profiles[0].model_copy(
                    update={"instructions": "A deliberately rolled-back profile."}
                ),
                *DEFAULT_SEARCH_CONFIGURATION.target_profiles[1:],
            )
        }
    )
    release = build_prompt_release(configuration)
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        before_versions = connection.execute("SELECT count(*) FROM prompt_versions").fetchone()

        with pytest.raises(RuntimeError, match="rollback publication"):
            with connection.transaction():
                assert (
                    store_prompt_release(
                        connection,
                        release,
                        created_at=now,
                        created_by="contract",
                    )
                    == release
                )
                raise RuntimeError("rollback publication")

        with pytest.raises(PromptReleaseError, match="Prompt release not found"):
            load_prompt_release(connection, release.id)
        assert (
            connection.execute("SELECT count(*) FROM prompt_versions").fetchone() == before_versions
        )
        assert connection.execute(
            "SELECT count(*) FROM prompt_release_members WHERE release_id = %s",
            (release.id,),
        ).fetchone() == (0,)


def test_bootstrap_fails_loudly_when_a_release_name_is_reused(
    authority_schema: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        first = bootstrap_prompt_release(connection)
        weakened_prompts = tuple(
            replace(prompt, model=None) if prompt.name == ENRICHMENT.name else prompt
            for prompt in PROMPTS
        )
        monkeypatch.setattr("job_finder.evaluation.prompt_releases.PROMPTS", weakened_prompts)

        with pytest.raises(psycopg.errors.UniqueViolation, match="prompt_releases_name_key"):
            bootstrap_prompt_release(connection)

        assert load_prompt_release(connection, first.id) == first


def test_resumes_usage_lookup_then_reuses_an_accepted_model_call(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    run_id = uuid4()
    processing_attempt_id = uuid4()
    input_digest = prompt_input_digest({"job": "job body"})
    calls = 0
    generation_responses = iter(
        (
            HttpResponse(404, '{"error":{"message":"not ready"}}'),
            HttpResponse(
                200,
                '{"data":{"tokens_prompt":12,"tokens_completion":4,"total_cost":0.00012}}',
            ),
        )
    )
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        release = bootstrap_prompt_release(connection)
        connection.execute(
            """
            INSERT INTO pipeline_runs (
              id, idempotency_key, kind, implementation_ref, prompt_release_id, parameters,
              status, started_at, completed_at
            ) VALUES (%s, %s, 'evaluation', 'test-ref', %s, '{}'::jsonb,
              'completed', %s, %s)
            """,
            (run_id, f"evaluation:{run_id}", release.id, now, now),
        )
        connection.execute(
            """
            INSERT INTO processing_attempts (
              id, pipeline_run_id, operation_key, attempt_number, input_digest,
              status, started_at, completed_at
            ) VALUES (%s, %s, 'evaluate_job', 0, %s, 'completed', %s, %s)
            """,
            (processing_attempt_id, run_id, input_digest, now, now),
        )
        context = ModelCallContext(
            processing_attempt_id=processing_attempt_id,
            pipeline_run_id=run_id,
            prompt_release_id=release.id,
            operation_key="evaluate_job",
            input_digest=input_digest,
        )

        def send(
            _url: str,
            _headers: Mapping[str, str],
            _body: dict[str, object],
            _timeout: float,
        ) -> HttpResponse:
            nonlocal calls
            calls += 1
            return HttpResponse(
                status_code=200,
                body=json.dumps(
                    {
                        "id": "generation-1",
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
                    }
                ),
            )

        def lookup_generation(
            _url: str, _headers: Mapping[str, str], _id: str, _timeout: float
        ) -> HttpResponse:
            return next(generation_responses)

        persistence = postgres_model_call_persistence(connection)
        first = evaluate_prompt(
            release.versions[0],
            {"job": "job body"},
            context,
            persistence,
            api_key="secret",
            sender=send,
            generation_sender=lookup_generation,
            retry_policy=RetryPolicy(max_attempts=1, base_delay_seconds=0),
            sleep=lambda _delay: None,
            now=lambda: now,
        )
        second = evaluate_prompt(
            release.versions[0],
            {"job": "job body"},
            context,
            persistence,
            api_key="secret",
            sender=send,
            generation_sender=lookup_generation,
            retry_policy=RetryPolicy(max_attempts=1, base_delay_seconds=0),
            sleep=lambda _delay: None,
            now=lambda: now,
        )
        third = evaluate_prompt(
            release.versions[0],
            {"job": "job body"},
            context,
            persistence,
            api_key="secret",
            sender=send,
            generation_sender=lookup_generation,
            retry_policy=RetryPolicy(max_attempts=1, base_delay_seconds=0),
            sleep=lambda _delay: None,
            now=lambda: now,
        )

        assert isinstance(first, RetryableOperationalError)
        assert second == CriterionAccepted(
            prompt_name=release.versions[0].definition.name,
            passed=True,
            reason="matched",
        )
        assert third == second
        assert calls == 1
        assert connection.execute(
            "SELECT status FROM model_call_attempts ORDER BY attempt_number"
        ).fetchall() == [("retryable_error",), ("accepted",)]
        assert connection.execute(
            """
            SELECT status, response_model, provider_response_id, input_tokens,
                   output_tokens, cost_usd, parsed_output, request_messages
            FROM model_call_attempts
            WHERE status = 'accepted'
            """
        ).fetchone() == (
            "accepted",
            "google/gemini-2.5-flash-001",
            "generation-1",
            12,
            4,
            Decimal("0.00012000"),
            {"pass": True, "reason": "matched"},
            [
                {"role": "system", "content": release.versions[0].messages[0]["content"]},
                {"role": "user", "content": "job body"},
            ],
        )
        assert connection.execute(
            """
            SELECT kind, payload ->> 'requested_model', payload ->> 'status'
            FROM langfuse_projection_items
            WHERE kind = 'model_call'
              AND payload ->> 'status' = 'accepted'
            """
        ).fetchone() == (
            "model_call",
            "google/gemini-2.5-flash",
            "accepted",
        )
        terminal_processing_attempt_id = uuid4()
        terminal_input_digest = prompt_input_digest({"job": "another job body"})
        connection.execute(
            """
            INSERT INTO processing_attempts (
              id, pipeline_run_id, operation_key, attempt_number, input_digest,
              status, started_at, completed_at
            ) VALUES (%s, %s, 'evaluate_terminal_usage', 0, %s, 'completed', %s, %s)
            """,
            (terminal_processing_attempt_id, run_id, terminal_input_digest, now, now),
        )
        terminal = evaluate_prompt(
            release.versions[0],
            {"job": "another job body"},
            ModelCallContext(
                processing_attempt_id=terminal_processing_attempt_id,
                pipeline_run_id=run_id,
                prompt_release_id=release.id,
                operation_key="evaluate_terminal_usage",
                input_digest=terminal_input_digest,
            ),
            persistence,
            api_key="secret",
            sender=send,
            generation_sender=lambda _url, _headers, _id, _timeout: HttpResponse(
                401, '{"error":{"message":"unauthorized"}}'
            ),
            retry_policy=RetryPolicy(max_attempts=1, base_delay_seconds=0),
            sleep=lambda _delay: None,
            now=lambda: now,
        )

        assert isinstance(terminal, TerminalOperationalError)
        assert connection.execute(
            """
            SELECT status, response_model
            FROM model_call_attempts
            WHERE processing_attempt_id = %s
            """,
            (terminal_processing_attempt_id,),
        ).fetchone() == ("terminal_error", "google/gemini-2.5-flash-001")


def test_records_and_reuses_a_terminal_model_error(authority_schema: str) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    run_id = uuid4()
    processing_attempt_id = uuid4()
    input_digest = prompt_input_digest({"job": "job body"})
    calls = 0
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        release = bootstrap_prompt_release(connection)
        connection.execute(
            """
            INSERT INTO pipeline_runs (
              id, idempotency_key, kind, implementation_ref, prompt_release_id, parameters,
              status, started_at, completed_at
            ) VALUES (%s, %s, 'evaluation', 'test-ref', %s, '{}'::jsonb,
              'completed', %s, %s)
            """,
            (run_id, f"evaluation:{run_id}", release.id, now, now),
        )
        connection.execute(
            """
            INSERT INTO processing_attempts (
              id, pipeline_run_id, operation_key, attempt_number, input_digest,
              status, started_at
            ) VALUES (%s, %s, 'evaluate_job', 0, %s, 'running', %s)
            """,
            (processing_attempt_id, run_id, input_digest, now),
        )
        context = ModelCallContext(
            processing_attempt_id=processing_attempt_id,
            pipeline_run_id=run_id,
            prompt_release_id=release.id,
            operation_key="evaluate_job",
            input_digest=input_digest,
        )

        def send(
            _url: str,
            _headers: Mapping[str, str],
            _body: dict[str, object],
            _timeout: float,
        ) -> HttpResponse:
            nonlocal calls
            calls += 1
            return HttpResponse(400, '{"error":{"message":"bad request"}}')

        persistence = postgres_model_call_persistence(connection)
        first = evaluate_prompt(
            release.versions[0],
            {"job": "job body"},
            context,
            persistence,
            api_key="secret",
            sender=send,
            now=lambda: now,
        )
        second = evaluate_prompt(
            release.versions[0],
            {"job": "job body"},
            context,
            persistence,
            api_key="secret",
            sender=send,
            now=lambda: now,
        )

    assert isinstance(first, TerminalOperationalError)
    assert second == first
    assert calls == 1


def test_accepted_model_call_attempts_require_provider_provenance(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 21, 12, tzinfo=UTC)
    run_id = uuid4()
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        release = bootstrap_prompt_release(connection)
        version = release.versions[0]
        connection.execute(
            """
            INSERT INTO pipeline_runs (
              id, idempotency_key, kind, implementation_ref, prompt_release_id, parameters,
              status, started_at, completed_at
            ) VALUES (%s, %s, 'evaluation', 'provenance-ref', %s, '{}'::jsonb,
              'completed', %s, %s)
            """,
            (run_id, f"evaluation:{run_id}", release.id, now, now),
        )

        def insert_model_call_attempt(
            *,
            provider: str,
            status: str,
            provider_response_id: str | None,
            raw_response: object,
            parsed_output: object,
            response_model: str | None,
            error: object,
            input_tokens: int | None,
            output_tokens: int | None,
            cost_usd: Decimal | None,
        ) -> str:
            request_id = token_hex(32)
            processing_attempt_id = uuid4()
            input_digest = token_hex(32)
            connection.execute(
                """
                INSERT INTO processing_attempts (
                  id, pipeline_run_id, operation_key, attempt_number, input_digest,
                  status, started_at, completed_at
                ) VALUES (%s, %s, %s, 0, %s, 'completed', %s, %s)
                """,
                (processing_attempt_id, run_id, request_id, input_digest, now, now),
            )
            connection.execute(
                """
                INSERT INTO model_call_attempts (
                  id, processing_attempt_id, pipeline_run_id, prompt_release_id, request_id,
                  attempt_number, operation_key, prompt_name, prompt_version_id, input_digest,
                  requested_model, provider, provider_response_id, status, parsed_output,
                  raw_response, input_tokens, output_tokens, cost_usd, latency_ms, observed_at,
                  request_messages, response_model, error
                ) VALUES (
                  %s, %s, %s, %s, %s, 0, %s, %s, %s, %s,
                  'provenance-model', %s, %s, %s, %s, %s, %s, %s, %s, 1, %s,
                  '[]'::jsonb, %s, %s
                )
                """,
                (
                    uuid4(),
                    processing_attempt_id,
                    run_id,
                    release.id,
                    request_id,
                    request_id,
                    version.definition.name,
                    version.id,
                    input_digest,
                    provider,
                    provider_response_id,
                    status,
                    None if parsed_output is None else Jsonb(parsed_output),
                    None if raw_response is None else Jsonb(raw_response),
                    input_tokens,
                    output_tokens,
                    cost_usd,
                    now,
                    response_model,
                    None if error is None else Jsonb(error),
                ),
            )
            return request_id

        with pytest.raises(psycopg.errors.CheckViolation) as missing_raw_response:
            insert_model_call_attempt(
                provider="typesafe",
                status="accepted",
                provider_response_id=None,
                raw_response=None,
                parsed_output={"pass": True},
                response_model="provenance-model",
                error=None,
                input_tokens=1,
                output_tokens=1,
                cost_usd=Decimal("0.00000001"),
            )
        assert (
            missing_raw_response.value.diag.constraint_name
            == "model_call_attempts_accepted_provenance"
        )

        with pytest.raises(psycopg.errors.CheckViolation) as missing_provider_response:
            insert_model_call_attempt(
                provider="openrouter",
                status="accepted",
                provider_response_id=None,
                raw_response={"id": "generation-1"},
                parsed_output={"pass": True},
                response_model="provenance-model",
                error=None,
                input_tokens=1,
                output_tokens=1,
                cost_usd=Decimal("0.00000001"),
            )
        assert (
            missing_provider_response.value.diag.constraint_name
            == "model_call_attempts_accepted_provenance"
        )

        with pytest.raises(psycopg.errors.CheckViolation) as unsupported_provider:
            insert_model_call_attempt(
                provider="anthropic",
                status="terminal_error",
                provider_response_id=None,
                raw_response=None,
                parsed_output=None,
                response_model=None,
                error={"code": "unauthorized"},
                input_tokens=None,
                output_tokens=None,
                cost_usd=None,
            )
        assert (
            unsupported_provider.value.diag.constraint_name == "model_call_attempts_provider_check"
        )

        relaxed_request_id = insert_model_call_attempt(
            provider="typesafe",
            status="accepted",
            provider_response_id=None,
            raw_response={"choices": []},
            parsed_output={"pass": True},
            response_model="provenance-model",
            error=None,
            input_tokens=1,
            output_tokens=1,
            cost_usd=Decimal("0.00000001"),
        )
        assert connection.execute(
            """
            SELECT provider, status, provider_response_id, raw_response IS NOT NULL
            FROM model_call_attempts
            WHERE request_id = %s
            """,
            (relaxed_request_id,),
        ).fetchone() == ("typesafe", "accepted", None, True)


def test_persists_a_terminal_decision_atomically_and_idempotently(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    run_id = uuid4()
    calls = {"enrichment": 0, "deduplication": 0}
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        release = bootstrap_prompt_release(connection)
        _insert_prompt_run(connection, run_id, release.id, now)
        store = postgres_decision_store(connection)
        listing = _decision_listing()
        context = DecisionContext(
            pipeline_run_id=run_id,
            prompt_release_id=release.id,
            policy_version="policy-1",
            implementation_ref="test-ref",
            observed_at=now,
        )

        def enrich(_listing: JobListing) -> PromptAccepted[EnrichedJob]:
            calls["enrichment"] += 1
            return PromptAccepted(
                prompt_name="job-finder-enrichment", output=_decision_enrichment()
            )

        def deduplicate(
            _title: str, existing_titles: tuple[str, ...]
        ) -> PromptAccepted[TitleDuplicate]:
            calls["deduplication"] += 1
            assert existing_titles == ()
            return PromptAccepted(
                prompt_name="job-finder-title-deduplication",
                output=TitleDuplicate(isDuplicate=False),
            )

        first = process_qualified_job(
            listing,
            Qualified(reason="Matches", profile_name="applied-ai"),
            context,
            store,
            enrich,
            deduplicate,
        )
        second = process_qualified_job(
            listing,
            Qualified(reason="Matches", profile_name="applied-ai"),
            context,
            store,
            enrich,
            deduplicate,
        )

        assert isinstance(first, PersistedDecision)
        assert second == first
        assert first.outcome == "qualified"
        assert first.job == _decision_enrichment()
        assert calls == {"enrichment": 1, "deduplication": 1}
        assert connection.execute("SELECT count(*) FROM jobs").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM job_snapshots").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM evaluation_decisions").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM pipeline_receipts").fetchone() == (1,)


def test_persists_structured_compensation_on_the_snapshot(authority_schema: str) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    run_id = uuid4()
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        release = bootstrap_prompt_release(connection)
        _insert_prompt_run(connection, run_id, release.id, now)
        store = postgres_decision_store(connection)
        listing = _decision_listing()
        context = DecisionContext(
            pipeline_run_id=run_id,
            prompt_release_id=release.id,
            policy_version="policy-1",
            implementation_ref="test-ref",
            observed_at=now,
        )
        result = process_qualified_job(
            listing,
            Qualified(reason="Matches", profile_name="applied-ai"),
            context,
            store,
            lambda _listing: PromptAccepted(
                prompt_name="job-finder-enrichment", output=_decision_enrichment()
            ),
            lambda _title, _existing: PromptAccepted(
                prompt_name="job-finder-title-deduplication",
                output=TitleDuplicate(isDuplicate=False),
            ),
            ats_evidence={
                "kind": "available",
                "source": "ashby",
                "location": "Berlin/Remote",
                "locations": ["Berlin/Remote"],
                "workplace_type": "Remote",
                "country": None,
                "description": None,
                "compensation": {
                    "minimum": 80000,
                    "maximum": 100000,
                    "currency": "EUR",
                    "period": "year",
                },
            },
        )

        assert isinstance(result, PersistedDecision)
        row = connection.execute(
            """
            SELECT compensation_min, compensation_max, compensation_currency,
                   compensation_period, compensation_source
            FROM job_snapshots
            WHERE raw_url = %s
            """,
            (listing.url,),
        ).fetchone()

    assert row == (Decimal("80000"), Decimal("100000"), "EUR", "year", "ats")


def test_run_model_call_attempt_fails_on_operational_error_and_completes_otherwise(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 22, 12, tzinfo=UTC)
    run_id = uuid4()
    job_id = uuid4()
    retryable = RetryableOperationalError(
        prompt_name="job-finder-filter-work-culture",
        error_code="http_503",
        reason="provider unavailable",
    )
    accepted = CriterionAccepted(
        prompt_name="job-finder-filter-work-culture", passed=True, reason="matched"
    )
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        release = bootstrap_prompt_release(connection)
        _insert_prompt_run(connection, run_id, release.id, now)
        connection.execute(
            """
            INSERT INTO jobs (id, raw_url, first_discovered_at, last_discovered_at)
            VALUES (%s, %s, %s, %s)
            """,
            (job_id, f"https://example.com/jobs/{job_id}", now, now),
        )
        retryable_digest = prompt_input_digest({"job": "retryable body"})
        first = run_model_call_attempt(
            connection,
            run_id=run_id,
            job_id=job_id,
            operation_key="evaluate_job",
            input_digest=retryable_digest,
            prompt_release_id=release.id,
            now=lambda: now,
            invoke=lambda _context: retryable,
        )
        failed_row = connection.execute(
            """
            SELECT status, completed_at, error->>'retryability'
            FROM processing_attempts
            WHERE operation_key = 'evaluate_job'
            """
        ).fetchone()

        assert first is retryable
        assert failed_row == ("failed", now, "retryable")

        attempt_id_row = connection.execute(
            "SELECT id FROM processing_attempts WHERE operation_key = 'evaluate_job'"
        ).fetchone()
        assert attempt_id_row is not None
        complete_model_call_context(
            connection,
            ModelCallContext(
                processing_attempt_id=UUID(str(attempt_id_row[0])),
                pipeline_run_id=run_id,
                prompt_release_id=release.id,
                operation_key="evaluate_job",
                input_digest=retryable_digest,
            ),
            completed_at=now,
        )
        guarded_row = connection.execute(
            """
            SELECT status, completed_at, error->>'retryability'
            FROM processing_attempts
            WHERE operation_key = 'evaluate_job'
            """
        ).fetchone()

        assert guarded_row == failed_row

        second = run_model_call_attempt(
            connection,
            run_id=run_id,
            job_id=job_id,
            operation_key="evaluate_job_accepted",
            input_digest=prompt_input_digest({"job": "accepted body"}),
            prompt_release_id=release.id,
            now=lambda: now,
            invoke=lambda _context: accepted,
        )
        completed_row = connection.execute(
            """
            SELECT status, completed_at, error IS NULL
            FROM processing_attempts
            WHERE operation_key = 'evaluate_job_accepted'
            """
        ).fetchone()

    assert second is accepted
    assert completed_row == ("completed", now, True)
