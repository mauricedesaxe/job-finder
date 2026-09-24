from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from job_finder.review.operations import OperationsUnavailable
from job_finder.review.postgres import Connection, ConnectionFactory

SPEND_DAY_LIMIT = 30
SPEND_MODEL_LIMIT = 10
SPEND_RUN_LIMIT = 20


@dataclass(frozen=True)
class DaySpend:
    day: date
    calls: int
    accepted: int
    errors: int
    known_cost_usd: Decimal

    def __post_init__(self) -> None:
        if self.calls < 0 or self.accepted < 0 or self.errors < 0 or self.known_cost_usd < 0:
            raise ValueError("spend values cannot be negative")
        if self.accepted + self.errors != self.calls:
            raise ValueError("day outcomes do not add up to the recorded calls")


@dataclass(frozen=True)
class ModelSpend:
    model: str
    calls: int
    accepted: int
    errors: int
    input_tokens: int
    output_tokens: int
    known_cost_usd: Decimal
    max_latency_ms: int

    def __post_init__(self) -> None:
        if (
            self.calls < 0
            or self.accepted < 0
            or self.errors < 0
            or self.input_tokens < 0
            or self.output_tokens < 0
            or self.known_cost_usd < 0
            or self.max_latency_ms < 0
        ):
            raise ValueError("spend values cannot be negative")
        if self.accepted + self.errors != self.calls:
            raise ValueError("model outcomes do not add up to the recorded calls")
        if not self.model:
            raise ValueError("model name must not be empty")


@dataclass(frozen=True)
class RunSpend:
    id: UUID
    kind: str
    started_at: datetime
    calls: int
    known_cost_usd: Decimal

    def __post_init__(self) -> None:
        if self.calls < 0 or self.known_cost_usd < 0:
            raise ValueError("spend values cannot be negative")
        if not self.kind:
            raise ValueError("run kind must not be empty")


@dataclass(frozen=True)
class SpendAnalytics:
    known_usd: Decimal
    calls: int
    accepted: int
    errors: int
    input_tokens: int
    output_tokens: int
    max_latency_ms: int
    days: tuple[DaySpend, ...]
    models: tuple[ModelSpend, ...]
    runs: tuple[RunSpend, ...]

    def __post_init__(self) -> None:
        if (
            self.known_usd < 0
            or self.calls < 0
            or self.accepted < 0
            or self.errors < 0
            or self.input_tokens < 0
            or self.output_tokens < 0
            or self.max_latency_ms < 0
        ):
            raise ValueError("spend values cannot be negative")
        if self.accepted + self.errors != self.calls:
            raise ValueError("outcomes do not add up to the recorded calls")


def _unavailable_analytics() -> SpendAnalytics:
    raise OperationsUnavailable("Model spend analytics are unavailable")


@dataclass(frozen=True)
class AnalyticsService:
    load: Callable[[], SpendAnalytics] = _unavailable_analytics


def postgres_analytics_service(connect: ConnectionFactory) -> AnalyticsService:
    def load() -> SpendAnalytics:
        with connect() as connection:
            return load_spend_analytics(connection)

    return AnalyticsService(load=load)


def load_spend_analytics(
    connection: Connection,
    *,
    day_limit: int = SPEND_DAY_LIMIT,
    model_limit: int = SPEND_MODEL_LIMIT,
    run_limit: int = SPEND_RUN_LIMIT,
) -> SpendAnalytics:
    if day_limit < 1:
        raise ValueError("spend day limit must be positive")
    if model_limit < 1:
        raise ValueError("spend model limit must be positive")
    if run_limit < 1:
        raise ValueError("spend run limit must be positive")

    totals_row = connection.execute(
        """
        SELECT COALESCE(sum(cost_usd), 0), count(*),
               count(*) FILTER (WHERE status = 'accepted'),
               count(*) FILTER (WHERE status <> 'accepted'),
               COALESCE(sum(input_tokens), 0), COALESCE(sum(output_tokens), 0),
               COALESCE(max(latency_ms), 0)
        FROM model_call_attempts
        """
    ).fetchone()
    if totals_row is None:
        raise RuntimeError("Could not read model spend totals")

    day_rows = connection.execute(
        """
        SELECT (observed_at AT TIME ZONE 'UTC')::date AS day, count(*),
               count(*) FILTER (WHERE status = 'accepted'),
               count(*) FILTER (WHERE status <> 'accepted'),
               COALESCE(sum(cost_usd), 0)
        FROM model_call_attempts
        WHERE observed_at >= now() - (%s * interval '1 day')
        GROUP BY day
        ORDER BY day DESC
        """,
        (day_limit,),
    ).fetchall()

    model_rows = connection.execute(
        """
        SELECT requested_model, count(*),
               count(*) FILTER (WHERE status = 'accepted'),
               count(*) FILTER (WHERE status <> 'accepted'),
               COALESCE(sum(input_tokens), 0), COALESCE(sum(output_tokens), 0),
               COALESCE(sum(cost_usd), 0), COALESCE(max(latency_ms), 0)
        FROM model_call_attempts
        GROUP BY requested_model
        ORDER BY COALESCE(sum(cost_usd), 0) DESC, count(*) DESC, requested_model
        LIMIT %s
        """,
        (model_limit,),
    ).fetchall()

    run_rows = connection.execute(
        """
        SELECT run.id, run.kind, run.started_at, count(m.id), COALESCE(sum(m.cost_usd), 0)
        FROM pipeline_runs run
        JOIN model_call_attempts m ON m.pipeline_run_id = run.id
        GROUP BY run.id, run.kind, run.started_at
        ORDER BY run.started_at DESC, run.id DESC
        LIMIT %s
        """,
        (run_limit,),
    ).fetchall()

    return SpendAnalytics(
        known_usd=Decimal(str(totals_row[0])),
        calls=int(str(totals_row[1])),
        accepted=int(str(totals_row[2])),
        errors=int(str(totals_row[3])),
        input_tokens=int(str(totals_row[4])),
        output_tokens=int(str(totals_row[5])),
        max_latency_ms=int(str(totals_row[6])),
        days=tuple(_parse_day_spend(row) for row in day_rows),
        models=tuple(_parse_model_spend(row) for row in model_rows),
        runs=tuple(_parse_run_spend(row) for row in run_rows),
    )


def _parse_day_spend(row: tuple[object, ...]) -> DaySpend:
    parsed_day = row[0]
    if isinstance(parsed_day, datetime):
        parsed_day = parsed_day.date()
    if not isinstance(parsed_day, date):
        raise RuntimeError("Spend day is invalid")
    return DaySpend(
        day=parsed_day,
        calls=int(str(row[1])),
        accepted=int(str(row[2])),
        errors=int(str(row[3])),
        known_cost_usd=Decimal(str(row[4])),
    )


def _parse_model_spend(row: tuple[object, ...]) -> ModelSpend:
    model = row[0]
    if not isinstance(model, str) or not model:
        raise RuntimeError("Spend model name is invalid")
    return ModelSpend(
        model=model,
        calls=int(str(row[1])),
        accepted=int(str(row[2])),
        errors=int(str(row[3])),
        input_tokens=int(str(row[4])),
        output_tokens=int(str(row[5])),
        known_cost_usd=Decimal(str(row[6])),
        max_latency_ms=int(str(row[7])),
    )


def _parse_run_spend(row: tuple[object, ...]) -> RunSpend:
    run_id, kind, started_at = row[0], row[1], row[2]
    if not isinstance(run_id, UUID):
        raise RuntimeError("Spend run id is invalid")
    if not isinstance(kind, str) or not kind:
        raise RuntimeError("Spend run kind is invalid")
    if not isinstance(started_at, datetime):
        raise RuntimeError("Spend run start time is invalid")
    return RunSpend(
        id=run_id,
        kind=kind,
        started_at=started_at,
        calls=int(str(row[3])),
        known_cost_usd=Decimal(str(row[4])),
    )
