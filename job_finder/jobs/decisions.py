from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, JsonValue

from job_finder.evaluation.models import (
    EvaluationModel,
    OperationalFailure,
    PromptAccepted,
    PromptReleaseId,
    Qualified,
    RetryableOperationalError,
    RelevanceReleaseId,
    TerminalOperationalError,
)
from job_finder.jobs.enrichment import EnrichedJob
from job_finder.jobs.listings import JobListing, job_id_for_url
from job_finder.jobs.title_deduplication import TitleDuplicate

DecisionOutcome = Literal[
    "qualified", "rejected", "duplicate", "company_blocked", "company_applied"
]
DecisionStage = Literal["ats_structural", "structural", "evaluation", "qualified", "company_policy"]
CompanyPolicy = Literal["blocked", "recent_application"]
Enricher = Callable[[JobListing], PromptAccepted[EnrichedJob] | OperationalFailure]
TitleDeduplicator = Callable[
    [str, tuple[str, ...]], PromptAccepted[TitleDuplicate] | OperationalFailure
]


class PersistedDecision(EvaluationModel):
    kind: Literal["persisted"] = "persisted"
    decision_id: str
    snapshot_id: str
    outcome: DecisionOutcome
    matched_profile: str | None
    reason: str
    job: EnrichedJob
    decision_stage: DecisionStage = "qualified"


class RetryableDecisionError(RetryableOperationalError):
    stage: Literal["enrichment", "deduplication"]


class TerminalDecisionError(TerminalOperationalError):
    stage: Literal["enrichment", "deduplication"]


DecisionFailure = Annotated[
    RetryableDecisionError | TerminalDecisionError,
    Field(discriminator="kind"),
]
DecisionResult = Annotated[
    PersistedDecision | RetryableDecisionError | TerminalDecisionError,
    Field(discriminator="kind"),
]


@dataclass(frozen=True)
class DecisionContext:
    pipeline_run_id: UUID
    prompt_release_id: PromptReleaseId
    policy_version: str
    implementation_ref: str
    observed_at: datetime
    relevance_release_id: RelevanceReleaseId | None = None
    source_snapshot_id: str | None = None
    predecessor_decision_id: str | None = None
    reevaluation_request_key: str | None = None


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
    decision_stage: DecisionStage = "qualified"
    ats_evidence: JsonValue | None = None


CompletedDecisionLookup = Callable[[str], PersistedDecision | None]
ExistingTitleLookup = Callable[[str], tuple[str, ...]]
CompanyPolicyLookup = Callable[[str, datetime], CompanyPolicy | None]
TerminalDecisionWriter = Callable[..., PersistedDecision]


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
    *,
    ats_evidence: JsonValue | None = None,
) -> DecisionResult:
    input_digest = _terminal_input_digest(listing, evaluation, context, ats_evidence)
    idempotency_key = f"qualified-decision:{input_digest}"
    completed = store.find_completed(idempotency_key)
    if completed is not None:
        return completed

    enrichment = enrich(listing)
    if isinstance(enrichment, (RetryableOperationalError, TerminalOperationalError)):
        return _decision_failure("enrichment", enrichment)

    enriched = enrichment.output
    normalized_company = normalize_ledger_text(enriched.company)
    titles = store.existing_titles(normalized_company)
    duplicate = deduplicate(enriched.title, titles)
    if isinstance(duplicate, (RetryableOperationalError, TerminalOperationalError)):
        return _decision_failure("deduplication", duplicate)

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
            ats_evidence=ats_evidence,
        )
    )


def persist_rejected_job(
    listing: JobListing,
    reason: str,
    decision_stage: Literal["ats_structural", "structural", "evaluation"],
    context: DecisionContext,
    store: DecisionStore,
    *,
    ats_evidence: JsonValue | None = None,
) -> PersistedDecision:
    return _persist_pre_evaluation_decision(
        listing,
        outcome="rejected",
        reason=reason,
        decision_stage=decision_stage,
        context=context,
        store=store,
        ats_evidence=ats_evidence,
    )


def persist_suppressed_job(
    listing: JobListing,
    policy: CompanyPolicy,
    context: DecisionContext,
    store: DecisionStore,
) -> PersistedDecision:
    outcome, reason = _policy_outcome(policy)
    return _persist_pre_evaluation_decision(
        listing,
        outcome=outcome,
        reason=reason,
        decision_stage="company_policy",
        context=context,
        store=store,
    )


def _persist_pre_evaluation_decision(
    listing: JobListing,
    *,
    outcome: Literal["rejected", "company_blocked", "company_applied"],
    reason: str,
    decision_stage: Literal["ats_structural", "structural", "evaluation", "company_policy"],
    context: DecisionContext,
    store: DecisionStore,
    ats_evidence: JsonValue | None = None,
) -> PersistedDecision:
    input_digest = decision_digest(
        {
            "listing": listing.model_dump(mode="json"),
            "reason": reason,
            "decision_stage": decision_stage,
            "prompt_release_id": str(context.prompt_release_id),
            "relevance_release_id": context.relevance_release_id,
            "policy_version": context.policy_version,
            "implementation_ref": context.implementation_ref,
            "reevaluation_request_key": context.reevaluation_request_key,
            "ats_evidence": ats_evidence,
        }
    )
    idempotency_key = (
        f"suppressed-decision:{input_digest}"
        if decision_stage == "company_policy"
        else f"rejected-decision:{input_digest}"
    )
    completed = store.find_completed(idempotency_key)
    if completed is not None:
        return completed
    return store.persist(
        TerminalDecision(
            idempotency_key=idempotency_key,
            input_digest=input_digest,
            job_id=job_id_for_url(listing.url),
            listing=listing,
            enriched=EnrichedJob(
                title=listing.title,
                company=listing.company,
                description=listing.description,
                location=listing.location,
            ),
            outcome=outcome,
            matched_profile=None,
            reason=reason,
            context=context,
            decision_stage=decision_stage,
            ats_evidence=ats_evidence,
        )
    )


def normalize_ledger_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).lower()


def _decision_failure(
    stage: Literal["enrichment", "deduplication"],
    failure: OperationalFailure,
) -> DecisionFailure:
    if isinstance(failure, RetryableOperationalError):
        return RetryableDecisionError(
            stage=stage,
            prompt_name=failure.prompt_name,
            error_code=failure.error_code,
            reason=failure.reason,
        )
    return TerminalDecisionError(
        stage=stage,
        prompt_name=failure.prompt_name,
        error_code=failure.error_code,
        reason=failure.reason,
    )


def _terminal_outcome(
    evaluation: Qualified,
    duplicate: TitleDuplicate,
    policy: CompanyPolicy | None,
) -> tuple[DecisionOutcome, str | None, str]:
    if duplicate.is_duplicate:
        match = duplicate.matched_title or "an existing role"
        return "duplicate", None, f"Duplicate of {match}"
    if policy is not None:
        outcome, reason = _policy_outcome(policy)
        return outcome, None, reason
    return "qualified", evaluation.profile_name, evaluation.reason


def _policy_outcome(
    policy: CompanyPolicy,
) -> tuple[Literal["company_blocked", "company_applied"], str]:
    match policy:
        case "blocked":
            return "company_blocked", "Company is blocked"
        case "recent_application":
            return "company_applied", "A recent company application is active"


def _terminal_input_digest(
    listing: JobListing,
    evaluation: Qualified,
    context: DecisionContext,
    ats_evidence: JsonValue | None,
) -> str:
    value = {
        "listing": listing.model_dump(mode="json"),
        "evaluation": evaluation.model_dump(mode="json"),
        "prompt_release_id": str(context.prompt_release_id),
        "relevance_release_id": context.relevance_release_id,
        "policy_version": context.policy_version,
        "implementation_ref": context.implementation_ref,
        "reevaluation_request_key": context.reevaluation_request_key,
        "ats_evidence": ats_evidence,
    }
    return decision_digest(value)


def decision_digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
