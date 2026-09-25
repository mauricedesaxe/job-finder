from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from job_finder.database import ConnectionFactory
from job_finder.operations._common import OperationsUnavailable
from job_finder.operations.health import (
    OperationsHealth,
    OperationsSnapshot,
    QueueCounts,
    SpendSummary,
    load_operations_snapshot,
)
from job_finder.operations.work_history import (
    WorkItemDetail,
    unavailable_work_detail,
    load_work_item_detail,
)
from job_finder.pipeline.work_dismissals import (
    WorkDismissalCommand,
    WorkDismissalResult,
    dismiss_work,
)
from job_finder.pipeline.work_recoveries import (
    WorkRecoveryCommand,
    WorkRecoveryResult,
    recover_work,
)
from job_finder.pipeline.reevaluations import (
    JobReevaluationCommand,
    JobReevaluationResult,
    request_job_reevaluation,
)


def _unavailable_recovery(_command: WorkRecoveryCommand) -> WorkRecoveryResult:
    raise OperationsUnavailable("Work recovery is unavailable")


def _unavailable_reevaluation(_command: JobReevaluationCommand) -> JobReevaluationResult:
    raise OperationsUnavailable("Job reevaluation is unavailable")


def _unavailable_dismissal(_command: WorkDismissalCommand) -> WorkDismissalResult:
    raise OperationsUnavailable("Work dismissal is unavailable")


@dataclass(frozen=True)
class OperationsService:
    load: Callable[[], OperationsSnapshot]
    recover: Callable[[WorkRecoveryCommand], WorkRecoveryResult] = _unavailable_recovery
    reevaluate: Callable[[JobReevaluationCommand], JobReevaluationResult] = (
        _unavailable_reevaluation
    )
    dismiss: Callable[[WorkDismissalCommand], WorkDismissalResult] = _unavailable_dismissal
    work_detail: Callable[[UUID], WorkItemDetail] = unavailable_work_detail


def unknown_operations_service() -> OperationsService:
    snapshot = OperationsSnapshot(
        health=OperationsHealth.UNKNOWN,
        queues=QueueCounts(),
        spend=SpendSummary(known_usd=Decimal(0), unknown_attempts=0),
        recent_runs=(),
        failures=(),
        actionable_work_total=0,
    )
    return OperationsService(load=lambda: snapshot)


def postgres_operations_service(connect: ConnectionFactory) -> OperationsService:
    def load() -> OperationsSnapshot:
        with connect() as connection:
            return load_operations_snapshot(connection)

    def recover(command: WorkRecoveryCommand) -> WorkRecoveryResult:
        with connect() as connection:
            return recover_work(connection, command)

    def reevaluate(command: JobReevaluationCommand) -> JobReevaluationResult:
        with connect() as connection:
            return request_job_reevaluation(connection, command)

    def dismiss(command: WorkDismissalCommand) -> WorkDismissalResult:
        with connect() as connection:
            return dismiss_work(connection, command)

    def work_detail(job: UUID) -> WorkItemDetail:
        with connect() as connection:
            return load_work_item_detail(connection, job)

    return OperationsService(
        load=load,
        recover=recover,
        reevaluate=reevaluate,
        dismiss=dismiss,
        work_detail=work_detail,
    )
