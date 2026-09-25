from __future__ import annotations

from collections.abc import Generator, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5
from threading import Barrier

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb
import pytest

from job_finder.config import PostgresContractSettings
from job_finder.database import apply_migrations
from job_finder.discovery.exchange_rates import ExchangeRateSnapshot
from job_finder.evaluation.prompt_releases import bootstrap_prompt_release
from job_finder.evaluation.release_targets import get_active_release_target
from job_finder.configuration_service import load_published_active_search_configuration
from job_finder.pipeline.work_items import claim_next_job
from job_finder.pipeline.runs import prepare_orchestration_run
from job_finder.review.analytics import load_spend_analytics
from job_finder.pipeline.work_dismissals import (
    DismissalAction,
    WorkDismissalApplied,
    WorkDismissalCommand,
    WorkDismissalKeyConflict,
    WorkDismissalNotFound,
    WorkDismissalResult,
    WorkDismissalStaleState,
    dismiss_work,
)
from job_finder.pipeline.work_recoveries import (
    RecoveryAction,
    WorkRecoveryActiveLease,
    WorkRecoveryApplied,
    WorkRecoveryCommand,
    WorkRecoveryKeyConflict,
    WorkRecoveryResult,
    WorkRecoveryStaleState,
    recover_work,
)
from job_finder.pipeline.reevaluations import (
    JobReevaluationAccepted,
    JobReevaluationActiveWork,
    JobReevaluationCommand,
    JobReevaluationKeyConflict,
    request_job_reevaluation,
)
from job_finder.review.operations import (
    ActivityPage,
    ActivityQuery,
    ActivityRun,
    WorkItemNotFound,
    OperationsHealth,
    RunNotFound,
    load_activity_page,
    load_operations_snapshot,
    load_work_item_detail,
    load_pipeline_runs,
    load_run_detail,
)


@pytest.fixture
def authority_schema() -> Iterator[str]:
    settings = PostgresContractSettings.from_environment()
    schema_name = f"job_finder_operations_contract_{uuid4().hex}"
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
        try:
            yield schema_name
        finally:
            connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema_name))
            )


def test_operations_snapshot_reads_authoritative_postgres_state(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 21, 12, tzinfo=UTC)
    run_id = uuid4()
    processing_attempt_id = uuid4()
    terminal_job_id = uuid4()
    pending_job_id = uuid4()

    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        release = bootstrap_prompt_release(connection)
        prompt_name, prompt_version_id = connection.execute(
            """
            SELECT prompt_name, prompt_version_id
            FROM prompt_release_members
            WHERE release_id = %s
            ORDER BY prompt_name
            LIMIT 1
            """,
            (release.id,),
        ).fetchone() or pytest.fail("bootstrap release has no members")
        connection.execute(
            """
            INSERT INTO pipeline_runs (
              id, idempotency_key, kind, implementation_ref, prompt_release_id,
              parameters, status, started_at, completed_at
            ) VALUES (%s, %s, 'processing', 'contract-ref', %s, '{}'::jsonb,
              'completed', %s, %s)
            """,
            (run_id, f"operations:{run_id}", release.id, now - timedelta(minutes=5), now),
        )
        connection.execute(
            """
            INSERT INTO processing_attempts (
              id, pipeline_run_id, operation_key, attempt_number, input_digest,
              status, started_at, completed_at
            ) VALUES (%s, %s, 'operations-test', 0, %s, 'completed', %s, %s)
            """,
            (processing_attempt_id, run_id, "a" * 64, now - timedelta(minutes=4), now),
        )
        connection.execute(
            """
            INSERT INTO model_call_attempts (
              id, processing_attempt_id, pipeline_run_id, prompt_release_id,
              request_id, attempt_number, operation_key, prompt_name,
              prompt_version_id, input_digest, requested_model, provider,
              status, parsed_output, raw_response, input_tokens, output_tokens,
              cost_usd, latency_ms, observed_at, response_model, request_messages
            ) VALUES (
              %s, %s, %s, %s, %s, 0, 'operations-test', %s, %s, %s,
              'test-model', 'typesafe', 'accepted', '{}'::jsonb, '{}'::jsonb,
              10, 5, 1.25000000, 25, %s, 'test-model', '[]'::jsonb
            )
            """,
            (
                uuid4(),
                processing_attempt_id,
                run_id,
                release.id,
                "b" * 64,
                prompt_name,
                prompt_version_id,
                "a" * 64,
                now - timedelta(minutes=3),
            ),
        )
        connection.execute(
            """
            INSERT INTO model_call_attempts (
              id, processing_attempt_id, pipeline_run_id, prompt_release_id,
              request_id, attempt_number, operation_key, prompt_name,
              prompt_version_id, input_digest, requested_model, provider,
              status, latency_ms, error, observed_at, request_messages
            ) VALUES (
              %s, %s, %s, %s, %s, 0, 'operations-test', %s, %s, %s,
              'test-model', 'typesafe', 'retryable_error', 50,
              '{"code":"provider_timeout","reason":"Provider did not respond"}'::jsonb,
              %s, '[]'::jsonb
            )
            """,
            (
                uuid4(),
                processing_attempt_id,
                run_id,
                release.id,
                "c" * 64,
                prompt_name,
                prompt_version_id,
                "a" * 64,
                now - timedelta(minutes=2),
            ),
        )
        connection.execute(
            """
            INSERT INTO jobs (id, raw_url, first_discovered_at, last_discovered_at)
            VALUES (%s, 'https://example.com/terminal', %s, %s),
                   (%s, 'https://example.com/pending', %s, %s)
            """,
            (terminal_job_id, now, now, pending_job_id, now, now),
        )
        connection.execute(
            """
            INSERT INTO job_work_items (
              job_id, discovery_run_id, keyword, state, last_error, created_at, completed_at
            ) VALUES (
              %s, %s, 'python', 'terminal_error',
              '{"retryability":"terminal","code":"invalid_job","reason":"Job is invalid"}'::jsonb,
              %s, %s
            )
            """,
            (terminal_job_id, run_id, now, now + timedelta(minutes=1)),
        )
        connection.execute(
            """
            INSERT INTO job_work_items (job_id, discovery_run_id, keyword, state, created_at)
            VALUES (%s, %s, 'python', 'pending', %s)
            """,
            (pending_job_id, run_id, now),
        )

        snapshot = load_operations_snapshot(connection, recent_run_limit=1, failure_limit=1)

    assert snapshot.health is OperationsHealth.ACTION_REQUIRED
    assert snapshot.queues.pending == 1
    assert snapshot.queues.terminal_error == 1
    assert snapshot.spend.known_usd == Decimal("1.25000000")
    assert snapshot.spend.unknown_attempts == 1
    assert len(snapshot.recent_runs) == 1
    assert snapshot.recent_runs[0].id == run_id
    assert len(snapshot.failures) == 1
    assert snapshot.failures[0].source == "job"
    assert snapshot.failures[0].summary == "invalid_job: Job is invalid"
    assert len(snapshot.actionable_work) == 1
    assert snapshot.actionable_work[0].job_id == terminal_job_id
    assert snapshot.actionable_work[0].state == "terminal_error"
    assert snapshot.actionable_work[0].attempt_count == 0
    assert snapshot.actionable_work[0].retry_at is None
    assert snapshot.actionable_work[0].failed_at == now + timedelta(minutes=1)
    assert snapshot.actionable_work[0].failure_summary == "invalid_job: Job is invalid"


def test_work_recovery_is_atomic_replay_safe_and_preserves_prior_evidence(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 21, 12, tzinfo=UTC)
    retry_at = now + timedelta(hours=2)
    lease_expires_at = now + timedelta(minutes=30)
    run_id = uuid4()
    failed_job_id = uuid4()
    terminal_job_id = uuid4()
    pending_job_id = uuid4()
    leased_job_id = uuid4()
    concurrent_job_id = uuid4()
    owner_token = uuid4()
    retry_error = {
        "retryability": "retryable",
        "code": "provider_timeout",
        "reason": "Provider did not respond",
    }
    terminal_error = {
        "retryability": "terminal",
        "code": "invalid_job",
        "reason": "Job is invalid",
    }

    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        release = bootstrap_prompt_release(connection)
        connection.execute(
            """
            INSERT INTO pipeline_runs (
              id, idempotency_key, kind, implementation_ref, prompt_release_id,
              parameters, status, started_at, completed_at
            ) VALUES (%s, %s, 'processing', 'recovery-contract', %s, '{}'::jsonb,
              'completed', %s, %s)
            """,
            (run_id, f"recovery:{run_id}", release.id, now - timedelta(hours=1), now),
        )
        connection.execute(
            """
            INSERT INTO jobs (id, raw_url, first_discovered_at, last_discovered_at)
            VALUES (%s, 'https://example.com/failed', %s, %s),
                   (%s, 'https://example.com/terminal', %s, %s),
                   (%s, 'https://example.com/pending', %s, %s),
                   (%s, 'https://example.com/leased', %s, %s)
            """,
            (
                failed_job_id,
                now,
                now,
                terminal_job_id,
                now,
                now,
                pending_job_id,
                now,
                now,
                leased_job_id,
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO jobs (id, raw_url, first_discovered_at, last_discovered_at)
            VALUES (%s, 'https://example.com/concurrent', %s, %s)
            """,
            (concurrent_job_id, now, now),
        )
        connection.execute(
            """
            INSERT INTO job_work_items (
              job_id, discovery_run_id, keyword, state, attempt_count,
              retry_at, last_error, last_failed_at, created_at
            ) VALUES (%s, %s, 'python', 'failed', 1, %s, %s, %s, %s)
            """,
            (
                concurrent_job_id,
                run_id,
                retry_at,
                Jsonb(retry_error),
                now - timedelta(minutes=2),
                now - timedelta(hours=1),
            ),
        )
        connection.execute(
            """
            INSERT INTO job_work_items (
              job_id, discovery_run_id, keyword, state, attempt_count,
              owner_token, lease_expires_at, retry_at, last_error,
              created_at, completed_at
            ) VALUES
              (%s, %s, 'python', 'failed', 2, NULL, NULL, %s, %s, %s, NULL),
              (%s, %s, 'python', 'terminal_error', 5, NULL, NULL, NULL, %s, %s, %s),
              (%s, %s, 'python', 'pending', 0, NULL, NULL, NULL, NULL, %s, NULL),
              (%s, %s, 'python', 'leased', 2, %s, %s, NULL, NULL, %s, NULL)
            """,
            (
                failed_job_id,
                run_id,
                retry_at,
                Jsonb(retry_error),
                now - timedelta(hours=1),
                terminal_job_id,
                run_id,
                Jsonb(terminal_error),
                now - timedelta(hours=1),
                now - timedelta(minutes=5),
                pending_job_id,
                run_id,
                now - timedelta(hours=1),
                leased_job_id,
                run_id,
                owner_token,
                lease_expires_at,
                now - timedelta(hours=1),
            ),
        )

        retry_command = WorkRecoveryCommand(
            idempotency_key="retry-failed-now",
            job_id=failed_job_id,
            action=RecoveryAction.RETRY_NOW,
            expected_state="failed",
            expected_attempt_count=2,
            actor="owner",
            requested_at=now,
        )
        applied_retry = recover_work(connection, retry_command)
        replayed_retry = recover_work(
            connection,
            WorkRecoveryCommand(
                idempotency_key=retry_command.idempotency_key,
                job_id=retry_command.job_id,
                action=retry_command.action,
                expected_state=retry_command.expected_state,
                expected_attempt_count=retry_command.expected_attempt_count,
                actor=retry_command.actor,
                requested_at=now + timedelta(minutes=1),
            ),
        )
        key_conflict = recover_work(
            connection,
            WorkRecoveryCommand(
                idempotency_key=retry_command.idempotency_key,
                job_id=pending_job_id,
                action=RecoveryAction.RETRY_NOW,
                expected_state="failed",
                expected_attempt_count=0,
                actor="owner",
                requested_at=now,
            ),
        )
        applied_terminal = recover_work(
            connection,
            WorkRecoveryCommand(
                idempotency_key="recover-terminal",
                job_id=terminal_job_id,
                action=RecoveryAction.RECOVER_TERMINAL,
                expected_state="terminal_error",
                expected_attempt_count=5,
                actor="owner",
                requested_at=now + timedelta(seconds=1),
            ),
        )
        stale = recover_work(
            connection,
            WorkRecoveryCommand(
                idempotency_key="stale-pending",
                job_id=pending_job_id,
                action=RecoveryAction.RETRY_NOW,
                expected_state="failed",
                expected_attempt_count=0,
                actor="owner",
                requested_at=now,
            ),
        )
        active_lease = recover_work(
            connection,
            WorkRecoveryCommand(
                idempotency_key="active-lease",
                job_id=leased_job_id,
                action=RecoveryAction.RETRY_NOW,
                expected_state="failed",
                expected_attempt_count=2,
                actor="owner",
                requested_at=now,
            ),
        )
        stale_generation = recover_work(
            connection,
            WorkRecoveryCommand(
                idempotency_key="stale-generation",
                job_id=failed_job_id,
                action=RecoveryAction.RETRY_NOW,
                expected_state="failed",
                expected_attempt_count=1,
                actor="owner",
                requested_at=now,
            ),
        )

        concurrent_command = WorkRecoveryCommand(
            idempotency_key="concurrent-retry",
            job_id=concurrent_job_id,
            action=RecoveryAction.RETRY_NOW,
            expected_state="failed",
            expected_attempt_count=1,
            actor="owner",
            requested_at=now,
        )
        barrier = Barrier(2)

        def concurrent_recovery(_index: int) -> WorkRecoveryResult:
            with _connection(authority_schema) as concurrent_connection:
                _ = barrier.wait()
                return recover_work(concurrent_connection, concurrent_command)

        with ThreadPoolExecutor(max_workers=2) as executor:
            concurrent_results = tuple(executor.map(concurrent_recovery, range(2)))

        assert isinstance(applied_retry, WorkRecoveryApplied)
        assert applied_retry.replayed is False
        assert applied_retry.receipt.prior_attempt_count == 2
        assert applied_retry.receipt.prior_retry_at == retry_at
        assert applied_retry.receipt.prior_error == retry_error
        assert applied_retry.receipt.resulting_retry_at == now
        assert isinstance(replayed_retry, WorkRecoveryApplied)
        assert replayed_retry.replayed is True
        assert replayed_retry.receipt == applied_retry.receipt
        assert isinstance(key_conflict, WorkRecoveryKeyConflict)

        assert isinstance(applied_terminal, WorkRecoveryApplied)
        assert applied_terminal.receipt.prior_attempt_count == 5
        assert applied_terminal.receipt.prior_error == terminal_error
        assert applied_terminal.receipt.resulting_state == "pending"
        assert applied_terminal.receipt.resulting_attempt_count == 0
        assert isinstance(stale, WorkRecoveryStaleState)
        assert stale.receipt.prior_state == "pending"
        assert isinstance(active_lease, WorkRecoveryActiveLease)
        assert active_lease.receipt.prior_state == "leased"
        assert isinstance(stale_generation, WorkRecoveryStaleState)
        assert stale_generation.receipt.prior_attempt_count == 2
        assert all(isinstance(result, WorkRecoveryApplied) for result in concurrent_results)
        assert sorted(
            result.replayed
            for result in concurrent_results
            if isinstance(result, WorkRecoveryApplied)
        ) == [False, True]

        assert connection.execute(
            "SELECT state, attempt_count, retry_at, last_error FROM job_work_items WHERE job_id = %s",
            (failed_job_id,),
        ).fetchone() == ("failed", 2, now, retry_error)
        assert connection.execute(
            """
            SELECT state, attempt_count, retry_at, last_error, completed_at
            FROM job_work_items WHERE job_id = %s
            """,
            (terminal_job_id,),
        ).fetchone() == ("pending", 0, None, None, None)
        assert connection.execute(
            """
            SELECT state, attempt_count, owner_token, lease_expires_at
            FROM job_work_items WHERE job_id = %s
            """,
            (leased_job_id,),
        ).fetchone() == ("leased", 2, owner_token, lease_expires_at)
        assert connection.execute("SELECT count(*) FROM work_recovery_receipts").fetchone() == (6,)
        with pytest.raises(psycopg.errors.CheckViolation, match="immutable"):
            connection.execute(
                "UPDATE work_recovery_receipts SET actor = 'tampered' WHERE idempotency_key = %s",
                (retry_command.idempotency_key,),
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                """
                INSERT INTO work_recovery_receipts (
                  idempotency_key, job_id, action, expected_state, actor, requested_at,
                  expected_attempt_count, outcome
                ) VALUES ('invalid-action-state', %s, 'retry_now', 'terminal_error',
                  'owner', %s, 0, 'not_found')
                """,
                (uuid4(), now),
            )


def test_work_dismissal_is_atomic_replay_safe_and_undoable(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 21, 12, tzinfo=UTC)
    run_id = uuid4()
    terminal_job_id = uuid4()
    retrying_job_id = uuid4()
    terminal_error = {
        "retryability": "terminal",
        "code": "invalid_job",
        "reason": "Job is invalid",
    }
    retry_error = {
        "retryability": "retryable",
        "code": "provider_timeout",
        "reason": "Provider did not respond",
    }

    def dismiss(job_id: UUID, attempt_count: int, key: str) -> WorkDismissalResult:
        return dismiss_work(
            connection,
            WorkDismissalCommand(
                idempotency_key=key,
                job_id=job_id,
                action=DismissalAction.DISMISS,
                expected_attempt_count=attempt_count,
                actor="owner",
                requested_at=now,
            ),
        )

    def undo(job_id: UUID, attempt_count: int, key: str) -> WorkDismissalResult:
        return dismiss_work(
            connection,
            WorkDismissalCommand(
                idempotency_key=key,
                job_id=job_id,
                action=DismissalAction.UNDO_DISMISS,
                expected_attempt_count=attempt_count,
                actor="owner",
                requested_at=now + timedelta(minutes=1),
            ),
        )

    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        release = bootstrap_prompt_release(connection)
        connection.execute(
            """
            INSERT INTO pipeline_runs (
              id, idempotency_key, kind, implementation_ref, prompt_release_id,
              parameters, status, started_at, completed_at
            ) VALUES (%s, %s, 'processing', 'dismissal-contract', %s, '{}'::jsonb,
              'completed', %s, %s)
            """,
            (run_id, f"dismissal:{run_id}", release.id, now - timedelta(hours=1), now),
        )
        connection.execute(
            """
            INSERT INTO jobs (id, raw_url, first_discovered_at, last_discovered_at)
            VALUES (%s, 'https://example.com/dismiss-terminal', %s, %s),
                   (%s, 'https://example.com/dismiss-retrying', %s, %s)
            """,
            (
                terminal_job_id,
                now,
                now,
                retrying_job_id,
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO job_work_items (
              job_id, discovery_run_id, keyword, state, attempt_count,
              retry_at, last_error, last_failed_at, created_at, completed_at
            ) VALUES (%s, %s, 'python', 'terminal_error', 3, NULL, %s, %s, %s, %s),
                     (%s, %s, 'python', 'failed', 1, %s, %s, %s, %s, NULL)
            """,
            (
                terminal_job_id,
                run_id,
                Jsonb(terminal_error),
                now - timedelta(hours=1),
                now - timedelta(hours=1),
                now - timedelta(minutes=5),
                retrying_job_id,
                run_id,
                now + timedelta(hours=2),
                Jsonb(retry_error),
                now - timedelta(hours=1),
                now - timedelta(hours=1),
            ),
        )

        before = load_operations_snapshot(connection)
        assert before.health is OperationsHealth.ACTION_REQUIRED
        assert before.dismissed_terminal == 0

        applied = dismiss(terminal_job_id, 3, "dismiss-terminal")
        assert isinstance(applied, WorkDismissalApplied)
        assert applied.replayed is False

        replayed = dismiss(terminal_job_id, 3, "dismiss-terminal")
        assert isinstance(replayed, WorkDismissalApplied)
        assert replayed.replayed is True

        key_conflict = dismiss(retrying_job_id, 3, "dismiss-terminal")
        assert isinstance(key_conflict, WorkDismissalKeyConflict)

        mid = load_operations_snapshot(connection)
        # the retrying item keeps health at WORKING; the terminal dismissal
        # only removes its ACTION_REQUIRED contribution
        assert mid.health is OperationsHealth.WORKING
        assert mid.dismissed_terminal == 1
        mid_terminal = next(item for item in mid.actionable_work if item.state == "terminal_error")
        assert mid_terminal.dismissed is True

        stale_undo = undo(terminal_job_id, 2, "undo-stale")
        assert isinstance(stale_undo, WorkDismissalStaleState)

        applied_undo = undo(terminal_job_id, 3, "undo-terminal")
        assert isinstance(applied_undo, WorkDismissalApplied)

        replayed_undo = undo(terminal_job_id, 3, "undo-terminal")
        assert isinstance(replayed_undo, WorkDismissalApplied)
        assert replayed_undo.replayed is True

        never_dismissed = undo(terminal_job_id, 3, "undo-never-dismissed")
        assert isinstance(never_dismissed, WorkDismissalNotFound)
        never_dismissed_row = connection.execute(
            """
            SELECT outcome, prior_state, prior_attempt_count, resulting_attempt_count
            FROM work_dismissal_receipts WHERE idempotency_key = 'undo-never-dismissed'
            """
        ).fetchone()
        assert never_dismissed_row == ("not_found", None, None, None)
        replayed_never_dismissed = undo(terminal_job_id, 3, "undo-never-dismissed")
        assert isinstance(replayed_never_dismissed, WorkDismissalNotFound)
        assert replayed_never_dismissed.replayed is True

        after = load_operations_snapshot(connection)
        assert after.health is OperationsHealth.ACTION_REQUIRED
        assert after.dismissed_terminal == 0
        after_terminal = next(
            item for item in after.actionable_work if item.state == "terminal_error"
        )
        assert after_terminal.dismissed is False

        stale_state = dismiss(retrying_job_id, 1, "dismiss-retrying")
        assert isinstance(stale_state, WorkDismissalStaleState)

        not_found = dismiss(uuid4(), 0, "dismiss-missing")
        assert isinstance(not_found, WorkDismissalNotFound)

        dismissed_again = dismiss(terminal_job_id, 3, "dismiss-again")
        assert isinstance(dismissed_again, WorkDismissalApplied)
        connection.execute(
            "UPDATE job_work_items SET attempt_count = 2 WHERE job_id = %s",
            (terminal_job_id,),
        )
        stale_dismissal_undo = undo(terminal_job_id, 2, "undo-stale-dismissal")
        assert isinstance(stale_dismissal_undo, WorkDismissalNotFound)
        stale_dismissal_row = connection.execute(
            """
            SELECT outcome, prior_state, prior_attempt_count, resulting_attempt_count
            FROM work_dismissal_receipts WHERE idempotency_key = 'undo-stale-dismissal'
            """
        ).fetchone()
        assert stale_dismissal_row == ("not_found", None, None, None)


def test_operations_health_follows_real_postgres_state_transitions(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 21, 12, tzinfo=UTC)
    run_id = uuid4()

    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        release = bootstrap_prompt_release(connection)

        empty = load_operations_snapshot(connection)

        connection.execute(
            """
            INSERT INTO pipeline_runs (
              id, idempotency_key, kind, implementation_ref, prompt_release_id,
              parameters, status, started_at
            ) VALUES (%s, %s, 'processing', 'contract-ref', %s, '{}'::jsonb,
              'running', %s)
            """,
            (run_id, f"operations-health:{run_id}", release.id, now),
        )
        working = load_operations_snapshot(connection)

        connection.execute(
            "UPDATE pipeline_runs SET status = 'completed', completed_at = %s WHERE id = %s",
            (now + timedelta(minutes=2), run_id),
        )
        caught_up = load_operations_snapshot(connection)

    assert empty.health is OperationsHealth.UNKNOWN
    assert empty.recent_runs == ()
    assert working.health is OperationsHealth.WORKING
    assert [run.status for run in working.recent_runs] == ["running"]
    assert caught_up.health is OperationsHealth.CAUGHT_UP
    assert [run.status for run in caught_up.recent_runs] == ["completed"]


def test_job_reevaluation_is_append_only_replay_safe_and_pins_the_active_target(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 22, 12, tzinfo=UTC)
    job_id = uuid4()
    snapshot_id = "a" * 64
    decision_id = "b" * 64
    review_item_id = uuid4()

    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        active_configuration = load_published_active_search_configuration(connection)
        active_target = get_active_release_target(connection)
        run = prepare_orchestration_run(
            connection,
            idempotency_key="reevaluation-source",
            implementation_ref="source-ref",
            configuration_revision_id=active_configuration.publication.revision_id,
            target=active_target.target,
            started_at=now - timedelta(hours=1),
            fetch_rates=lambda: ExchangeRateSnapshot(
                rates={"EUR": Decimal("1.1")},
                source="frankfurter",
                observed_at=now - timedelta(hours=1),
            ),
        )
        connection.execute(
            """
            INSERT INTO jobs (id, raw_url, first_discovered_at, last_discovered_at)
            VALUES (%s, 'https://example.com/reevaluate', %s, %s)
            """,
            (job_id, now - timedelta(hours=1), now),
        )
        connection.execute(
            """
            INSERT INTO job_snapshots (
              id, job_id, content_digest, title, company, normalized_company,
              normalized_title, source, raw_url, description, location, keywords,
              observed_at
            ) VALUES (%s, %s, %s, 'Applied AI Engineer', 'Acme', 'acme',
              'applied ai engineer', 'other', 'https://example.com/reevaluate',
              %s, 'Remote', '["python"]'::jsonb, %s)
            """,
            (snapshot_id, job_id, "c" * 64, "Build useful AI products. " * 30, now),
        )
        connection.execute(
            """
            INSERT INTO evaluation_decisions (
              id, snapshot_id, pipeline_run_id, prompt_release_id,
              relevance_release_id, policy_version, outcome, matched_profile,
              reason, created_at, decision_stage
            ) VALUES (%s, %s, %s, %s, %s, 'orchestration-v1', 'qualified',
              'applied-ai-product-engineer', 'Strong fit', %s, 'qualified')
            """,
            (
                decision_id,
                snapshot_id,
                run.id,
                run.target.prompt_release_id,
                run.target.relevance_release_id,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO review_items (id, evaluation_id, review_day, lane, position, created_at)
            VALUES (%s, %s, %s, 'qualified', 0, %s)
            """,
            (review_item_id, decision_id, now.date(), now),
        )
        connection.execute(
            """
            INSERT INTO review_events (
              id, review_item_id, decision, target_profile, primary_reason,
              block_company, actor, created_at
            ) VALUES (%s, %s, 'unsure', 'applied-ai-product-engineer', 'other',
              false, 'owner', %s)
            """,
            (uuid4(), review_item_id, now),
        )
        connection.execute(
            """
            INSERT INTO job_work_items (
              job_id, discovery_run_id, keyword, state, terminal_decision_id,
              created_at, completed_at
            ) VALUES (%s, %s, 'python', 'completed', %s, %s, %s)
            """,
            (job_id, run.id, decision_id, now - timedelta(hours=1), now),
        )
        command = JobReevaluationCommand(
            idempotency_key="reevaluate-once",
            expected_decision_id=decision_id,
            expected_snapshot_id=snapshot_id,
            actor="owner",
            requested_at=now + timedelta(minutes=1),
        )

        accepted = request_job_reevaluation(connection, command)
        replayed = request_job_reevaluation(
            connection,
            JobReevaluationCommand(
                idempotency_key=command.idempotency_key,
                expected_decision_id=decision_id,
                expected_snapshot_id=snapshot_id,
                actor="owner",
                requested_at=now + timedelta(minutes=2),
            ),
        )
        key_conflict = request_job_reevaluation(
            connection,
            JobReevaluationCommand(
                idempotency_key=command.idempotency_key,
                expected_decision_id="d" * 64,
                expected_snapshot_id=snapshot_id,
                actor="owner",
                requested_at=now + timedelta(minutes=2),
            ),
        )
        active_work = request_job_reevaluation(
            connection,
            JobReevaluationCommand(
                idempotency_key="reevaluate-again",
                expected_decision_id=decision_id,
                expected_snapshot_id=snapshot_id,
                actor="owner",
                requested_at=now + timedelta(minutes=2),
            ),
        )

        concurrent_job_id = uuid4()
        concurrent_snapshot_id = "e" * 64
        concurrent_decision_id = "f" * 64
        connection.execute(
            """
            INSERT INTO jobs (id, raw_url, first_discovered_at, last_discovered_at)
            VALUES (%s, 'https://example.com/concurrent-reevaluation', %s, %s)
            """,
            (concurrent_job_id, now - timedelta(hours=1), now),
        )
        connection.execute(
            """
            INSERT INTO job_snapshots (
              id, job_id, content_digest, title, company, normalized_company,
              normalized_title, source, raw_url, description, location, keywords,
              observed_at
            ) VALUES (%s, %s, %s, 'ML Engineer', 'Beta', 'beta', 'ml engineer',
              'other', 'https://example.com/concurrent-reevaluation', %s,
              'Remote', '["python"]'::jsonb, %s)
            """,
            (
                concurrent_snapshot_id,
                concurrent_job_id,
                "1" * 64,
                "Build reliable ML products. " * 30,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO evaluation_decisions (
              id, snapshot_id, pipeline_run_id, prompt_release_id,
              relevance_release_id, policy_version, outcome, matched_profile,
              reason, created_at, decision_stage
            ) VALUES (%s, %s, %s, %s, %s, 'orchestration-v1', 'qualified',
              'applied-ai-product-engineer', 'Strong fit', %s, 'qualified')
            """,
            (
                concurrent_decision_id,
                concurrent_snapshot_id,
                run.id,
                run.target.prompt_release_id,
                run.target.relevance_release_id,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO job_work_items (
              job_id, discovery_run_id, keyword, state, terminal_decision_id,
              created_at, completed_at
            ) VALUES (%s, %s, 'python', 'completed', %s, %s, %s)
            """,
            (
                concurrent_job_id,
                run.id,
                concurrent_decision_id,
                now - timedelta(hours=1),
                now,
            ),
        )
        barrier = Barrier(2)

        def concurrent_request(index: int) -> object:
            with _connection(authority_schema) as concurrent_connection:
                _ = barrier.wait()
                return request_job_reevaluation(
                    concurrent_connection,
                    JobReevaluationCommand(
                        idempotency_key=f"concurrent-reevaluation-{index}",
                        expected_decision_id=concurrent_decision_id,
                        expected_snapshot_id=concurrent_snapshot_id,
                        actor="owner",
                        requested_at=now + timedelta(minutes=3),
                    ),
                )

        with ThreadPoolExecutor(max_workers=2) as executor:
            concurrent_results = tuple(executor.map(concurrent_request, range(2)))

        assert isinstance(accepted, JobReevaluationAccepted)
        assert accepted.replayed is False
        assert accepted.receipt.prompt_release_id == run.target.prompt_release_id
        assert accepted.receipt.relevance_release_id == run.target.relevance_release_id
        assert accepted.receipt.source_pipeline_run_id == run.id
        assert accepted.receipt.reevaluation_pipeline_run_id == uuid5(
            NAMESPACE_URL, "job-reevaluation-run:reevaluate-once"
        )
        assert isinstance(replayed, JobReevaluationAccepted)
        assert replayed.replayed is True
        assert replayed.receipt == accepted.receipt
        assert isinstance(key_conflict, JobReevaluationKeyConflict)
        assert isinstance(active_work, JobReevaluationActiveWork)
        assert active_work.receipt.observed_work_state == "pending"
        assert (
            sum(isinstance(result, JobReevaluationAccepted) for result in concurrent_results) == 1
        )
        assert (
            sum(isinstance(result, JobReevaluationActiveWork) for result in concurrent_results) == 1
        )

        assert connection.execute(
            """
            SELECT state, attempt_count, terminal_decision_id, active_reevaluation_key
            FROM job_work_items WHERE job_id = %s
            """,
            (job_id,),
        ).fetchone() == ("pending", 0, None, command.idempotency_key)
        expired_owner = uuid4()
        connection.execute(
            """
            UPDATE job_work_items
            SET state = 'leased', attempt_count = 3, owner_token = %s,
                lease_expires_at = %s
            WHERE job_id = %s
            """,
            (expired_owner, now + timedelta(minutes=3), job_id),
        )
        _ = claim_next_job(
            connection,
            owner_token=uuid4(),
            claimed_at=now + timedelta(minutes=4),
            lease_for=timedelta(minutes=1),
        )
        assert connection.execute(
            """
            SELECT state, last_error->>'code' FROM job_work_items WHERE job_id = %s
            """,
            (job_id,),
        ).fetchone() == ("terminal_error", "lease_expired")
        assert connection.execute(
            "SELECT status, error->>'code' FROM pipeline_runs WHERE id = %s",
            (accepted.receipt.reevaluation_pipeline_run_id,),
        ).fetchone() == ("failed", "lease_expired")
        with pytest.raises(psycopg.errors.CheckViolation, match="provenance is immutable"):
            connection.execute(
                "UPDATE pipeline_runs SET implementation_ref = 'tampered' WHERE id = %s",
                (accepted.receipt.reevaluation_pipeline_run_id,),
            )
        assert connection.execute("SELECT count(*) FROM evaluation_decisions").fetchone() == (2,)
        assert connection.execute("SELECT count(*) FROM review_items").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM review_events").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM job_reevaluation_requests").fetchone() == (
            4,
        )
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            connection.execute(
                """
                UPDATE job_work_items
                SET active_reevaluation_key = %s
                WHERE job_id = %s
                """,
                (command.idempotency_key, concurrent_job_id),
            )
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            connection.execute(
                """
                INSERT INTO evaluation_decisions (
                  id, snapshot_id, pipeline_run_id, prompt_release_id,
                  relevance_release_id, policy_version, outcome, matched_profile,
                  reason, created_at, decision_stage, source_snapshot_id,
                  predecessor_decision_id, reevaluation_request_key
                ) VALUES (%s, %s, %s, %s, %s, 'orchestration-v1', 'qualified',
                  'applied-ai-product-engineer', 'Invalid cross-link', %s, 'qualified',
                  %s, %s, %s)
                """,
                (
                    "2" * 64,
                    snapshot_id,
                    accepted.receipt.reevaluation_pipeline_run_id,
                    accepted.receipt.prompt_release_id,
                    accepted.receipt.relevance_release_id,
                    now,
                    snapshot_id,
                    concurrent_decision_id,
                    command.idempotency_key,
                ),
            )
        with pytest.raises(
            psycopg.errors.ForeignKeyViolation,
            match="output must belong to the requested job",
        ):
            connection.execute(
                """
                INSERT INTO evaluation_decisions (
                  id, snapshot_id, pipeline_run_id, prompt_release_id,
                  relevance_release_id, policy_version, outcome, matched_profile,
                  reason, created_at, decision_stage, source_snapshot_id,
                  predecessor_decision_id, reevaluation_request_key
                ) VALUES (%s, %s, %s, %s, %s, 'orchestration-v1', 'qualified',
                  'applied-ai-product-engineer', 'Invalid output job', %s, 'qualified',
                  %s, %s, %s)
                """,
                (
                    "3" * 64,
                    concurrent_snapshot_id,
                    accepted.receipt.reevaluation_pipeline_run_id,
                    accepted.receipt.prompt_release_id,
                    accepted.receipt.relevance_release_id,
                    now,
                    snapshot_id,
                    decision_id,
                    command.idempotency_key,
                ),
            )
        with pytest.raises(psycopg.errors.CheckViolation, match="immutable"):
            connection.execute(
                """
                UPDATE job_reevaluation_requests SET actor = 'tampered'
                WHERE idempotency_key = %s
                """,
                (command.idempotency_key,),
            )


@contextmanager
def _connection(
    schema_name: str,
) -> Generator[psycopg.Connection[tuple[object, ...]], None, None]:
    settings = PostgresContractSettings.from_environment()
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema_name)))
        yield connection


def test_pipeline_run_reads_expose_counts_costs_and_children(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 21, 12, tzinfo=UTC)
    run_id = uuid4()
    idle_run_id = uuid4()
    job_id = uuid4()
    attempt_id = uuid4()

    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        release = bootstrap_prompt_release(connection)
        prompt_name, prompt_version_id = connection.execute(
            """
            SELECT prompt_name, prompt_version_id
            FROM prompt_release_members
            WHERE release_id = %s
            ORDER BY prompt_name
            LIMIT 1
            """,
            (release.id,),
        ).fetchone() or pytest.fail("bootstrap release has no members")
        for run, key, started, completed, kind in (
            (
                run_id,
                f"runs:{run_id}",
                now - timedelta(minutes=10),
                now - timedelta(minutes=9),
                "discovery",
            ),
            (
                idle_run_id,
                f"runs:{idle_run_id}",
                now - timedelta(minutes=5),
                now - timedelta(minutes=5),
                "discovery",
            ),
        ):
            connection.execute(
                """
                INSERT INTO pipeline_runs (
                  id, idempotency_key, kind, implementation_ref, prompt_release_id,
                  parameters, status, started_at, completed_at
                ) VALUES (%s, %s, %s, 'runs-contract', %s, '{"source": "contract"}'::jsonb,
                  'completed', %s, %s)
                """,
                (run, key, kind, release.id, started, completed),
            )
        connection.execute(
            """
            INSERT INTO jobs (id, raw_url, first_discovered_at, last_discovered_at)
            VALUES (%s, 'https://example.com/runs-contract', %s, %s)
            """,
            (job_id, now, now),
        )
        connection.execute(
            """
            INSERT INTO job_discoveries (pipeline_run_id, job_id, keyword, domain, discovered_at)
            VALUES (%s, %s, 'python', 'example.com', %s)
            """,
            (run_id, job_id, now - timedelta(minutes=10)),
        )
        connection.execute(
            """
            INSERT INTO processing_attempts (
              id, pipeline_run_id, job_id, operation_key, attempt_number,
              input_digest, status, started_at, completed_at
            ) VALUES (%s, %s, %s, 'evaluation', 0, %s, 'completed', %s, %s)
            """,
            (
                attempt_id,
                run_id,
                job_id,
                "c" * 64,
                now - timedelta(minutes=9),
                now - timedelta(minutes=9),
            ),
        )
        connection.execute(
            """
            INSERT INTO model_call_attempts (
              id, processing_attempt_id, pipeline_run_id, prompt_release_id,
              request_id, attempt_number, operation_key, prompt_name,
              prompt_version_id, input_digest, requested_model, provider,
              status, parsed_output, raw_response, input_tokens, output_tokens,
              cost_usd, latency_ms, observed_at, response_model, request_messages
            ) VALUES (
              %s, %s, %s, %s, %s, 0, 'evaluation', %s, %s, %s,
              'test-model', 'typesafe', 'accepted', '{}'::jsonb, '{}'::jsonb,
              10, 5, 0.25000000, 40, %s, 'test-model', '[]'::jsonb
            )
            """,
            (
                uuid4(),
                attempt_id,
                run_id,
                release.id,
                "d" * 64,
                prompt_name,
                prompt_version_id,
                "c" * 64,
                now - timedelta(minutes=9),
            ),
        )

        items = load_pipeline_runs(connection, limit=10)
        by_id = {item.id: item for item in items}
        working = by_id[run_id]
        idle = by_id[idle_run_id]
        assert working.idle_tick is False
        assert idle.idle_tick is False  # idle ticks are orchestration-only; unit-pinned
        assert working.discoveries == 1
        assert working.processed_jobs == 1
        assert working.model_calls == 1
        assert working.known_cost_usd == Decimal("0.25")

        detail = load_run_detail(connection, run_id)
        assert detail.item.model_calls == 1
        assert detail.unknown_cost_calls == 0
        assert detail.keywords[0].keyword == "python"
        assert detail.models[0].model == "test-model"
        assert detail.models[0].known_cost_usd == Decimal("0.25")
        assert detail.models[0].max_latency_ms == 40

        with pytest.raises(RunNotFound):
            load_run_detail(connection, uuid4())


def test_spend_analytics_reads_totals_days_and_models(
    authority_schema: str,
) -> None:
    now = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    recent_run_id = uuid4()
    older_run_id = uuid4()
    job_id = uuid4()
    recent_attempt_id = uuid4()
    older_attempt_id = uuid4()

    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        release = bootstrap_prompt_release(connection)
        prompt_name, prompt_version_id = connection.execute(
            """
            SELECT prompt_name, prompt_version_id
            FROM prompt_release_members
            WHERE release_id = %s
            ORDER BY prompt_name
            LIMIT 1
            """,
            (release.id,),
        ).fetchone() or pytest.fail("bootstrap release has no members")
        for run, key, started, kind in (
            (recent_run_id, f"spend:{recent_run_id}", now - timedelta(hours=4), "discovery"),
            (older_run_id, f"spend:{older_run_id}", now - timedelta(days=44), "discovery"),
        ):
            connection.execute(
                """
                INSERT INTO pipeline_runs (
                  id, idempotency_key, kind, implementation_ref, prompt_release_id,
                  parameters, status, started_at, completed_at
                ) VALUES (%s, %s, %s, 'spend-contract', %s, '{"source": "contract"}'::jsonb,
                  'completed', %s, %s)
                """,
                (run, key, kind, release.id, started, started),
            )
        connection.execute(
            """
            INSERT INTO jobs (id, raw_url, first_discovered_at, last_discovered_at)
            VALUES (%s, 'https://example.com/spend-contract', %s, %s)
            """,
            (job_id, now, now),
        )
        for attempt, run, digest in (
            (recent_attempt_id, recent_run_id, "e" * 64),
            (older_attempt_id, older_run_id, "0" * 63 + "1"),
        ):
            connection.execute(
                """
                INSERT INTO processing_attempts (
                  id, pipeline_run_id, job_id, operation_key, attempt_number,
                  input_digest, status, started_at, completed_at
                ) VALUES (%s, %s, %s, 'evaluation', 0, %s, 'completed', %s, %s)
                """,
                (attempt, run, job_id, digest, now, now),
            )

        def model_attempt(
            attempt_id: UUID,
            request_digest: str,
            *,
            run_id: UUID,
            processing_attempt_id: UUID,
            input_digest: str,
            model: str,
            status: str,
            cost: Decimal | None,
            input_tokens: int | None,
            output_tokens: int | None,
            latency_ms: int,
            observed_at: datetime,
            response_model: str | None,
        ) -> None:
            accepted = status == "accepted"
            connection.execute(
                """
                INSERT INTO model_call_attempts (
                  id, processing_attempt_id, pipeline_run_id, prompt_release_id,
                  request_id, attempt_number, operation_key, prompt_name,
                  prompt_version_id, input_digest, requested_model, provider,
                  status, parsed_output, raw_response, input_tokens, output_tokens,
                  cost_usd, latency_ms, observed_at, error, response_model,
                  request_messages
                ) VALUES (
                  %s, %s, %s, %s, %s, 0, 'evaluation', %s, %s, %s,
                  %s, 'typesafe', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, '[]'::jsonb
                )
                """,
                (
                    attempt_id,
                    processing_attempt_id,
                    run_id,
                    release.id,
                    request_digest,
                    prompt_name,
                    prompt_version_id,
                    input_digest,
                    model,
                    status,
                    Jsonb({}) if accepted else None,
                    Jsonb({}) if accepted else None,
                    input_tokens,
                    output_tokens,
                    cost,
                    latency_ms,
                    observed_at,
                    None if accepted else Jsonb({"code": "provider_timeout"}),
                    response_model,
                ),
            )

        model_attempt(
            uuid4(),
            "f" * 64,
            run_id=recent_run_id,
            processing_attempt_id=recent_attempt_id,
            input_digest="e" * 64,
            model="glm-4.6",
            status="accepted",
            cost=Decimal("0.25000000"),
            input_tokens=10,
            output_tokens=5,
            latency_ms=40,
            observed_at=now - timedelta(hours=4),
            response_model="glm-4.6",
        )
        model_attempt(
            uuid4(),
            "a" * 64,
            run_id=recent_run_id,
            processing_attempt_id=recent_attempt_id,
            input_digest="e" * 64,
            model="gpt-5-mini",
            status="accepted",
            cost=Decimal("0.10000000"),
            input_tokens=8,
            output_tokens=4,
            latency_ms=20,
            observed_at=now - timedelta(hours=3),
            response_model="gpt-5-mini",
        )
        model_attempt(
            uuid4(),
            "b" * 64,
            run_id=recent_run_id,
            processing_attempt_id=recent_attempt_id,
            input_digest="e" * 64,
            model="glm-4.6",
            status="retryable_error",
            cost=None,
            input_tokens=None,
            output_tokens=None,
            latency_ms=5,
            observed_at=now - timedelta(hours=2),
            response_model=None,
        )
        model_attempt(
            uuid4(),
            "c" * 64,
            run_id=older_run_id,
            processing_attempt_id=older_attempt_id,
            input_digest="0" * 63 + "1",
            model="glm-4.6",
            status="accepted",
            cost=Decimal("1.00000000"),
            input_tokens=100,
            output_tokens=50,
            latency_ms=90,
            observed_at=now - timedelta(days=44),
            response_model="glm-4.6",
        )

        spend = load_spend_analytics(connection)

        assert spend.known_usd == Decimal("1.35")
        assert spend.calls == 4
        assert spend.accepted == 3
        assert spend.errors == 1
        assert spend.input_tokens == 118
        assert spend.output_tokens == 59
        assert spend.max_latency_ms == 90

        assert len(spend.days) == 1
        assert spend.days[0].day == (now - timedelta(hours=2)).date()
        assert spend.days[0].calls == 3
        assert spend.days[0].accepted == 2
        assert spend.days[0].errors == 1
        assert spend.days[0].known_cost_usd == Decimal("0.35")
        assert [part.model for part in spend.days[0].by_model] == ["glm-4.6", "gpt-5-mini"]
        assert spend.days[0].by_model[0].known_cost_usd == Decimal("0.25")
        assert spend.days[0].by_model[0].p90_latency_ms == 37
        assert spend.days[0].by_model[1].known_cost_usd == Decimal("0.10")
        assert spend.days[0].by_model[1].p90_latency_ms == 20

        assert [item.model for item in spend.models] == ["glm-4.6", "gpt-5-mini"]
        assert spend.models[0].calls == 3
        assert spend.models[0].accepted == 2
        assert spend.models[0].errors == 1
        assert spend.models[0].known_cost_usd == Decimal("1.25")
        assert spend.models[0].input_tokens == 110
        assert spend.models[0].output_tokens == 55
        assert spend.models[0].max_latency_ms == 90

        bounded = load_spend_analytics(connection, day_limit=45)
        assert len(bounded.days) == 2
        assert [part.model for part in bounded.days[1].by_model] == ["glm-4.6"]
        assert bounded.days[1].by_model[0].known_cost_usd == Decimal("1.00")
        assert bounded.days[1].by_model[0].p90_latency_ms == 90


def test_activity_page_merges_runs_and_work_with_filters_and_pagination(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 21, 12, tzinfo=UTC)
    failed_run_id = uuid4()
    completed_run_id = uuid4()
    retry_job_id = uuid4()
    terminal_job_id = uuid4()
    dismissed_job_id = uuid4()
    pending_job_id = uuid4()

    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        release = bootstrap_prompt_release(connection)
        prompt_name, prompt_version_id = connection.execute(
            """
            SELECT prompt_name, prompt_version_id
            FROM prompt_release_members
            WHERE release_id = %s
            ORDER BY prompt_name
            LIMIT 1
            """,
            (release.id,),
        ).fetchone() or pytest.fail("bootstrap release has no members")

        def insert_run(
            run_id: UUID,
            kind: str,
            status: str,
            started: datetime,
            completed: datetime | None,
            error: str | None = None,
        ) -> None:
            connection.execute(
                """
                INSERT INTO pipeline_runs (
                  id, idempotency_key, kind, implementation_ref, prompt_release_id,
                  parameters, status, started_at, completed_at, error
                ) VALUES (%s, %s, %s, 'activity-contract', %s, '{}'::jsonb, %s, %s, %s,
                  %s::jsonb)
                """,
                (run_id, f"activity:{run_id}", kind, release.id, status, started, completed, error),
            )

        insert_run(
            failed_run_id,
            "discovery",
            "failed",
            now - timedelta(hours=1),
            now,
            error='{"code":"provider_timeout","reason":"Provider did not respond"}',
        )
        insert_run(completed_run_id, "processing", "completed", now - timedelta(hours=2), now)
        failed_attempt_id = uuid4()
        connection.execute(
            """
            INSERT INTO processing_attempts (
              id, pipeline_run_id, operation_key, attempt_number, input_digest,
              status, started_at, completed_at
            ) VALUES (%s, %s, 'activity-test', 0, %s, 'completed', %s, %s)
            """,
            (
                failed_attempt_id,
                failed_run_id,
                "a" * 64,
                now - timedelta(minutes=31),
                now - timedelta(minutes=30),
            ),
        )
        connection.execute(
            """
            INSERT INTO model_call_attempts (
              id, processing_attempt_id, pipeline_run_id, prompt_release_id,
              request_id, attempt_number, operation_key, prompt_name,
              prompt_version_id, input_digest, requested_model, provider,
              status, parsed_output, raw_response, input_tokens, output_tokens,
              cost_usd, latency_ms, observed_at, response_model, request_messages
            ) VALUES (
              %s, %s, %s, %s, %s, 0, 'activity-test', %s, %s, %s,
              'test-model', 'typesafe', 'accepted', '{}'::jsonb, '{}'::jsonb,
              4, 2, 0.50000000, 10, %s, 'test-model', '[]'::jsonb
            )
            """,
            (
                uuid4(),
                failed_attempt_id,
                failed_run_id,
                release.id,
                "d" * 64,
                prompt_name,
                prompt_version_id,
                "a" * 64,
                now - timedelta(minutes=30),
            ),
        )

        def insert_job(job_id: UUID, url_slug: str) -> None:
            connection.execute(
                """
                INSERT INTO jobs (id, raw_url, first_discovered_at, last_discovered_at)
                VALUES (%s, %s, %s, %s)
                """,
                (job_id, f"https://example.com/{url_slug}", now - timedelta(hours=3), now),
            )

        def insert_work(
            job_id: UUID,
            state: str,
            *,
            created: datetime,
            completed: datetime | None = None,
            last_failed: datetime | None = None,
            retry_at: datetime | None = None,
            error: str | None = None,
        ) -> None:
            connection.execute(
                """
                INSERT INTO job_work_items (
                  job_id, discovery_run_id, keyword, state, created_at, completed_at,
                  last_failed_at, retry_at, last_error
                ) VALUES (%s, %s, 'python', %s, %s, %s, %s, %s,
                  %s::jsonb)
                """,
                (
                    job_id,
                    completed_run_id,
                    state,
                    created,
                    completed,
                    last_failed,
                    retry_at,
                    error,
                ),
            )

        insert_job(retry_job_id, "retry")
        insert_job(terminal_job_id, "terminal")
        insert_job(dismissed_job_id, "dismissed")
        insert_job(pending_job_id, "pending")

        insert_work(
            retry_job_id,
            "failed",
            created=now,
            last_failed=now,
            retry_at=now + timedelta(minutes=15),
            error='{"retryability":"retryable","code":"provider_timeout","reason":"Provider did not respond"}',
        )
        insert_work(
            terminal_job_id,
            "terminal_error",
            created=now - timedelta(minutes=5),
            completed=now - timedelta(minutes=4),
            last_failed=now - timedelta(minutes=4),
            error='{"retryability":"terminal","code":"invalid_job","reason":"Job is invalid"}',
        )
        insert_work(
            dismissed_job_id,
            "terminal_error",
            created=now - timedelta(minutes=10),
            completed=now - timedelta(minutes=9),
            last_failed=now - timedelta(minutes=9),
            error='{"retryability":"terminal","code":"invalid_job","reason":"Job is invalid"}',
        )
        connection.execute(
            """
            INSERT INTO work_dismissals (job_id, attempt_count, actor, dismissed_at)
            VALUES (%s, 0, 'owner', %s)
            """,
            (dismissed_job_id, now - timedelta(minutes=3)),
        )
        insert_work(pending_job_id, "pending", created=now - timedelta(minutes=1))

        def refs(page: ActivityPage) -> list[str]:
            return [entry.ref for entry in page.entries]

        def statuses(page: ActivityPage) -> list[str]:
            return [entry.status for entry in page.entries]

        everything = load_activity_page(connection, ActivityQuery(limit=50))
        assert len(everything.entries) == 6
        assert refs(everything) == [
            str(retry_job_id),
            str(pending_job_id),
            str(terminal_job_id),
            str(dismissed_job_id),
            str(failed_run_id),
            str(completed_run_id),
        ]
        assert statuses(everything) == [
            "retrying",
            "running",
            "terminal",
            "dismissed",
            "failed",
            "completed",
        ]
        assert everything.next_cursor is None

        run_entry = next(e for e in everything.entries if e.entry_type == "run")
        assert isinstance(run_entry.item, ActivityRun)
        assert run_entry.item.model_calls == 1
        assert run_entry.item.known_cost_usd == Decimal("0.5")
        assert run_entry.item.processing_attempts == 1

        failed_only = load_activity_page(connection, ActivityQuery(statuses=frozenset({"failed"})))
        assert refs(failed_only) == [str(failed_run_id)]

        attention = load_activity_page(
            connection, ActivityQuery(statuses=frozenset({"retrying", "terminal", "dismissed"}))
        )
        assert refs(attention) == [str(retry_job_id), str(terminal_job_id), str(dismissed_job_id)]

        work_only = load_activity_page(connection, ActivityQuery(kind="work"))
        assert all(entry.kind == "work" for entry in work_only.entries)
        assert len(work_only.entries) == 4

        discovery_only = load_activity_page(connection, ActivityQuery(kind="discovery"))
        assert refs(discovery_only) == [str(failed_run_id)]

        windowed = load_activity_page(
            connection,
            ActivityQuery(
                from_at=now - timedelta(hours=1),
                to_at=now - timedelta(minutes=2),
            ),
        )
        assert refs(windowed) == [
            str(terminal_job_id),
            str(dismissed_job_id),
            str(failed_run_id),
        ]

        first = load_activity_page(connection, ActivityQuery(limit=3))
        assert refs(first) == refs(everything)[:3]
        assert first.next_cursor is not None
        second = load_activity_page(connection, ActivityQuery(limit=3, cursor=first.next_cursor))
        assert refs(second) == refs(everything)[3:]
        assert second.next_cursor is None

        with pytest.raises(ValueError):
            load_activity_page(connection, ActivityQuery(cursor="garbage!"))


def test_activity_pagination_neither_duplicates_nor_skips_tied_entries(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 21, 12, tzinfo=UTC)
    run_id = uuid4()
    tied_job_ids = [uuid4() for _ in range(3)]
    older_job_id = uuid4()
    oldest_job_id = uuid4()

    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        release = bootstrap_prompt_release(connection)
        connection.execute(
            """
            INSERT INTO pipeline_runs (
              id, idempotency_key, kind, implementation_ref, prompt_release_id,
              parameters, status, started_at, completed_at
            ) VALUES (%s, %s, 'processing', 'activity-tie-contract', %s, '{}'::jsonb,
              'completed', %s, %s)
            """,
            (
                run_id,
                f"activity-tie:{run_id}",
                release.id,
                now - timedelta(hours=3),
                now - timedelta(hours=3),
            ),
        )
        work_rows = [(job_id, now) for job_id in tied_job_ids] + [
            (older_job_id, now - timedelta(hours=1)),
            (oldest_job_id, now - timedelta(hours=2)),
        ]
        for job_id, created in work_rows:
            connection.execute(
                """
                INSERT INTO jobs (id, raw_url, first_discovered_at, last_discovered_at)
                VALUES (%s, %s, %s, %s)
                """,
                (job_id, f"https://example.com/tie-{job_id}", created, created),
            )
            connection.execute(
                """
                INSERT INTO job_work_items (
                  job_id, discovery_run_id, keyword, state, created_at
                ) VALUES (%s, %s, 'python', 'pending', %s)
                """,
                (job_id, run_id, created),
            )

        def refs(page: ActivityPage) -> list[str]:
            return [entry.ref for entry in page.entries]

        everything = load_activity_page(connection, ActivityQuery(limit=50))
        assert len(everything.entries) == 6
        assert {entry.ref for entry in everything.entries[:3]} == {
            str(job_id) for job_id in tied_job_ids
        }
        assert all(entry.occurred_at == now for entry in everything.entries[:3])

        pages: list[ActivityPage] = []
        cursor: str | None = None
        for _ in range(10):
            page = load_activity_page(connection, ActivityQuery(limit=2, cursor=cursor))
            pages.append(page)
            cursor = page.next_cursor
            if cursor is None:
                break
        assert cursor is None

        paged_refs = [entry.ref for page in pages for entry in page.entries]
        assert [len(page.entries) for page in pages] == [2, 2, 2]
        assert paged_refs == refs(everything)
        assert len(set(paged_refs)) == 6
        assert pages[0].entries[-1].occurred_at == now
        assert pages[1].entries[0].occurred_at == now
        assert pages[0].entries[-1].ref != pages[1].entries[0].ref


def test_activity_cursor_continues_filtered_pages_without_duplicates_or_skips(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 21, 12, tzinfo=UTC)
    run_id = uuid4()
    retrying_job_ids = [uuid4() for _ in range(4)]
    pending_job_ids = [uuid4() for _ in range(3)]
    retry_error = (
        '{"retryability":"retryable","code":"provider_timeout","reason":"Provider did not respond"}'
    )

    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        release = bootstrap_prompt_release(connection)
        connection.execute(
            """
            INSERT INTO pipeline_runs (
              id, idempotency_key, kind, implementation_ref, prompt_release_id,
              parameters, status, started_at, completed_at
            ) VALUES (%s, %s, 'processing', 'activity-filter-contract', %s, '{}'::jsonb,
              'completed', %s, %s)
            """,
            (
                run_id,
                f"activity-filter:{run_id}",
                release.id,
                now - timedelta(hours=3),
                now - timedelta(hours=3),
            ),
        )
        retry_rows = list(
            zip(
                retrying_job_ids,
                [
                    now,
                    now - timedelta(minutes=10),
                    now - timedelta(minutes=20),
                    now - timedelta(minutes=30),
                ],
                strict=True,
            )
        )
        for job_id, failed_at in retry_rows:
            connection.execute(
                """
                INSERT INTO jobs (id, raw_url, first_discovered_at, last_discovered_at)
                VALUES (%s, %s, %s, %s)
                """,
                (job_id, f"https://example.com/retry-{job_id}", failed_at, failed_at),
            )
            connection.execute(
                """
                INSERT INTO job_work_items (
                  job_id, discovery_run_id, keyword, state, created_at,
                  last_failed_at, retry_at, last_error
                ) VALUES (%s, %s, 'python', 'failed', %s, %s, %s, %s::jsonb)
                """,
                (job_id, run_id, failed_at, failed_at, failed_at + timedelta(hours=2), retry_error),
            )
        for job_id, created in zip(
            pending_job_ids,
            [now - timedelta(minutes=5), now - timedelta(minutes=15), now - timedelta(minutes=25)],
            strict=True,
        ):
            connection.execute(
                """
                INSERT INTO jobs (id, raw_url, first_discovered_at, last_discovered_at)
                VALUES (%s, %s, %s, %s)
                """,
                (job_id, f"https://example.com/filtered-{job_id}", created, created),
            )
            connection.execute(
                """
                INSERT INTO job_work_items (
                  job_id, discovery_run_id, keyword, state, created_at
                ) VALUES (%s, %s, 'python', 'pending', %s)
                """,
                (job_id, run_id, created),
            )

        def refs(page: ActivityPage) -> list[str]:
            return [entry.ref for entry in page.entries]

        filtered = ActivityQuery(
            statuses=frozenset({"retrying"}),
            from_at=now - timedelta(hours=1),
            to_at=now + timedelta(minutes=1),
        )
        everything = load_activity_page(connection, replace(filtered, limit=50))
        assert refs(everything) == [str(job_id) for job_id in retrying_job_ids]
        assert all(entry.status == "retrying" for entry in everything.entries)

        first = load_activity_page(connection, replace(filtered, limit=3))
        assert first.next_cursor is not None
        second = load_activity_page(
            connection, replace(filtered, limit=3, cursor=first.next_cursor)
        )
        assert refs(first) == refs(everything)[:3]
        assert refs(second) == refs(everything)[3:]
        assert second.next_cursor is None
        assert set(refs(first) + refs(second)).isdisjoint(
            {str(job_id) for job_id in pending_job_ids}
        )


def test_work_item_detail_reads_state_dismissal_and_attempt_history(
    authority_schema: str,
) -> None:
    now = datetime(2026, 9, 21, 12, tzinfo=UTC)
    run_id = uuid4()
    job_id = uuid4()

    with _connection(authority_schema) as connection:
        apply_migrations(connection)
        release = bootstrap_prompt_release(connection)
        connection.execute(
            """
            INSERT INTO pipeline_runs (
              id, idempotency_key, kind, implementation_ref, prompt_release_id,
              parameters, status, started_at, completed_at
            ) VALUES (%s, %s, 'processing', 'work-detail-contract', %s, '{}'::jsonb,
              'completed', %s, %s)
            """,
            (run_id, f"work-detail:{run_id}", release.id, now - timedelta(hours=1), now),
        )
        connection.execute(
            """
            INSERT INTO jobs (id, raw_url, first_discovered_at, last_discovered_at)
            VALUES (%s, 'https://example.com/work-detail', %s, %s)
            """,
            (job_id, now, now),
        )
        connection.execute(
            """
            INSERT INTO job_work_items (
              job_id, discovery_run_id, keyword, state, attempt_count, created_at,
              last_failed_at, retry_at, last_error
            ) VALUES (
              %s, %s, 'python', 'failed', 2, %s, %s, %s,
              '{"retryability":"retryable","code":"provider_timeout",
                "reason":"Provider did not respond"}'::jsonb
            )
            """,
            (
                job_id,
                run_id,
                now - timedelta(hours=1),
                now - timedelta(minutes=5),
                now + timedelta(minutes=10),
            ),
        )
        connection.execute(
            """
            INSERT INTO processing_attempts (
              id, pipeline_run_id, job_id, operation_key, attempt_number, input_digest,
              status, started_at, completed_at, error
            ) VALUES (%s, %s, %s, 'evaluation', 1, %s, 'failed', %s, %s,
              '{"retryability":"retryable","code":"provider_timeout",
                "reason":"Provider did not respond"}'::jsonb
            )
            """,
            (
                uuid4(),
                run_id,
                job_id,
                "e" * 64,
                now - timedelta(minutes=5),
                now - timedelta(minutes=4),
            ),
        )
        connection.execute(
            """
            INSERT INTO work_dismissals (job_id, attempt_count, actor, dismissed_at)
            VALUES (%s, 1, 'owner', %s)
            """,
            (job_id, now - timedelta(minutes=40)),
        )

        detail = load_work_item_detail(connection, job_id)

        assert detail.state == "failed"
        assert detail.attempt_count == 2
        assert detail.failure_summary is not None
        assert "provider_timeout" in detail.failure_summary
        assert detail.retry_at == now + timedelta(minutes=10)
        assert detail.dismissed is False
        assert detail.dismissed_at is None
        assert detail.verdict is None
        assert len(detail.attempts) == 1
        attempt = detail.attempts[0]
        assert attempt.operation_key == "evaluation"
        assert attempt.run_id == run_id
        assert attempt.model_calls == 0

        for index, (outcome, profile, reason) in enumerate(
            (
                ("qualified", "Backend engineer", "Matches the required profile."),
                ("company_blocked", None, "Company is blocked by policy."),
            )
        ):
            snapshot_id = f"{index + 1:064x}"
            connection.execute(
                """
                INSERT INTO job_snapshots (
                  id, job_id, content_digest, title, company, normalized_company,
                  normalized_title, source, raw_url, description, location,
                  keywords, observed_at
                ) VALUES (%s, %s, %s, 'Backend engineer', 'Acme', 'acme',
                  'backend engineer', 'test', 'https://example.com/work-detail',
                  'Build services', 'Remote', '[]'::jsonb, %s)
                """,
                (snapshot_id, job_id, f"{index + 10:064x}", now),
            )
            connection.execute(
                """
                INSERT INTO evaluation_decisions (
                  id, snapshot_id, pipeline_run_id, prompt_release_id,
                  policy_version, outcome, matched_profile, reason, created_at
                ) VALUES (%s, %s, %s, %s, 'work-detail-contract', %s, %s, %s, %s)
                """,
                (
                    f"{index + 20:064x}",
                    snapshot_id,
                    run_id,
                    release.id,
                    outcome,
                    profile,
                    reason,
                    now - timedelta(minutes=2 - index),
                ),
            )

        latest = load_work_item_detail(connection, job_id)
        assert latest.verdict is not None
        assert latest.verdict.outcome == "company_blocked"
        assert latest.verdict.reason == "Company is blocked by policy."
        assert latest.verdict.matched_profile is None
        assert latest.verdict.decided_at == now - timedelta(minutes=1)

        with pytest.raises(WorkItemNotFound):
            load_work_item_detail(connection, uuid4())
