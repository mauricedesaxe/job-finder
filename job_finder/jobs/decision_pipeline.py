from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

import psycopg
from psycopg.types.json import Jsonb
from pydantic import Field

from job_finder.evaluation.models import (
    CriterionUnavailable,
    EvaluationModel,
    PromptAccepted,
    PromptReleaseId,
    Qualified,
)
from job_finder.jobs.enrichment import EnrichedJob
from job_finder.jobs.models import JobListing
from job_finder.jobs.title_deduplication import TitleDuplicate

DecisionOutcome = Literal["qualified", "duplicate", "company_blocked", "company_applied"]
CompanyPolicy = Literal["blocked", "recent_application"]
Enricher = Callable[[JobListing], PromptAccepted[EnrichedJob] | CriterionUnavailable]
TitleDeduplicator = Callable[
    [str, tuple[str, ...]], PromptAccepted[TitleDuplicate] | CriterionUnavailable
]


class PersistedDecision(EvaluationModel):
    kind: Literal["persisted"] = "persisted"
    decision_id: str
    snapshot_id: str
    outcome: DecisionOutcome
    matched_profile: str | None
    reason: str
    job: EnrichedJob


class DecisionPipelineUnavailable(EvaluationModel):
    kind: Literal["unavailable"] = "unavailable"
    stage: Literal["enrichment", "deduplication"]
    error_code: str
    reason: str


DecisionPipelineResult = Annotated[
    PersistedDecision | DecisionPipelineUnavailable, Field(discriminator="kind")
]


@dataclass(frozen=True)
class DecisionContext:
    pipeline_run_id: UUID
    prompt_release_id: PromptReleaseId
    policy_version: str
    implementation_ref: str
    observed_at: datetime


@dataclass(frozen=True)
class TerminalDecision:
    idempotency_key: str
    input_digest: str
    job_id: UUID
    listing: JobListing
    enriched: EnrichedJob
    outcome: DecisionOutcome
    matched_profile: str | None
    reason: str
    context: DecisionContext


CompletedDecisionLookup = Callable[[str], PersistedDecision | None]
ExistingTitleLookup = Callable[[str], tuple[str, ...]]
CompanyPolicyLookup = Callable[[str, datetime], CompanyPolicy | None]
TerminalDecisionWriter = Callable[[TerminalDecision], PersistedDecision]


@dataclass(frozen=True)
class DecisionStore:
    find_completed: CompletedDecisionLookup
    existing_titles: ExistingTitleLookup
    active_company_policy: CompanyPolicyLookup
    persist: TerminalDecisionWriter


def process_qualified_job(
    listing: JobListing,
    evaluation: Qualified,
    context: DecisionContext,
    store: DecisionStore,
    enrich: Enricher,
    deduplicate: TitleDeduplicator,
) -> DecisionPipelineResult:
    input_digest = _terminal_input_digest(listing, evaluation, context)
    idempotency_key = f"qualified-decision:{input_digest}"
    completed = store.find_completed(idempotency_key)
    if completed is not None:
        return completed

    enrichment = enrich(listing)
    if isinstance(enrichment, CriterionUnavailable):
        return DecisionPipelineUnavailable(
            stage="enrichment",
            error_code=enrichment.error_code,
            reason=enrichment.reason,
        )

    enriched = enrichment.output
    normalized_company = normalize_ledger_text(enriched.company)
    titles = store.existing_titles(normalized_company)
    duplicate = deduplicate(enriched.title, titles)
    if isinstance(duplicate, CriterionUnavailable):
        return DecisionPipelineUnavailable(
            stage="deduplication",
            error_code=duplicate.error_code,
            reason=duplicate.reason,
        )

    policy = store.active_company_policy(normalized_company, context.observed_at)
    outcome, matched_profile, reason = _terminal_outcome(evaluation, duplicate.output, policy)
    return store.persist(
        TerminalDecision(
            idempotency_key=idempotency_key,
            input_digest=input_digest,
            job_id=job_id_for_url(listing.url),
            listing=listing,
            enriched=enriched,
            outcome=outcome,
            matched_profile=matched_profile,
            reason=reason,
            context=context,
        )
    )


def postgres_decision_store(
    connection: psycopg.Connection[tuple[object, ...]],
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
            ORDER BY s.normalized_title, s.title
            """,
            (normalized_company,),
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

    def persist(decision: TerminalDecision) -> PersistedDecision:
        with connection.transaction():
            _upsert_job(connection, decision)
            snapshot_id = _insert_snapshot(connection, decision)
            authoritative = _insert_decision(connection, decision, snapshot_id)
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


def job_id_for_url(raw_url: str) -> UUID:
    return uuid5(NAMESPACE_URL, raw_url)


def normalize_ledger_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).lower()


def _terminal_outcome(
    evaluation: Qualified,
    duplicate: TitleDuplicate,
    policy: CompanyPolicy | None,
) -> tuple[DecisionOutcome, str | None, str]:
    if duplicate.is_duplicate:
        match = duplicate.matched_title or "an existing role"
        return "duplicate", None, f"Duplicate of {match}"
    if policy == "blocked":
        return "company_blocked", None, "Company is blocked"
    if policy == "recent_application":
        return "company_applied", None, "A recent company application is active"
    return "qualified", evaluation.profile_name, evaluation.reason


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
    }
    content_digest = _digest(snapshot)
    snapshot_id = _digest([str(decision.job_id), content_digest])
    _ = connection.execute(
        """
        INSERT INTO job_snapshots (
          id, job_id, content_digest, title, company, normalized_company,
          normalized_title, source, raw_url, description, location, keywords,
          date_posted, observed_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
        ),
    )
    return snapshot_id


def _insert_decision(
    connection: psycopg.Connection[tuple[object, ...]],
    decision: TerminalDecision,
    snapshot_id: str,
) -> PersistedDecision:
    decision_id = _digest(
        [snapshot_id, str(decision.context.prompt_release_id), decision.context.policy_version]
    )
    _ = connection.execute(
        """
        INSERT INTO evaluation_decisions (
          id, snapshot_id, pipeline_run_id, prompt_release_id, policy_version,
          outcome, matched_profile, reason, created_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (snapshot_id, prompt_release_id, policy_version) DO NOTHING
        """,
        (
            decision_id,
            snapshot_id,
            decision.context.pipeline_run_id,
            decision.context.prompt_release_id,
            decision.context.policy_version,
            decision.outcome,
            decision.matched_profile,
            decision.reason,
            decision.context.observed_at,
        ),
    )
    row = connection.execute(
        """
        SELECT d.id, d.snapshot_id, d.outcome, d.matched_profile, d.reason,
               s.title, s.company, s.description, s.location
        FROM evaluation_decisions d
        JOIN job_snapshots s ON s.id = d.snapshot_id
        WHERE d.snapshot_id = %s AND d.prompt_release_id = %s AND d.policy_version = %s
        """,
        (
            snapshot_id,
            decision.context.prompt_release_id,
            decision.context.policy_version,
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
    )


def _parse_decision_outcome(value: str) -> DecisionOutcome:
    match value:
        case "qualified":
            return "qualified"
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
    output_digest = _digest(output)
    receipt_id = _digest([decision.idempotency_key, output_digest])
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


def _terminal_input_digest(
    listing: JobListing, evaluation: Qualified, context: DecisionContext
) -> str:
    value = {
        "listing": listing.model_dump(mode="json"),
        "evaluation": evaluation.model_dump(mode="json"),
        "prompt_release_id": str(context.prompt_release_id),
        "policy_version": context.policy_version,
        "implementation_ref": context.implementation_ref,
    }
    return _digest(value)


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
