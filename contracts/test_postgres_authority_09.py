from __future__ import annotations

from contracts.test_postgres_authority import (
    CompensationObservation,
    Decimal,
    DecisionContext,
    PersistedDecision,
    PromptAccepted,
    Qualified,
    ReviewSaved,
    ReviewSubmission,
    TitleDuplicate,
    UTC,
    UUID,
    _connection,
    _decision_enrichment,
    _decision_listing,
    _insert_prompt_run,
    _insert_review_decision,
    apply_migrations,
    bootstrap_prompt_release,
    datetime,
    deterministic_rejected_sample,
    enqueue_qualified_review_item,
    enqueue_rejected_audit_sample,
    load_review_queue,
    postgres_decision_store,
    process_qualified_job,
    psycopg,
    pytest,
    record_review,
    timedelta,
    uuid4,
)

pytest_plugins = ("contracts.test_postgres_authority",)


def test_persists_llm_extracted_compensation_when_the_ats_has_none(
    authority_schema: str,
) -> None:
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
                prompt_name="job-finder-enrichment",
                output=_decision_enrichment().model_copy(
                    update={
                        "compensation": CompensationObservation(
                            minimum=70000, maximum=90000, currency="USD", period="year"
                        )
                    }
                ),
            ),
            lambda _title, _existing: PromptAccepted(
                prompt_name="job-finder-title-deduplication",
                output=TitleDuplicate(isDuplicate=False),
            ),
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

    assert row == (Decimal("70000"), Decimal("90000"), "USD", "year", "llm")


def test_rolls_back_every_terminal_row_when_the_decision_is_invalid(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        release = bootstrap_prompt_release(connection)
        store = postgres_decision_store(connection)

        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            process_qualified_job(
                _decision_listing(),
                Qualified(reason="Matches", profile_name="applied-ai"),
                DecisionContext(
                    pipeline_run_id=uuid4(),
                    prompt_release_id=release.id,
                    policy_version="policy-1",
                    implementation_ref="test-ref",
                    observed_at=now,
                ),
                store,
                lambda _listing: PromptAccepted(
                    prompt_name="job-finder-enrichment", output=_decision_enrichment()
                ),
                lambda _title, _existing: PromptAccepted(
                    prompt_name="job-finder-title-deduplication",
                    output=TitleDuplicate(isDuplicate=False),
                ),
            )

        assert connection.execute("SELECT count(*) FROM jobs").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM job_snapshots").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM evaluation_decisions").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM pipeline_receipts").fetchone() == (0,)


def test_enqueues_a_stable_review_queue_with_every_qualified_job(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    review_day = now.date()
    run_id = uuid4()
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        release = bootstrap_prompt_release(connection)
        _insert_prompt_run(connection, run_id, release.id, now)
        qualified_ids = tuple(
            _insert_review_decision(connection, run_id, release.id, now, value, "qualified")
            for value in range(1, 5)
        )
        rejected_ids = tuple(
            _insert_review_decision(connection, run_id, release.id, now, value, "rejected")
            for value in range(10, 18)
        )

        enqueued = tuple(
            enqueue_qualified_review_item(connection, evaluation_id, review_day)
            for evaluation_id in qualified_ids
        )
        late_qualified_id = _insert_review_decision(
            connection, run_id, release.id, now, 99, "qualified"
        )
        late_enqueued = enqueue_qualified_review_item(connection, late_qualified_id, review_day)
        repeated = tuple(
            enqueue_qualified_review_item(connection, evaluation_id, review_day)
            for evaluation_id in qualified_ids
        )
        sampled = enqueue_rejected_audit_sample(connection, review_day)
        resampled = enqueue_rejected_audit_sample(connection, review_day)
        queue = load_review_queue(connection)
        rows = connection.execute(
            """
            SELECT evaluation_id, lane, position
            FROM review_items
            ORDER BY lane, position, created_at
            """
        ).fetchall()

        first_sample = deterministic_rejected_sample(review_day, rejected_ids)
        remaining = tuple(sorted(set(rejected_ids) - set(first_sample)))
        second_sample = deterministic_rejected_sample(review_day, remaining)
        audit_rows = [row[0] for row in rows if row[1] == "rejected_audit"]
        audit_positions = sorted(int(str(row[2])) for row in rows if row[1] == "rejected_audit")
        assert all(enqueued)
        assert late_enqueued
        assert not any(repeated)
        assert sampled == 3
        assert resampled == 3
        assert set(audit_rows) == set(first_sample) | set(second_sample)
        assert audit_positions == [0, 0, 1, 1, 2, 2]
        assert set(first_sample).isdisjoint(second_sample)
        assert {item.evaluation_id for item in queue.items} == {
            *qualified_ids,
            late_qualified_id,
            *first_sample,
            *second_sample,
        }
        assert queue.reviewed_counts == {}
        with pytest.raises(psycopg.errors.CheckViolation, match="immutable"):
            connection.execute(
                "UPDATE review_items SET position = 99 WHERE id = %s",
                (queue.items[0].id,),
            )


def test_records_feedback_and_company_block_in_one_exact_transaction(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    run_id = uuid4()
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        release = bootstrap_prompt_release(connection)
        _insert_prompt_run(connection, run_id, release.id, now)
        evaluation_id = _insert_review_decision(connection, run_id, release.id, now, 1, "qualified")
        assert enqueue_qualified_review_item(connection, evaluation_id, now.date())
        item = load_review_queue(connection).items[0]
        assert item is not None
        submission = ReviewSubmission(
            review_item_id=item.id,
            evaluation_id=item.evaluation_id,
            snapshot_id=item.snapshot_id,
            decision="pursue",
            note="Strong fit.",
            block_company=True,
            actor="owner",
            created_at=now,
        )

        first = record_review(connection, submission)
        revision = record_review(
            connection,
            submission.model_copy(
                update={
                    "decision": "reject",
                    "note": "Ukraine-based team.",
                    "created_at": now + timedelta(minutes=5),
                }
            ),
        )

        assert isinstance(first, ReviewSaved)
        assert isinstance(revision, ReviewSaved)
        assert connection.execute("SELECT count(*) FROM review_events").fetchone() == (2,)
        assert connection.execute("SELECT policy FROM company_policies").fetchone() == ("blocked",)
        stored = connection.execute(
            """
            SELECT e.decision, e.target_profile, e.primary_reason, i.id, d.id, s.id
            FROM review_events e
            JOIN review_items i ON i.id = e.review_item_id
            JOIN evaluation_decisions d ON d.id = i.evaluation_id
            JOIN job_snapshots s ON s.id = d.snapshot_id
            WHERE e.created_at = %s
            """,
            (now + timedelta(minutes=5),),
        ).fetchone()
        assert stored == (
            "reject",
            "applied-ai-product-engineer",
            None,
            item.id,
            item.evaluation_id,
            item.snapshot_id,
        )
        queue = load_review_queue(connection)
        assert queue.items == ()
        assert queue.reviewed_counts == {now.date(): 1}


def test_an_identical_revision_is_a_stored_no_op(authority_schema: str) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    run_id = uuid4()
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        release = bootstrap_prompt_release(connection)
        _insert_prompt_run(connection, run_id, release.id, now)
        evaluation_id = _insert_review_decision(connection, run_id, release.id, now, 1, "qualified")
        assert enqueue_qualified_review_item(connection, evaluation_id, now.date())
        item = load_review_queue(connection).items[0]
        assert item is not None
        submission = ReviewSubmission(
            review_item_id=item.id,
            evaluation_id=item.evaluation_id,
            snapshot_id=item.snapshot_id,
            decision="unsure",
            note="Need more detail.",
            actor="owner",
            created_at=now,
        )

        first = record_review(connection, submission)
        repeat = record_review(connection, submission)

        assert isinstance(first, ReviewSaved)
        assert isinstance(repeat, ReviewSaved)
        assert first.review_event_id == repeat.review_event_id
        assert connection.execute("SELECT count(*) FROM review_events").fetchone() == (1,)

        middle = record_review(
            connection,
            submission.model_copy(
                update={"decision": "reject", "created_at": now + timedelta(minutes=1)}
            ),
        )
        restored = record_review(
            connection,
            submission.model_copy(update={"created_at": now + timedelta(minutes=2)}),
        )
        assert isinstance(middle, ReviewSaved)
        assert isinstance(restored, ReviewSaved)
        assert len({first.review_event_id, middle.review_event_id, restored.review_event_id}) == 3
        assert connection.execute("SELECT count(*) FROM review_events").fetchone() == (3,)
        assert load_review_queue(connection).reviewed_items[0].decision == "unsure"


def test_rolls_back_feedback_when_company_policy_fails(authority_schema: str) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    run_id = uuid4()
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        release = bootstrap_prompt_release(connection)
        _insert_prompt_run(connection, run_id, release.id, now)
        evaluation_id = _insert_review_decision(connection, run_id, release.id, now, 1, "qualified")
        assert enqueue_qualified_review_item(connection, evaluation_id, now.date())
        item = load_review_queue(connection).items[0]
        assert item is not None
        connection.execute(
            """
            CREATE FUNCTION reject_company_policy_for_contract() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'policy unavailable'; END; $$
            """
        )
        connection.execute(
            """
            CREATE TRIGGER reject_company_policy_for_contract
            BEFORE INSERT OR UPDATE ON company_policies
            FOR EACH ROW EXECUTE FUNCTION reject_company_policy_for_contract()
            """
        )

        with pytest.raises(psycopg.Error, match="policy unavailable"):
            record_review(
                connection,
                ReviewSubmission(
                    review_item_id=item.id,
                    evaluation_id=item.evaluation_id,
                    snapshot_id=item.snapshot_id,
                    decision="pursue",
                    target_profile="neither",
                    primary_reason="company-quality",
                    block_company=True,
                    actor="owner",
                    created_at=now,
                ),
            )

        assert connection.execute("SELECT count(*) FROM review_events").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM application_events").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM company_policies").fetchone() == (0,)


def test_a_pursue_records_the_application_and_cooldowns_the_company(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    run_id = uuid4()
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        release = bootstrap_prompt_release(connection)
        _insert_prompt_run(connection, run_id, release.id, now)
        first = _insert_review_decision(connection, run_id, release.id, now, 1, "qualified")
        second = _insert_review_decision(connection, run_id, release.id, now, 2, "qualified")
        assert enqueue_qualified_review_item(connection, first, now.date())
        assert enqueue_qualified_review_item(connection, second, now.date())
        first_item, second_item = load_review_queue(connection).items

        saved = record_review(
            connection,
            ReviewSubmission(
                review_item_id=first_item.id,
                evaluation_id=first_item.evaluation_id,
                snapshot_id=first_item.snapshot_id,
                decision="pursue",
                note="Strong fit.",
                actor="owner",
                created_at=now,
            ),
        )

        assert isinstance(saved, ReviewSaved)
        application = connection.execute(
            """
            SELECT job_id, kind, source_review_event_id, actor, occurred_at
            FROM application_events
            """
        ).fetchone()
        assert application == (
            UUID(int=1),
            "applied",
            saved.review_event_id,
            "owner",
            now,
        )
        policy = connection.execute(
            """
            SELECT policy, effective_at, expires_at, source_review_event_id
            FROM company_policies
            """
        ).fetchone()
        assert policy == (
            "recent_application",
            now,
            now + timedelta(days=180),
            saved.review_event_id,
        )

        queue = load_review_queue(connection)
        assert queue.items == ()
        assert len(queue.reviewed_items) == 1

        resaved = record_review(
            connection,
            ReviewSubmission(
                review_item_id=second_item.id,
                evaluation_id=second_item.evaluation_id,
                snapshot_id=second_item.snapshot_id,
                decision="pursue",
                note="Even stronger fit.",
                actor="owner",
                created_at=now + timedelta(days=30),
            ),
        )

        assert isinstance(resaved, ReviewSaved)
        assert connection.execute("SELECT count(*) FROM application_events").fetchone() == (2,)
        policy = connection.execute(
            "SELECT policy, effective_at, expires_at FROM company_policies"
        ).fetchone()
        assert policy == (
            "recent_application",
            now + timedelta(days=30),
            now + timedelta(days=30) + timedelta(days=180),
        )


def test_a_blocked_company_is_not_downgraded_by_a_pursue(authority_schema: str) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    run_id = uuid4()
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        release = bootstrap_prompt_release(connection)
        _insert_prompt_run(connection, run_id, release.id, now)
        first = _insert_review_decision(connection, run_id, release.id, now, 1, "qualified")
        second = _insert_review_decision(connection, run_id, release.id, now, 2, "qualified")
        assert enqueue_qualified_review_item(connection, first, now.date())
        assert enqueue_qualified_review_item(connection, second, now.date())
        first_item, second_item = load_review_queue(connection).items

        blocked = record_review(
            connection,
            ReviewSubmission(
                review_item_id=first_item.id,
                evaluation_id=first_item.evaluation_id,
                snapshot_id=first_item.snapshot_id,
                decision="pursue",
                block_company=True,
                actor="owner",
                created_at=now,
            ),
        )
        pursued = record_review(
            connection,
            ReviewSubmission(
                review_item_id=second_item.id,
                evaluation_id=second_item.evaluation_id,
                snapshot_id=second_item.snapshot_id,
                decision="pursue",
                actor="owner",
                created_at=now + timedelta(minutes=5),
            ),
        )

        assert isinstance(blocked, ReviewSaved)
        assert isinstance(pursued, ReviewSaved)
        policy = connection.execute(
            "SELECT policy, effective_at, expires_at FROM company_policies"
        ).fetchone()
        assert policy == ("blocked", now, None)
        assert connection.execute("SELECT count(*) FROM application_events").fetchone() == (2,)


def test_a_snapshot_correction_replaces_the_broken_body_and_adds_compensation(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    run_id = uuid4()
    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        release = bootstrap_prompt_release(connection)
        _insert_prompt_run(connection, run_id, release.id, now)
        _ = _insert_review_decision(connection, run_id, release.id, now, 1, "qualified")
        assert enqueue_qualified_review_item(connection, f"{1001:064x}", now.date())
        thin = load_review_queue(connection).items[0]
        assert len(thin.job.description) < 500
        _ = connection.execute(
            """
            INSERT INTO snapshot_corrections (
              snapshot_id, description, compensation_min, compensation_max,
              compensation_currency, compensation_period, compensation_source,
              reason, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                thin.snapshot_id,
                "## Your mission\n" + "Own features end to end. " * 40,
                Decimal("80000"),
                Decimal("100000"),
                "EUR",
                "year",
                "ats",
                "ats backfill",
                now,
            ),
        )

        queue = load_review_queue(connection)

    assert queue.items[0].job.description.startswith("## Your mission")
    compensation = queue.items[0].job.compensation
    assert compensation is not None
    assert compensation.minimum == Decimal("80000")
    assert compensation.maximum == Decimal("100000")
    assert compensation.currency == "EUR"
    assert compensation.source == "ats"
