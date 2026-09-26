from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb
from pydantic import JsonValue

from job_finder.jobs.decisions import (
    CompanyPolicy,
    DecisionOutcome,
    DecisionStage,
    DecisionStore,
    PersistedDecision,
    TerminalDecision,
    decision_digest,
    normalize_ledger_text,
)
from job_finder.jobs.enrichment import EnrichedJob

EnqueueReviewItem = Callable[[psycopg.Connection[tuple[object, ...]], str, date], None]


def postgres_decision_store(
    connection: psycopg.Connection[tuple[object, ...]],
    *,
    excluded_job_id: UUID | None = None,
) -> DecisionStore:
    if not connection.autocommit:
        raise ValueError("Decision persistence requires an autocommit connection")

    def find_completed(idempotency_key: str) -> PersistedDecision | None:
        row = connection.execute(
            "SELECT output FROM pipeline_receipts WHERE idempotency_key = %s",
            (idempotency_key,),
        ).fetchone()
        if row is None:
            return None
        return PersistedDecision.model_validate(row[0])

    def existing_titles(normalized_company: str) -> tuple[str, ...]:
        rows = connection.execute(
            """
            SELECT DISTINCT s.title, s.normalized_title
            FROM job_snapshots s
            JOIN evaluation_decisions d ON d.snapshot_id = s.id
            WHERE s.normalized_company = %s AND d.outcome <> 'duplicate'
              AND s.job_id IS DISTINCT FROM %s::UUID
            ORDER BY s.normalized_title, s.title
            """,
            (normalized_company, excluded_job_id),
        ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def active_company_policy(
        normalized_company: str, observed_at: datetime
    ) -> CompanyPolicy | None:
        row = connection.execute(
            """
            SELECT policy
            FROM company_policies
            WHERE normalized_company = %s
              AND effective_at <= %s
              AND (expires_at IS NULL OR expires_at > %s)
            """,
            (normalized_company, observed_at, observed_at),
        ).fetchone()
        if row is None:
            return None
        value = str(row[0])
        if value == "blocked" or value == "recent_application":
            return value
        raise RuntimeError(f"Unknown company policy: {value}")

    def persist(
        decision: TerminalDecision, *, enqueue_review_item: EnqueueReviewItem | None = None
    ) -> PersistedDecision:
        with connection.transaction():
            _upsert_job(connection, decision)
            snapshot_id = _insert_snapshot(connection, decision)
            authoritative = _insert_decision(connection, decision, snapshot_id)
            if enqueue_review_item is not None and authoritative.outcome == "qualified":
                enqueue_review_item(
                    connection,
                    authoritative.decision_id,
                    decision.context.observed_at.astimezone(UTC).date(),
                )
            _insert_receipt(connection, decision, authoritative)
        completed = find_completed(decision.idempotency_key)
        if completed is None:
            raise RuntimeError("Terminal decision transaction did not create its receipt")
        return completed

    return DecisionStore(
        find_completed=find_completed,
        existing_titles=existing_titles,
        active_company_policy=active_company_policy,
        persist=persist,
    )


def _upsert_job(
    connection: psycopg.Connection[tuple[object, ...]], decision: TerminalDecision
) -> None:
    _ = connection.execute(
        """
        INSERT INTO jobs (id, raw_url, first_discovered_at, last_discovered_at)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (raw_url) DO UPDATE
        SET last_discovered_at = GREATEST(jobs.last_discovered_at, EXCLUDED.last_discovered_at)
        """,
        (
            decision.job_id,
            decision.listing.url,
            decision.context.observed_at,
            decision.context.observed_at,
        ),
    )


def _insert_snapshot(
    connection: psycopg.Connection[tuple[object, ...]], decision: TerminalDecision
) -> str:
    snapshot = {
        "title": decision.enriched.title,
        "company": decision.enriched.company,
        "normalized_company": normalize_ledger_text(decision.enriched.company),
        "normalized_title": normalize_ledger_text(decision.enriched.title),
        "source": decision.listing.source,
        "raw_url": decision.listing.url,
        "description": decision.enriched.description,
        "location": decision.enriched.location,
        "keywords": list(decision.listing.keywords_matched),
        "date_posted": decision.listing.date_posted.isoformat()
        if decision.listing.date_posted is not None
        else None,
        "ats_evidence": decision.ats_evidence,
    }
    content_digest = decision_digest(snapshot)
    snapshot_id = decision_digest([str(decision.job_id), content_digest])
    compensation = _compensation_fields(decision)
    _ = connection.execute(
        """
        INSERT INTO job_snapshots (
          id, job_id, content_digest, title, company, normalized_company,
          normalized_title, source, raw_url, description, location, keywords,
          date_posted, observed_at, ats_evidence,
          compensation_min, compensation_max, compensation_currency,
          compensation_period, compensation_source
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                  %s, %s, %s, %s, %s)
        ON CONFLICT (id) DO NOTHING
        """,
        (
            snapshot_id,
            decision.job_id,
            content_digest,
            decision.enriched.title,
            decision.enriched.company,
            snapshot["normalized_company"],
            snapshot["normalized_title"],
            decision.listing.source,
            decision.listing.url,
            decision.enriched.description,
            decision.enriched.location,
            Jsonb(snapshot["keywords"]),
            decision.listing.date_posted,
            decision.context.observed_at,
            Jsonb(decision.ats_evidence) if decision.ats_evidence is not None else None,
            *compensation,
        ),
    )
    return snapshot_id


def _compensation_fields(
    decision: TerminalDecision,
) -> tuple[int | None, int | None, str | None, str | None, str | None]:
    from_ats = _compensation_from_ats_evidence(decision.ats_evidence)
    if from_ats[4] is not None:
        return from_ats
    compensation = decision.enriched.compensation
    if compensation is None:
        return (None, None, None, None, None)
    if compensation.minimum is None and compensation.maximum is None:
        return (None, None, None, None, None)
    return (
        compensation.minimum,
        compensation.maximum,
        compensation.currency,
        compensation.period,
        "llm",
    )


def _compensation_from_ats_evidence(
    ats_evidence: JsonValue | None,
) -> tuple[int | None, int | None, str | None, str | None, str | None]:
    if not isinstance(ats_evidence, dict):
        return (None, None, None, None, None)
    compensation = ats_evidence.get("compensation")
    if not isinstance(compensation, dict):
        return (None, None, None, None, None)
    minimum = compensation.get("minimum")
    maximum = compensation.get("maximum")
    if not isinstance(minimum, int) and not isinstance(maximum, int):
        return (None, None, None, None, None)
    currency = compensation.get("currency")
    period = compensation.get("period")
    return (
        minimum if isinstance(minimum, int) else None,
        maximum if isinstance(maximum, int) else None,
        currency if isinstance(currency, str) else None,
        period if isinstance(period, str) else None,
        "ats",
    )


def _insert_decision(
    connection: psycopg.Connection[tuple[object, ...]],
    decision: TerminalDecision,
    snapshot_id: str,
) -> PersistedDecision:
    decision_id = decision_digest(
        [
            snapshot_id,
            str(decision.context.prompt_release_id),
            decision.context.relevance_release_id,
            decision.context.policy_version,
            decision.context.reevaluation_request_key,
        ]
    )
    _ = connection.execute(
        """
        INSERT INTO evaluation_decisions (
          id, snapshot_id, pipeline_run_id, prompt_release_id, relevance_release_id,
          policy_version, outcome, matched_profile, reason, created_at, decision_stage,
          source_snapshot_id, predecessor_decision_id, reevaluation_request_key
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT DO NOTHING
        """,
        (
            decision_id,
            snapshot_id,
            decision.context.pipeline_run_id,
            decision.context.prompt_release_id,
            decision.context.relevance_release_id,
            decision.context.policy_version,
            decision.outcome,
            decision.matched_profile,
            decision.reason,
            decision.context.observed_at,
            decision.decision_stage,
            decision.context.source_snapshot_id,
            decision.context.predecessor_decision_id,
            decision.context.reevaluation_request_key,
        ),
    )
    row = connection.execute(
        """
        SELECT d.id, d.snapshot_id, d.outcome, d.matched_profile, d.reason,
               s.title, s.company, s.description, s.location, d.decision_stage
        FROM evaluation_decisions d
        JOIN job_snapshots s ON s.id = d.snapshot_id
        WHERE d.snapshot_id = %s AND d.prompt_release_id = %s
          AND d.relevance_release_id IS NOT DISTINCT FROM %s
          AND d.policy_version = %s
          AND d.reevaluation_request_key IS NOT DISTINCT FROM %s
        """,
        (
            snapshot_id,
            decision.context.prompt_release_id,
            decision.context.relevance_release_id,
            decision.context.policy_version,
            decision.context.reevaluation_request_key,
        ),
    ).fetchone()
    if row is None:
        raise RuntimeError("Could not read the terminal decision")
    outcome_value = _parse_decision_outcome(str(row[2]))
    return PersistedDecision(
        decision_id=str(row[0]),
        snapshot_id=str(row[1]),
        outcome=outcome_value,
        matched_profile=str(row[3]) if row[3] is not None else None,
        reason=str(row[4]),
        job=EnrichedJob(
            title=str(row[5]),
            company=str(row[6]),
            description=str(row[7]),
            location=str(row[8]),
        ),
        decision_stage=_parse_decision_stage(str(row[9])),
    )


def _parse_decision_stage(value: str) -> DecisionStage:
    match value:
        case "ats_structural":
            return "ats_structural"
        case "structural":
            return "structural"
        case "evaluation":
            return "evaluation"
        case "qualified":
            return "qualified"
        case "company_policy":
            return "company_policy"
        case _:
            raise RuntimeError(f"Unknown decision stage: {value}")


def _parse_decision_outcome(value: str) -> DecisionOutcome:
    match value:
        case "qualified":
            return "qualified"
        case "rejected":
            return "rejected"
        case "duplicate":
            return "duplicate"
        case "company_blocked":
            return "company_blocked"
        case "company_applied":
            return "company_applied"
        case _:
            raise RuntimeError(f"Unknown decision outcome: {value}")


def _insert_receipt(
    connection: psycopg.Connection[tuple[object, ...]],
    decision: TerminalDecision,
    persisted: PersistedDecision,
) -> None:
    output = persisted.model_dump(mode="json")
    output_digest = decision_digest(output)
    receipt_id = decision_digest([decision.idempotency_key, output_digest])
    _ = connection.execute(
        """
        INSERT INTO pipeline_receipts (
          id, idempotency_key, pipeline_run_id, job_id, operation_key,
          input_digest, output_digest, output, implementation_ref,
          prompt_release_id, completed_at
        ) VALUES (%s, %s, %s, %s, 'process_qualified_job', %s, %s, %s, %s, %s, %s)
        ON CONFLICT (idempotency_key) DO NOTHING
        """,
        (
            receipt_id,
            decision.idempotency_key,
            decision.context.pipeline_run_id,
            decision.job_id,
            decision.input_digest,
            output_digest,
            Jsonb(output),
            decision.context.implementation_ref,
            decision.context.prompt_release_id,
            decision.context.observed_at,
        ),
    )
