from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, date, datetime
from uuid import uuid4

from job_finder.evaluation.models import (
    CriterionUnavailable,
    InputDigest,
    ModelCallContext,
    PromptAccepted,
    PromptReleaseId,
    Qualified,
)
from job_finder.evaluation.openrouter import HttpResponse, ModelCallPersistence, prompt_input_digest
from job_finder.evaluation.prompt_releases import build_prompt_release
from job_finder.jobs.decision_pipeline import (
    CompanyPolicy,
    DecisionContext,
    DecisionOutcome,
    DecisionStore,
    PersistedDecision,
    TerminalDecision,
    normalize_ledger_text,
    process_qualified_job,
)
from job_finder.jobs.enrichment import EnrichedJob, enrich_job, enrichment_message
from job_finder.jobs.models import JobListing
from job_finder.jobs.title_deduplication import TitleDuplicate, deduplicate_title


def test_calls_the_enrichment_prompt_with_the_legacy_input_shape() -> None:
    listing = _listing()
    release = build_prompt_release()
    values = {"job": enrichment_message(listing)}
    sent_body: dict[str, object] = {}

    def send(
        _url: str,
        _headers: Mapping[str, str],
        body: dict[str, object],
        _timeout: float,
    ) -> HttpResponse:
        sent_body.update(body)
        return _completion(
            "enrich_job",
            {
                "title": "Senior Engineer",
                "company": "Acme",
                "description": "## Overview\nBuild things.",
                "location": "Remote",
            },
        )

    result = enrich_job(
        listing,
        release,
        _model_context(prompt_input_digest(values)),
        _empty_model_persistence(),
        api_key="secret",
        sender=send,
    )

    assert isinstance(result, PromptAccepted)
    assert result.output.company == "Acme"
    assert sent_body["tool_choice"] == {
        "type": "function",
        "function": {"name": "enrich_job"},
    }
    assert enrichment_message(listing) == (
        "Job Title: Sr Eng - Acme\n"
        "Company: acme.io\n"
        "Source: other\n"
        "URL: https://example.com/jobs/1\n\n"
        "Raw Description:\nRaw description"
    )


def test_avoids_a_model_call_for_empty_and_exact_title_sets() -> None:
    release = build_prompt_release()
    context = _model_context(InputDigest("unused"))

    empty = deduplicate_title(
        "Senior Engineer",
        (),
        release,
        context,
        _empty_model_persistence(),
        api_key="secret",
        sender=_unexpected_send,
    )
    exact = deduplicate_title(
        " senior engineer ",
        ("Senior Engineer",),
        release,
        context,
        _empty_model_persistence(),
        api_key="secret",
        sender=_unexpected_send,
    )

    assert isinstance(empty, PromptAccepted)
    assert empty.output == TitleDuplicate(isDuplicate=False)
    assert isinstance(exact, PromptAccepted)
    assert exact.output == TitleDuplicate(isDuplicate=True, matchedTitle="Senior Engineer")


def test_calls_the_title_prompt_for_a_fuzzy_comparison() -> None:
    release = build_prompt_release()
    values = {
        "newTitle": "Sr Engineer",
        "existingTitles": '1. "Senior Engineer"',
    }

    def send(
        _url: str,
        _headers: Mapping[str, str],
        body: dict[str, object],
        _timeout: float,
    ) -> HttpResponse:
        assert body["tool_choice"] == {
            "type": "function",
            "function": {"name": "check_duplicate"},
        }
        return _completion(
            "check_duplicate",
            {"isDuplicate": True, "matchedTitle": "Senior Engineer"},
        )

    result = deduplicate_title(
        "Sr Engineer",
        ("Senior Engineer",),
        release,
        _model_context(prompt_input_digest(values)),
        _empty_model_persistence(),
        api_key="secret",
        sender=send,
    )

    assert isinstance(result, PromptAccepted)
    assert result.output.is_duplicate is True
    assert result.output.matched_title == "Senior Engineer"


def test_persists_the_qualified_outcome_and_reuses_its_receipt() -> None:
    listing = _listing()
    evaluation = Qualified(reason="Matches the applied AI profile", profile_name="applied-ai")
    persisted: dict[str, PersistedDecision] = {}
    enrichment_calls = 0
    deduplication_calls = 0

    def enrich(_listing: JobListing) -> PromptAccepted[EnrichedJob]:
        nonlocal enrichment_calls
        enrichment_calls += 1
        return PromptAccepted(
            prompt_name="job-finder-enrichment",
            output=_enriched(),
        )

    def deduplicate(
        _title: str, existing_titles: tuple[str, ...]
    ) -> PromptAccepted[TitleDuplicate]:
        nonlocal deduplication_calls
        deduplication_calls += 1
        assert existing_titles == ("Backend Engineer",)
        return PromptAccepted(
            prompt_name="job-finder-title-deduplication",
            output=TitleDuplicate(isDuplicate=False),
        )

    def persist(decision: TerminalDecision) -> PersistedDecision:
        result = PersistedDecision(
            decision_id="d" * 64,
            snapshot_id="s" * 64,
            outcome=decision.outcome,
            matched_profile=decision.matched_profile,
            reason=decision.reason,
            job=decision.enriched,
        )
        persisted[decision.idempotency_key] = result
        return result

    store = DecisionStore(
        find_completed=lambda key: persisted.get(key),
        existing_titles=lambda company: ("Backend Engineer",) if company == "acme" else (),
        active_company_policy=lambda _company, _at: None,
        persist=persist,
    )
    first = process_qualified_job(
        listing, evaluation, _decision_context(), store, enrich, deduplicate
    )
    second = process_qualified_job(
        listing, evaluation, _decision_context(), store, enrich, deduplicate
    )

    assert first == second
    assert isinstance(first, PersistedDecision)
    assert first.outcome == "qualified"
    assert first.matched_profile == "applied-ai"
    assert enrichment_calls == 1
    assert deduplication_calls == 1


def test_applies_duplicate_before_company_policy() -> None:
    store = DecisionStore(
        find_completed=lambda _key: None,
        existing_titles=lambda _company: ("Senior Engineer",),
        active_company_policy=lambda _company, _at: "blocked",
        persist=lambda decision: PersistedDecision(
            decision_id="d" * 64,
            snapshot_id="s" * 64,
            outcome=decision.outcome,
            matched_profile=decision.matched_profile,
            reason=decision.reason,
            job=decision.enriched,
        ),
    )
    result = process_qualified_job(
        _listing(),
        Qualified(reason="qualified", profile_name="profile"),
        _decision_context(),
        store,
        lambda _listing: PromptAccepted(prompt_name="job-finder-enrichment", output=_enriched()),
        lambda _title, _existing: PromptAccepted(
            prompt_name="job-finder-title-deduplication",
            output=TitleDuplicate(isDuplicate=True, matchedTitle="Senior Engineer"),
        ),
    )

    assert isinstance(result, PersistedDecision)
    assert result.outcome == "duplicate"
    assert result.matched_profile is None


def test_maps_active_company_policies_to_terminal_outcomes() -> None:
    cases: tuple[tuple[CompanyPolicy, DecisionOutcome], ...] = (
        ("blocked", "company_blocked"),
        ("recent_application", "company_applied"),
    )
    for policy, expected in cases:

        def active_policy(
            _company: str, _at: datetime, value: CompanyPolicy = policy
        ) -> CompanyPolicy:
            return value

        store = DecisionStore(
            find_completed=lambda _key: None,
            existing_titles=lambda _company: (),
            active_company_policy=active_policy,
            persist=lambda decision: PersistedDecision(
                decision_id="d" * 64,
                snapshot_id="s" * 64,
                outcome=decision.outcome,
                matched_profile=decision.matched_profile,
                reason=decision.reason,
                job=decision.enriched,
            ),
        )
        result = process_qualified_job(
            _listing(),
            Qualified(reason="qualified", profile_name="profile"),
            _decision_context(),
            store,
            lambda _listing: PromptAccepted(
                prompt_name="job-finder-enrichment", output=_enriched()
            ),
            lambda _title, _existing: PromptAccepted(
                prompt_name="job-finder-title-deduplication",
                output=TitleDuplicate(isDuplicate=False),
            ),
        )

        assert isinstance(result, PersistedDecision)
        assert result.outcome == expected
        assert result.matched_profile is None


def test_stops_without_a_terminal_write_when_enrichment_is_unavailable() -> None:
    writes = 0

    def persist(_decision: TerminalDecision) -> PersistedDecision:
        nonlocal writes
        writes += 1
        raise AssertionError("terminal persistence must not run")

    result = process_qualified_job(
        _listing(),
        Qualified(reason="qualified", profile_name="profile"),
        _decision_context(),
        DecisionStore(
            find_completed=lambda _key: None,
            existing_titles=lambda _company: (),
            active_company_policy=lambda _company, _at: None,
            persist=persist,
        ),
        lambda _listing: CriterionUnavailable(
            prompt_name="job-finder-enrichment",
            error_code="invalid_response",
            reason="missing tool call",
        ),
        lambda _title, _existing: PromptAccepted(
            prompt_name="job-finder-title-deduplication",
            output=TitleDuplicate(isDuplicate=False),
        ),
    )

    assert result.kind == "unavailable"
    assert result.stage == "enrichment"
    assert writes == 0


def test_normalizes_company_identity_like_the_legacy_ledger() -> None:
    assert normalize_ledger_text("  Iİ   Labs  ") == "ii̇ labs"


def _listing() -> JobListing:
    return JobListing(
        title="Sr Eng - Acme",
        company="acme.io",
        url="https://example.com/jobs/1",
        source="other",
        keywords_matched=("python",),
        date_posted=date(2026, 9, 9),
        date_scraped=date(2026, 9, 10),
        description="Raw description",
        location="",
    )


def _enriched() -> EnrichedJob:
    return EnrichedJob(
        title="Senior Engineer",
        company="Acme",
        description="## Overview\nBuild things.",
        location="Remote",
    )


def _decision_context() -> DecisionContext:
    return DecisionContext(
        pipeline_run_id=uuid4(),
        prompt_release_id=PromptReleaseId("r" * 64),
        policy_version="policy-1",
        implementation_ref="test-ref",
        observed_at=datetime(2026, 9, 10, 12, 0, tzinfo=UTC),
    )


def _model_context(input_digest: InputDigest) -> ModelCallContext:
    return ModelCallContext(
        processing_attempt_id=uuid4(),
        pipeline_run_id=uuid4(),
        prompt_release_id=build_prompt_release().id,
        operation_key="test",
        input_digest=input_digest,
    )


def _empty_model_persistence() -> ModelCallPersistence:
    return ModelCallPersistence(
        find_completed=lambda _request_id: None,
        next_attempt_number=lambda _request_id: 0,
        record=lambda _attempt: None,
    )


def _completion(tool_name: str, output: dict[str, object]) -> HttpResponse:
    return HttpResponse(
        status_code=200,
        body=json.dumps(
            {
                "id": "generation-1",
                "model": "google/gemini-2.5-flash-001",
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "type": "function",
                                    "function": {
                                        "name": tool_name,
                                        "arguments": json.dumps(output),
                                    },
                                }
                            ]
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 4,
                    "cost": 0.00012,
                },
            }
        ),
    )


def _unexpected_send(
    _url: str,
    _headers: Mapping[str, str],
    _body: dict[str, object],
    _timeout: float,
) -> HttpResponse:
    raise AssertionError("OpenRouter must not be called")
