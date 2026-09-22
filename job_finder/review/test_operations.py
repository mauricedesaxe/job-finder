from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from job_finder.review.operations import (
    OperationsHealth,
    OperationsSnapshot,
    PipelineRunStatus,
    PipelineRunSummary,
    QueueCounts,
    SpendSummary,
    operations_health,
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
