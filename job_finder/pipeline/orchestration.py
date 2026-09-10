from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import ClassVar, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter

from job_finder.ats.client import fetch_ats_data
from job_finder.ats.models import AtsAvailable, AtsEvidence, AtsNotApplicable
from job_finder.ats.policy import ats_structural_filter, format_ats_block
from job_finder.discovery.catalog import SEARCH_DOMAINS, SEARCH_KEYWORDS
from job_finder.discovery.exchange_rates import format_compensation_rates
from job_finder.discovery.jina import (
    JinaUnavailable,
    ScrapeResult,
    SearchResult,
    scrape_job,
    search_jobs,
)
from job_finder.evaluation.evaluate import evaluate_job
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
    evaluate_prompt,
    postgres_model_call_persistence,
    prompt_input_digest,
)
from job_finder.evaluation.prompt_releases import PromptRelease, PromptVersion, load_prompt_release
from job_finder.jobs.decision_pipeline import (
    DecisionContext,
    DecisionStore,
    PersistedDecision,
    TerminalDecision,
    persist_rejected_job,
    postgres_decision_store,
    process_qualified_job,
)
from job_finder.jobs.enrichment import EnrichedJob, enrich_job, enrichment_message
from job_finder.jobs.models import JobListing, StructuralRejection
from job_finder.jobs.scraping import parse_job_details
from job_finder.jobs.structural_filter import structural_filter
from job_finder.jobs.title_deduplication import TitleDuplicate, deduplicate_title
from job_finder.pipeline.state import (
    Connection,
    JobWorkClaim,
    OrchestrationRun,
    claim_next_job,
    complete_job_claim,
    complete_model_call_context,
    ensure_model_call_context,
    fail_job_claim,
    fail_model_call_context,
    find_terminal_decision_id,
    register_discoveries,
    terminally_fail_job_claim,
)

POLICY_VERSION = "orchestration-v1"
_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
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


class PipelineServiceModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class DiscoverySummary(PipelineServiceModel):
    query_count: int
    unavailable_query_count: int
    discovered_count: int
    new_work_count: int

    def require_complete(self) -> None:
        if self.unavailable_query_count:
            raise RuntimeError(
                f"{self.unavailable_query_count} discovery queries remained unavailable"
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
    queries = tuple((keyword, domain) for keyword in SEARCH_KEYWORDS for domain in SEARCH_DOMAINS)

    def run_search(query: tuple[str, str]) -> SearchResult:
        return boundaries.search(*query)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        results = tuple(executor.map(run_search, queries))
    unavailable_count = 0
    discovered_count = 0
    new_work_count = 0
    for (keyword, domain), result in zip(queries, results, strict=True):
        if isinstance(result, JinaUnavailable):
            unavailable_count += 1
            continue
        registration = register_discoveries(
            connection,
            run_id=run.id,
            keyword=keyword,
            domain=domain,
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
    owner_token: UUID,
    observed_at: datetime,
    max_items: int,
    lease_for: timedelta,
    retry_after: timedelta,
    enable_ats_enrichment: bool,
    now: Now = lambda: datetime.now(UTC),
) -> ProcessingSummary:
    if run.status != "running":
        raise ValueError("Processing requires a running orchestration run")
    if max_items < 1:
        raise ValueError("Processing requires at least one work item")
    release = load_prompt_release(connection, run.prompt_release_id)
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
        )
        if claim is None:
            break
        claimed_count += 1
        try:
            outcome = _process_claim(
                connection,
                run,
                release,
                claim,
                boundaries,
                openrouter_api_key=openrouter_api_key,
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
    claim: JobWorkClaim,
    boundaries: PipelineBoundaries,
    *,
    openrouter_api_key: str,
    observed_at: datetime,
    retry_after: timedelta,
    enable_ats_enrichment: bool,
    now: Now,
) -> Literal["terminal", "terminal_error", "retry", "lease_lost"]:
    existing_decision_id = find_terminal_decision_id(connection, claim.job_id)
    if existing_decision_id is not None:
        completed = complete_job_claim(
            connection,
            claim,
            decision_id=existing_decision_id,
            completed_at=now(),
        )
        return "terminal" if completed else "lease_lost"

    scrape = boundaries.scrape(claim.raw_url)
    if isinstance(scrape, JinaUnavailable):
        return _schedule_retry(
            connection, claim, scrape.error_code, scrape.reason, now(), retry_after
        )
    listing = parse_job_details(
        scrape.markdown,
        claim.raw_url,
        claim.keyword,
        scraped_on=observed_at.date(),
    )
    ats_evidence = (
        boundaries.fetch_ats(listing.url, listing.title)
        if enable_ats_enrichment
        else AtsNotApplicable()
    )
    ats_json = _JSON.validate_python(ats_evidence.model_dump(mode="json"))
    if isinstance(ats_evidence, AtsAvailable):
        listing = listing.model_copy(
            update={
                "description": f"{format_ats_block(ats_evidence)}\n\n{listing.description}",
                "location": ats_evidence.location,
            }
        )
    decision_context = DecisionContext(
        pipeline_run_id=run.id,
        prompt_release_id=run.prompt_release_id,
        policy_version=POLICY_VERSION,
        implementation_ref=run.implementation_ref,
        observed_at=observed_at,
    )
    store = _claim_completing_store(connection, claim, now)

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

    evaluation = evaluate_job(
        listing,
        release,
        lambda prompt, values: _evaluate_criterion(
            connection,
            run,
            claim,
            prompt,
            values,
            boundaries,
            openrouter_api_key,
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
    boundaries: PipelineBoundaries,
    api_key: str,
    now: Now,
) -> CriterionResult:
    context = ensure_model_call_context(
        connection,
        run_id=run.id,
        job_id=claim.job_id,
        operation_key=f"evaluation:{prompt.definition.name}",
        input_digest=prompt_input_digest(values),
        started_at=now(),
        prompt_release_id=run.prompt_release_id,
    )
    result = evaluate_prompt(
        prompt,
        values,
        context,
        postgres_model_call_persistence(connection),
        api_key=api_key,
        sender=boundaries.model_sender,
        retry_policy=boundaries.model_retry_policy,
        now=now,
    )
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
    values = {"job": enrichment_message(job)}
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
    values = {
        "newTitle": new_title,
        "existingTitles": "\n".join(
            f'{index}. "{title}"' for index, title in enumerate(existing_titles, start=1)
        ),
    }
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
    store = postgres_decision_store(connection)

    def persist(decision: TerminalDecision) -> PersistedDecision:
        with connection.transaction():
            persisted = store.persist(decision)
            if not complete_job_claim(
                connection,
                claim,
                decision_id=persisted.decision_id,
                completed_at=now(),
            ):
                raise LeaseOwnershipLost(f"Lost the lease for {claim.job_id}")
        return persisted

    return replace(store, persist=persist)


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
) -> Literal["retry", "lease_lost"]:
    scheduled = fail_job_claim(
        connection,
        claim,
        failed_at=failed_at,
        retry_after=retry_after,
        error_code=error_code,
        reason=reason,
    )
    return "retry" if scheduled else "lease_lost"
