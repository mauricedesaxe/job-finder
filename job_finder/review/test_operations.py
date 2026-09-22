from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from job_finder.review.operations import (
    ActionableWork,
    JobReevaluationCommand,
    OperationsHealth,
    OperationsSnapshot,
    OperationsUnavailable,
    PipelineRunStatus,
    PipelineRunSummary,
    QueueCounts,
    RecoveryAction,
    SpendSummary,
    WorkRecoveryCommand,
    operations_health,
    unknown_operations_service,
)

NOW = datetime(2026, 9, 21, 12, tzinfo=UTC)


def _run(
    status: PipelineRunStatus,
    *,
    value: int = 1,
) -> PipelineRunSummary:
    return PipelineRunSummary(
        id=UUID(int=value),
        kind="processing",
        status=status,
        started_at=NOW,
        completed_at=None if status == "running" else NOW,
    )


@pytest.mark.parametrize(
    ("queues", "runs", "expected"),
    [
        (QueueCounts(), (), OperationsHealth.UNKNOWN),
        (QueueCounts(pending=1), (), OperationsHealth.WORKING),
        (QueueCounts(retrying=1), (), OperationsHealth.WORKING),
        (QueueCounts(terminal_error=1), (), OperationsHealth.ACTION_REQUIRED),
        (
            QueueCounts(pending=1),
            (_run("failed"),),
            OperationsHealth.ACTION_REQUIRED,
        ),
        (
            QueueCounts(),
            (_run("completed"),),
            OperationsHealth.CAUGHT_UP,
        ),
        (
            QueueCounts(),
            (_run("running"),),
            OperationsHealth.WORKING,
        ),
        (
            QueueCounts(),
            (_run("failed"),),
            OperationsHealth.ACTION_REQUIRED,
        ),
        (
            QueueCounts(),
            (_run("completed"), _run("failed", value=2)),
            OperationsHealth.CAUGHT_UP,
        ),
    ],
)
def test_health_uses_current_queue_state_and_the_newest_run(
    queues: QueueCounts,
    runs: tuple[PipelineRunSummary, ...],
    expected: OperationsHealth,
) -> None:
    assert operations_health(queues, runs) is expected


def test_snapshot_rejects_a_health_value_that_does_not_match_its_evidence() -> None:
    with pytest.raises(ValueError, match="health does not match"):
        OperationsSnapshot(
            health=OperationsHealth.CAUGHT_UP,
            queues=QueueCounts(),
            spend=SpendSummary(known_usd=Decimal("0"), unknown_attempts=0),
            recent_runs=(),
            failures=(),
        )


def test_actionable_work_requires_retry_evidence_only_for_failed_work() -> None:
    failed = ActionableWork(
        job_id=UUID(int=1),
        state="failed",
        attempt_count=2,
        retry_at=NOW,
        failed_at=NOW,
        failure_summary="provider_timeout: Provider did not respond",
    )
    terminal = ActionableWork(
        job_id=UUID(int=2),
        state="terminal_error",
        attempt_count=3,
        retry_at=None,
        failed_at=NOW,
        failure_summary="invalid_job: Job is invalid",
    )

    assert failed.job_id == UUID(int=1)
    assert terminal.attempt_count == 3
    with pytest.raises(ValueError, match="retry time"):
        ActionableWork(
            job_id=UUID(int=3),
            state="terminal_error",
            attempt_count=1,
            retry_at=NOW,
            failed_at=NOW,
            failure_summary="invalid",
        )


def test_recovery_command_encodes_the_action_expected_state_relation() -> None:
    with pytest.raises(ValueError, match="does not match"):
        WorkRecoveryCommand(
            idempotency_key="invalid",
            job_id=UUID(int=1),
            action=RecoveryAction.RETRY_NOW,
            expected_state="terminal_error",
            expected_attempt_count=1,
            actor="owner",
            requested_at=NOW,
        )


def test_unknown_operations_service_rejects_mutation() -> None:
    service = unknown_operations_service()
    command = WorkRecoveryCommand(
        idempotency_key="safe-unavailable",
        job_id=UUID(int=1),
        action=RecoveryAction.RETRY_NOW,
        expected_state="failed",
        expected_attempt_count=1,
        actor="owner",
        requested_at=NOW,
    )

    with pytest.raises(OperationsUnavailable, match="unavailable"):
        service.recover(command)

    reevaluation = JobReevaluationCommand(
        idempotency_key="safe-unavailable-reevaluation",
        expected_decision_id="a" * 64,
        expected_snapshot_id="b" * 64,
        actor="owner",
        requested_at=NOW,
    )
    with pytest.raises(OperationsUnavailable, match="unavailable"):
        service.reevaluate(reevaluation)


def test_reevaluation_command_requires_exact_content_identities() -> None:
    with pytest.raises(ValueError, match="decision id"):
        JobReevaluationCommand(
            idempotency_key="invalid",
            expected_decision_id="not-a-digest",
            expected_snapshot_id="b" * 64,
            actor="owner",
            requested_at=NOW,
        )
