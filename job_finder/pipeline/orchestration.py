from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import ClassVar, Literal, assert_never
from uuid import UUID

from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter

from job_finder.ats.client import fetch_ats_data
from job_finder.ats.models import AtsAvailable, AtsEvidence, AtsNotApplicable
from job_finder.ats.policy import ats_structural_filter, format_ats_description
from job_finder.discovery.exchange_rates import format_compensation_rates
from job_finder.discovery.jina import (
    JinaUnavailable,
    ScrapeResult,
    SearchResult,
    scrape_job,
    search_jobs,
)
from job_finder.evaluation.evaluate import evaluate_job
from job_finder.evaluation.jev import (
    JevRetryPolicy,
    JevSender,
    evaluate_persisted_prompt as evaluate_persisted_jev_prompt,
)
from job_finder.evaluation.models import (
    CriterionResult,
    OperationalError,
    OperationalFailure,
    PromptAccepted,
    Rejected,
    RetryableOperationalError,
    TerminalOperationalError,
)
from job_finder.evaluation.openrouter import (
    ChatCompletionSender,
    RetryPolicy,
    evaluate_prompt as evaluate_persisted_openrouter_prompt,
    postgres_model_call_persistence,
    prompt_input_digest,
)
from job_finder.evaluation.prompt_releases import PromptRelease, PromptVersion
from job_finder.evaluation.release_targets import load_release_target
from job_finder.evaluation.relevance_releases import (
    GeminiExecutionPolicy,
    JevAtomicExecutionPolicy,
    JevFaithfulExecutionPolicy,
    RelevanceExecutionPolicy,
)
from job_finder.jobs.decision_pipeline import (
    THIN_BODY_THRESHOLD,
    DecisionContext,
    DecisionStore,
    PersistedDecision,
    TerminalDecision,
    normalize_ledger_text,
    persist_rejected_job,
    persist_suppressed_job,
    postgres_decision_store,
    process_qualified_job,
)
from job_finder.jobs.enrichment import EnrichedJob, enrich_job, enrichment_values
from job_finder.jobs.models import JobListing, StructuralRejection
from job_finder.jobs.scraping import parse_job_details
from job_finder.jobs.structural_filter import structural_filter
from job_finder.jobs.title_deduplication import (
    TitleDuplicate,
    deduplicate_title,
    title_deduplication_values,
)
from job_finder.pipeline.connection import Connection
from job_finder.pipeline.state import (
    JOB_WORK_ATTEMPT_LIMIT,
    JobWorkClaim,
    claim_next_job,
    complete_job_claim,
    complete_model_call_context,
    ensure_model_call_context,
    fail_job_claim,
    fail_model_call_context,
    find_terminal_decision_id,
    terminally_fail_job_claim,
)
from job_finder.pipeline.runs import OrchestrationRun, load_processing_run
from job_finder.pipeline.discoveries import register_discoveries
from job_finder.review.queue import enqueue_qualified_review_item
from job_finder.search_configuration import (
    SearchQuery,
    build_search_queries,
    load_search_configuration_revision,
)

POLICY_VERSION = "orchestration-v1"
_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
_KEYWORDS: TypeAdapter[tuple[str, ...]] = TypeAdapter(tuple[str, ...])
_ATS_EVIDENCE: TypeAdapter[AtsEvidence] = TypeAdapter(AtsEvidence)
SearchBoundary = Callable[[str, str], SearchResult]
ScrapeBoundary = Callable[[str], ScrapeResult]
AtsBoundary = Callable[[str, str | None], AtsEvidence]
Now = Callable[[], datetime]


@dataclass(frozen=True)
class PipelineBoundaries:
    search: SearchBoundary
    scrape: ScrapeBoundary
    fetch_ats: AtsBoundary
    model_sender: ChatCompletionSender | None = None
    model_retry_policy: RetryPolicy | None = None
    jev_sender: JevSender | None = None
    jev_retry_policy: JevRetryPolicy | None = None
    model_call_started: Callable[[str], None] | None = None


class PipelineServiceModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class DiscoverySummary(PipelineServiceModel):
    query_count: int
    unavailable_query_count: int
    discovered_count: int
    new_work_count: int

    def require_complete(self, *, max_unavailable_ratio: float = 0.2) -> None:
        if self.query_count == 0 or self.unavailable_query_count == self.query_count:
            raise RuntimeError("Every discovery query remained unavailable")
        if self.unavailable_query_count / self.query_count > max_unavailable_ratio:
            raise RuntimeError(
                f"{self.unavailable_query_count} of {self.query_count} discovery queries remained unavailable"
            )


class ProcessingSummary(PipelineServiceModel):
    claimed_count: int
    terminal_count: int
    terminal_error_count: int
    retry_scheduled_count: int
    lease_lost_count: int


class LeaseOwnershipLost(RuntimeError):
    pass


def production_boundaries(
    *,
    jina_api_key: str,
    model_sender: ChatCompletionSender | None = None,
) -> PipelineBoundaries:
    ats_cache: dict[str, JsonValue] = {}
    return PipelineBoundaries(
        search=lambda keyword, domain: search_jobs(keyword, domain, api_key=jina_api_key),
        scrape=lambda url: scrape_job(url, api_key=jina_api_key),
        fetch_ats=lambda url, title: fetch_ats_data(url, title=title, ashby_cache=ats_cache),
        model_sender=model_sender,
    )


def discover_jobs(
    connection: Connection,
    run: OrchestrationRun,
    boundaries: PipelineBoundaries,
    *,
    discovered_at: datetime,
    max_workers: int,
) -> DiscoverySummary:
    if run.status != "running":
        raise ValueError("Discovery requires a running orchestration run")
    if max_workers < 1:
        raise ValueError("Discovery requires at least one search worker")
    configuration = load_search_configuration_revision(
        connection, run.configuration_revision_id
    ).configuration
    queries = build_search_queries(configuration)

    def run_search(query: SearchQuery) -> SearchResult:
        return boundaries.search(query.keyword, query.domain)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        results = tuple(executor.map(run_search, queries))
    unavailable_count = 0
    discovered_count = 0
    new_work_count = 0
    for query, result in zip(queries, results, strict=True):
        if isinstance(result, JinaUnavailable):
            unavailable_count += 1
            continue
        registration = register_discoveries(
            connection,
            run_id=run.id,
            keyword=query.keyword,
            domain=query.domain,
            raw_urls=result.urls,
            discovered_at=discovered_at,
        )
        discovered_count += registration.discovered_count
        new_work_count += registration.new_work_count
    return DiscoverySummary(
        query_count=len(queries),
        unavailable_query_count=unavailable_count,
        discovered_count=discovered_count,
        new_work_count=new_work_count,
    )


def process_claimed_jobs(
    connection: Connection,
    run: OrchestrationRun,
    boundaries: PipelineBoundaries,
    *,
    openrouter_api_key: str,
    typesafe_api_key: str | None = None,
    owner_token: UUID,
    observed_at: datetime,
    max_items: int,
    lease_for: timedelta,
    retry_after: timedelta,
    enable_ats_enrichment: bool,
    onboarding_request_key: str | None = None,
    attempt_limit: int = JOB_WORK_ATTEMPT_LIMIT,
    on_claim: Callable[[JobWorkClaim], None] | None = None,
    now: Now = lambda: datetime.now(UTC),
) -> ProcessingSummary:
    if run.status != "running":
        raise ValueError("Processing requires a running orchestration run")
    if max_items < 1:
        raise ValueError("Processing requires at least one work item")
    release, relevance_release = load_release_target(connection, run.target)
    counts: dict[Literal["terminal", "terminal_error", "retry", "lease_lost"], int] = {
        "terminal": 0,
        "terminal_error": 0,
        "retry": 0,
        "lease_lost": 0,
    }
    claimed_count = 0
    for _ in range(max_items):
        claim = claim_next_job(
            connection,
            owner_token=owner_token,
            claimed_at=now(),
            lease_for=lease_for,
            onboarding_request_key=onboarding_request_key,
            attempt_limit=attempt_limit,
        )
        if claim is None:
            break
        claimed_count += 1
        if on_claim is not None:
            on_claim(claim)
        try:
            execution_run = run
            execution_release = release
            execution_relevance_policy = relevance_release.policy
            if claim.reevaluation_pipeline_run_id is not None:
                execution_run = load_processing_run(connection, claim.reevaluation_pipeline_run_id)
                if execution_run.status != "running":
                    raise RuntimeError("Reevaluation run is not active")
                execution_release, execution_relevance = load_release_target(
                    connection, execution_run.target
                )
                execution_relevance_policy = execution_relevance.policy
            outcome = _process_claim(
                connection,
                execution_run,
                execution_release,
                execution_relevance_policy,
                claim,
                boundaries,
                openrouter_api_key=openrouter_api_key,
                typesafe_api_key=typesafe_api_key,
                observed_at=observed_at,
                retry_after=retry_after,
                enable_ats_enrichment=enable_ats_enrichment,
                now=now,
            )
        except Exception as error:
            _ = fail_job_claim(
                connection,
                claim,
                failed_at=now(),
                retry_after=retry_after,
                error_code=type(error).__name__,
                reason=str(error),
            )
            raise
        counts[outcome] += 1
    return ProcessingSummary(
        claimed_count=claimed_count,
        terminal_count=counts["terminal"],
        terminal_error_count=counts["terminal_error"],
        retry_scheduled_count=counts["retry"],
        lease_lost_count=counts["lease_lost"],
    )


def _process_claim(
    connection: Connection,
    run: OrchestrationRun,
    release: PromptRelease,
    relevance_policy: RelevanceExecutionPolicy,
    claim: JobWorkClaim,
    boundaries: PipelineBoundaries,
    *,
    openrouter_api_key: str,
    typesafe_api_key: str | None,
    observed_at: datetime,
    retry_after: timedelta,
    enable_ats_enrichment: bool,
    now: Now,
) -> Literal["terminal", "terminal_error", "retry", "lease_lost"]:
    existing_decision_id = find_terminal_decision_id(connection, claim.job_id)
    if claim.reevaluation_request_key is None and existing_decision_id is not None:
        completed = complete_job_claim(
            connection,
            claim,
            decision_id=existing_decision_id,
            completed_at=now(),
        )
        return "terminal" if completed else "lease_lost"

    acquired = _load_claim_listing(connection, claim, boundaries, observed_at)
    if isinstance(acquired, JinaUnavailable):
        return _schedule_retry(
            connection, claim, acquired.error_code, acquired.reason, now(), retry_after
        )
    listing, pinned_ats_evidence = acquired
    decision_context = DecisionContext(
        pipeline_run_id=run.id,
        prompt_release_id=run.prompt_release_id,
        policy_version=POLICY_VERSION,
        implementation_ref=run.implementation_ref,
        observed_at=observed_at,
        relevance_release_id=run.target.relevance_release_id,
        source_snapshot_id=claim.source_snapshot_id,
        predecessor_decision_id=claim.predecessor_decision_id,
        reevaluation_request_key=claim.reevaluation_request_key,
    )
    store = _claim_completing_store(connection, claim, now)
    company_policy = store.active_company_policy(
        normalize_ledger_text(listing.company), observed_at
    )
    if company_policy is not None:
        _ = persist_suppressed_job(listing, company_policy, decision_context, store)
        return "terminal"

    listing, ats_evidence, ats_json, body = _resolve_claim_ats(
        claim,
        listing,
        pinned_ats_evidence,
        boundaries,
        enable_ats_enrichment=enable_ats_enrichment,
    )
    ats_decision = ats_structural_filter(ats_evidence)
    if isinstance(ats_decision, StructuralRejection):
        _ = persist_rejected_job(
            listing,
            ats_decision.reason,
            "ats_structural",
            decision_context,
            store,
            ats_evidence=ats_json,
        )
        return "terminal"
    structural_decision = structural_filter(listing)
    if isinstance(structural_decision, StructuralRejection):
        _ = persist_rejected_job(
            listing,
            structural_decision.reason,
            "structural",
            decision_context,
            store,
            ats_evidence=ats_json,
        )
        return "terminal"

    if len(body.strip()) < THIN_BODY_THRESHOLD:
        return _schedule_retry(
            connection,
            claim,
            "thin_scrape",
            "Scrape produced no usable job body",
            now(),
            retry_after,
        )
    if isinstance(relevance_policy, GeminiExecutionPolicy):
        relevance_api_key = openrouter_api_key
    else:
        if not typesafe_api_key:
            raise ValueError("TYPESAFE_API_KEY is required for relevance evaluation")
        relevance_api_key = typesafe_api_key
    evaluation = evaluate_job(
        listing,
        release,
        lambda prompt, values: _evaluate_criterion(
            connection,
            run,
            claim,
            prompt,
            values,
            relevance_policy,
            boundaries,
            relevance_api_key,
            now,
        ),
        rates=format_compensation_rates(run.exchange_rates.rates),
    )
    if isinstance(evaluation, RetryableOperationalError):
        return _schedule_retry(
            connection,
            claim,
            evaluation.error_code,
            evaluation.reason,
            now(),
            retry_after,
        )
    if isinstance(evaluation, TerminalOperationalError):
        return _record_terminal_error(connection, claim, evaluation, now())
    if isinstance(evaluation, Rejected):
        _ = persist_rejected_job(
            listing,
            evaluation.reason,
            "evaluation",
            decision_context,
            store,
            ats_evidence=ats_json,
        )
        return "terminal"
    decision = process_qualified_job(
        listing,
        evaluation,
        decision_context,
        store,
        lambda job: _enrich(
            connection,
            run,
            claim,
            release,
            job,
            boundaries,
            openrouter_api_key,
            now,
        ),
        lambda title, titles: _deduplicate(
            connection,
            run,
            claim,
            release,
            title,
            titles,
            boundaries,
            openrouter_api_key,
            now,
        ),
        ats_evidence=ats_json,
    )
    if isinstance(decision, RetryableOperationalError):
        return _schedule_retry(
            connection,
            claim,
            decision.error_code,
            decision.reason,
            now(),
            retry_after,
        )
    if isinstance(decision, TerminalOperationalError):
        return _record_terminal_error(connection, claim, decision, now())
    return "terminal"


def _evaluate_criterion(
    connection: Connection,
    run: OrchestrationRun,
    claim: JobWorkClaim,
    prompt: PromptVersion,
    values: Mapping[str, str],
    relevance_policy: RelevanceExecutionPolicy,
    boundaries: PipelineBoundaries,
    api_key: str,
    now: Now,
) -> CriterionResult:
    if boundaries.model_call_started is not None:
        boundaries.model_call_started(f"evaluation:{prompt.definition.name}")
    context = ensure_model_call_context(
        connection,
        run_id=run.id,
        job_id=claim.job_id,
        operation_key=f"evaluation:{prompt.definition.name}",
        input_digest=prompt_input_digest(values),
        started_at=now(),
        prompt_release_id=run.prompt_release_id,
    )
    match relevance_policy:
        case GeminiExecutionPolicy():
            result = evaluate_persisted_openrouter_prompt(
                prompt,
                values,
                context,
                postgres_model_call_persistence(connection),
                api_key=api_key,
                sender=boundaries.model_sender,
                retry_policy=boundaries.model_retry_policy,
                now=now,
            )
        case JevAtomicExecutionPolicy() | JevFaithfulExecutionPolicy():
            result = evaluate_persisted_jev_prompt(
                prompt,
                values,
                context,
                postgres_model_call_persistence(connection, provider="typesafe"),
                api_key=api_key,
                execution_policy=relevance_policy,
                sender=boundaries.jev_sender,
                retry_policy=boundaries.jev_retry_policy,
                now=now,
            )
        case _:
            assert_never(relevance_policy)
    if isinstance(result, OperationalError):
        fail_model_call_context(connection, context, result, completed_at=now())
    else:
        complete_model_call_context(connection, context, completed_at=now())
    return result


def _enrich(
    connection: Connection,
    run: OrchestrationRun,
    claim: JobWorkClaim,
    release: PromptRelease,
    job: JobListing,
    boundaries: PipelineBoundaries,
    api_key: str,
    now: Now,
) -> PromptAccepted[EnrichedJob] | OperationalFailure:
    if boundaries.model_call_started is not None:
        boundaries.model_call_started("enrichment")
    values = enrichment_values(job)
    context = ensure_model_call_context(
        connection,
        run_id=run.id,
        job_id=claim.job_id,
        operation_key="enrichment",
        input_digest=prompt_input_digest(values),
        started_at=now(),
        prompt_release_id=run.prompt_release_id,
    )
    result = enrich_job(
        job,
        release,
        context,
        postgres_model_call_persistence(connection),
        api_key=api_key,
        sender=boundaries.model_sender,
        retry_policy=boundaries.model_retry_policy,
    )
    if isinstance(result, OperationalError):
        fail_model_call_context(connection, context, result, completed_at=now())
    else:
        complete_model_call_context(connection, context, completed_at=now())
    return result


def _deduplicate(
    connection: Connection,
    run: OrchestrationRun,
    claim: JobWorkClaim,
    release: PromptRelease,
    new_title: str,
    existing_titles: tuple[str, ...],
    boundaries: PipelineBoundaries,
    api_key: str,
    now: Now,
) -> PromptAccepted[TitleDuplicate] | OperationalFailure:
    if boundaries.model_call_started is not None:
        boundaries.model_call_started("deduplication")
    values = title_deduplication_values(new_title, existing_titles)
    context = ensure_model_call_context(
        connection,
        run_id=run.id,
        job_id=claim.job_id,
        operation_key="deduplication",
        input_digest=prompt_input_digest(values),
        started_at=now(),
        prompt_release_id=run.prompt_release_id,
    )
    result = deduplicate_title(
        new_title,
        existing_titles,
        release,
        context,
        postgres_model_call_persistence(connection),
        api_key=api_key,
        sender=boundaries.model_sender,
        retry_policy=boundaries.model_retry_policy,
    )
    if isinstance(result, OperationalError):
        fail_model_call_context(connection, context, result, completed_at=now())
    else:
        complete_model_call_context(connection, context, completed_at=now())
    return result


def _claim_completing_store(connection: Connection, claim: JobWorkClaim, now: Now) -> DecisionStore:
    store = postgres_decision_store(connection, excluded_job_id=claim.job_id)

    def persist(decision: TerminalDecision) -> PersistedDecision:
        with connection.transaction():
            persisted = store.persist(decision, enqueue_review_item=enqueue_qualified_review_item)
            if not complete_job_claim(
                connection,
                claim,
                decision_id=persisted.decision_id,
                completed_at=now(),
            ):
                raise LeaseOwnershipLost(f"Lost the lease for {claim.job_id}")
        return persisted

    return replace(store, persist=persist)


def _load_claim_listing(
    connection: Connection,
    claim: JobWorkClaim,
    boundaries: PipelineBoundaries,
    observed_at: datetime,
) -> tuple[JobListing, JsonValue | None] | JinaUnavailable:
    if claim.source_snapshot_id is not None:
        return _load_reevaluation_listing(connection, claim.job_id, claim.source_snapshot_id)
    scrape = boundaries.scrape(claim.raw_url)
    if isinstance(scrape, JinaUnavailable):
        return scrape
    return (
        parse_job_details(
            scrape.markdown,
            claim.raw_url,
            claim.keyword,
            scraped_on=observed_at.date(),
            page_title=scrape.title,
        ),
        None,
    )


def _resolve_claim_ats(
    claim: JobWorkClaim,
    listing: JobListing,
    pinned_evidence: JsonValue | None,
    boundaries: PipelineBoundaries,
    *,
    enable_ats_enrichment: bool,
) -> tuple[JobListing, AtsEvidence, JsonValue, str]:
    if claim.source_snapshot_id is not None:
        evidence = (
            AtsNotApplicable()
            if pinned_evidence is None
            else _ATS_EVIDENCE.validate_python(pinned_evidence)
        )
        return (
            listing,
            evidence,
            _JSON.validate_python(evidence.model_dump(mode="json")),
            listing.description,
        )

    evidence = (
        boundaries.fetch_ats(listing.url, listing.title)
        if enable_ats_enrichment
        else AtsNotApplicable()
    )
    evidence_json = _JSON.validate_python(evidence.model_dump(mode="json"))
    ats_description = evidence.description if isinstance(evidence, AtsAvailable) else None
    body = ats_description if ats_description is not None else listing.description
    if isinstance(evidence, AtsAvailable):
        listing = listing.model_copy(
            update={
                "description": format_ats_description(evidence, body),
                "location": evidence.location,
            }
        )
    return listing, evidence, evidence_json, body


def _load_reevaluation_listing(
    connection: Connection, job_id: UUID, snapshot_id: str
) -> tuple[JobListing, JsonValue | None]:
    row = connection.execute(
        """
        SELECT title, company, raw_url, source, description, location,
               keywords, date_posted, observed_at, ats_evidence
        FROM job_snapshots
        WHERE id = %s AND job_id = %s
        """,
        (snapshot_id, job_id),
    ).fetchone()
    if row is None or not isinstance(row[8], datetime):
        raise RuntimeError("Pinned reevaluation snapshot is missing or invalid")
    keywords = _KEYWORDS.validate_python(row[6])
    listing = JobListing.model_validate(
        {
            "title": row[0],
            "company": row[1],
            "url": row[2],
            "source": row[3],
            "description": row[4],
            "location": row[5],
            "keywords_matched": keywords,
            "date_posted": row[7],
            "date_scraped": row[8].date(),
        }
    )
    ats_evidence = None if row[9] is None else _JSON.validate_python(row[9])
    return listing, ats_evidence


def _record_terminal_error(
    connection: Connection,
    claim: JobWorkClaim,
    failure: TerminalOperationalError,
    completed_at: datetime,
) -> Literal["terminal_error", "lease_lost"]:
    recorded = terminally_fail_job_claim(
        connection,
        claim,
        completed_at=completed_at,
        error_code=failure.error_code,
        reason=failure.reason,
    )
    return "terminal_error" if recorded else "lease_lost"


def _schedule_retry(
    connection: Connection,
    claim: JobWorkClaim,
    error_code: str,
    reason: str,
    failed_at: datetime,
    retry_after: timedelta,
) -> Literal["retry", "terminal_error", "lease_lost"]:
    return fail_job_claim(
        connection,
        claim,
        failed_at=failed_at,
        retry_after=retry_after,
        error_code=error_code,
        reason=reason,
    )
