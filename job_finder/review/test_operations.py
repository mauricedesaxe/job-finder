from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import cast
from uuid import UUID

import pytest

from job_finder.review.operations import (
    ActionableWork,
    ActivityEntry,
    ActivityQuery,
    ActivityRun,
    ActivityWork,
    JobReevaluationCommand,
    RunListItem,
    OperationsHealth,
    OperationsSnapshot,
    OperationsUnavailable,
    PipelineRunStatus,
    PipelineRunSummary,
    QueueCounts,
    RecoveryAction,
    SpendSummary,
    WorkItemState,
    WorkRecoveryCommand,
    decode_activity_cursor,
    encode_activity_cursor,
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


def test_dismissed_terminal_work_does_not_require_action_by_itself() -> None:
    assert (
        operations_health(QueueCounts(terminal_error=2), (), dismissed_terminal=2)
        is OperationsHealth.CAUGHT_UP
    )
    assert (
        operations_health(QueueCounts(terminal_error=2), (), dismissed_terminal=1)
        is OperationsHealth.ACTION_REQUIRED
    )


def test_a_snapshot_rejects_more_dismissals_than_terminal_work() -> None:
    with pytest.raises(ValueError):
        OperationsSnapshot(
            health=operations_health(QueueCounts(terminal_error=1), (), 2),
            queues=QueueCounts(terminal_error=1),
            spend=SpendSummary(known_usd=Decimal(0), unknown_attempts=0),
            recent_runs=(),
            failures=(),
            dismissed_terminal=2,
        )


def test_completed_orchestration_runs_without_work_are_idle_ticks() -> None:
    def item(discoveries: int, processing_attempts: int) -> RunListItem:
        return RunListItem(
            id=UUID(int=1),
            kind="orchestration",
            status="completed",
            started_at=NOW,
            completed_at=NOW,
            discoveries=discoveries,
            processing_attempts=processing_attempts,
            processed_jobs=0,
            model_calls=0,
            known_cost_usd=Decimal(0),
            error_summary=None,
        )

    assert item(0, 0).idle_tick is True
    assert item(2, 0).idle_tick is False
    assert item(0, 1).idle_tick is False


def _activity_run(status: PipelineRunStatus) -> ActivityRun:
    return ActivityRun(
        id=UUID(int=1),
        kind="orchestration",
        status=status,
        started_at=NOW,
        completed_at=None if status == "running" else NOW,
        discoveries=0,
        processing_attempts=0,
        processed_jobs=0,
        model_calls=0,
        known_cost_usd=Decimal(0),
        error_summary=None,
    )


def _activity_work(state: str, *, dismissed: bool = False) -> ActivityWork:
    return ActivityWork(
        job_id=UUID(int=2),
        state=cast(WorkItemState, state),
        attempt_count=1,
        occurred_at=NOW,
        retry_at=NOW if state == "failed" else None,
        failure_summary=None,
        dismissed=dismissed,
    )


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        (_activity_run("running"), "running"),
        (_activity_run("completed"), "completed"),
        (_activity_run("failed"), "failed"),
        (_activity_work("pending"), "running"),
        (_activity_work("leased"), "running"),
        (_activity_work("completed"), "completed"),
        (_activity_work("failed"), "retrying"),
        (_activity_work("terminal_error"), "terminal"),
        (_activity_work("terminal_error", dismissed=True), "dismissed"),
    ],
)
def test_activity_entries_map_to_one_unified_status(
    item: ActivityRun | ActivityWork, expected: str
) -> None:
    entry = ActivityEntry(occurred_at=NOW, ref="ref", item=item)

    assert entry.status == expected


def test_activity_query_rejects_unknown_statuses_limits_and_cursors() -> None:
    with pytest.raises(ValueError):
        ActivityQuery(statuses=frozenset({"exploded"}))
    with pytest.raises(ValueError):
        ActivityQuery(limit=0)
    with pytest.raises(ValueError):
        ActivityQuery(cursor="broken-cursor")


def test_activity_query_accepts_a_round_tripped_cursor() -> None:
    entry = ActivityEntry(
        occurred_at=NOW,
        ref=str(UUID(int=7)),
        item=_activity_run("completed"),
    )

    query = ActivityQuery(limit=3, cursor=encode_activity_cursor(entry))

    assert query.cursor is not None
    assert decode_activity_cursor(query.cursor) == (NOW, "run", str(UUID(int=7)))


def test_activity_cursor_round_trips_through_encoding() -> None:
    entry = ActivityEntry(
        occurred_at=NOW,
        ref=str(UUID(int=7)),
        item=_activity_run("completed"),
    )

    cursor = encode_activity_cursor(entry)
    occurred_at, entry_type, ref = decode_activity_cursor(cursor)

    assert occurred_at == NOW
    assert entry_type == "run"
    assert ref == str(UUID(int=7))


@pytest.mark.parametrize("cursor", ["", "not-base64!!", "x", "abc"])
def test_activity_cursor_rejects_garbage(cursor: str) -> None:
    with pytest.raises(ValueError):
        decode_activity_cursor(cursor)
