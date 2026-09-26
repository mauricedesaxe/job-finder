from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import re
from uuid import UUID

import psycopg
import pytest
from starlette.testclient import TestClient

from job_finder.operations.spend import (
    AnalyticsService,
    SpendAnalytics,
)
from job_finder.web.app import create_review_app
from job_finder.pipeline.work_dismissals import (
    WorkDismissalApplied,
    WorkDismissalReceipt,
)
from job_finder.pipeline.work_recoveries import (
    WorkRecoveryCommand,
    WorkRecoveryResult,
)
from job_finder.operations.run_history import RunAttemptSummary, RunDetail, RunsService
from job_finder.operations.service import OperationsService
from job_finder.operations.work_history import (
    ModelCallDetail,
    JobVerdict,
    WorkItemNotFound,
    WorkItemDetail,
)

from job_finder.review.test_app_support import (
    helper_default_submit_review as _default_submit_review,
    helper_client as _client,
    helper_queue as _queue,
    helper_csrf as _csrf,
    helper_shell_link as _shell_link,
    helper_operations_snapshot as _operations_snapshot,
    helper_run_item as _run_item,
    helper_activity_run_entry as _activity_run_entry,
    helper_activity_service as _activity_service,
    helper_work_item_detail as _work_item_detail,
    helper_spend_analytics as _spend_analytics,
    helper_applied_recovery as _applied_recovery,
    NOW,
    SETTINGS,
    OWNER_ACCESS,
)


def test_the_run_detail_page_keeps_its_run_content() -> None:
    runs = RunsService(
        detail=lambda _run_id: RunDetail(
            item=_run_item(value=2, kind="discovery"),
            parameters={},
            unknown_cost_calls=1,
            keywords=(),
            attempts=(),
            models=(),
            decisions=(),
        ),
    )
    client = _client(_queue(), runs=runs)

    detail = client.get("/operations/runs/00000000-0000-0000-0000-000000000002")

    assert detail.status_code == 200
    assert "Discovery run" in detail.text
    assert "No jobs were discovered by this run." in detail.text
    assert "1 call returned no usage, so it has no recorded cost." in detail.text
    assert 'href="/operations/runs"' in detail.text


def test_the_activity_page_passes_filters_to_the_query() -> None:
    activity, captured = _activity_service()
    client = _client(_queue(), activity=activity)

    filtered = client.get(
        "/operations/runs?status=failed&status=retrying&kind=work&from=2026-09-01&to=2026-09-10"
    )

    assert filtered.status_code == 200
    query = captured[0]
    assert query.statuses == frozenset({"failed", "retrying"})
    assert query.kind == "work"
    assert query.from_at == datetime(2026, 9, 1, tzinfo=UTC)
    assert query.to_at == datetime(2026, 9, 10, tzinfo=UTC)
    assert query.show_no_ops is False
    assert "No activity matches these filters." in filtered.text

    showing = client.get("/operations/runs?show_noops=1")

    assert captured[1].show_no_ops is True
    assert 'name="show_noops" value="1" checked' in showing.text

    empty = client.get("/operations/runs")

    assert "No activity recorded yet." in empty.text


def test_unconfigured_operations_pages_name_the_missing_service(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _client(_queue())

    activity = client.get("/operations/runs")
    run = client.get(f"/operations/runs/{UUID(int=2)}")
    detail = client.get(f"/operations/work/{UUID(int=31)}")
    analytics = client.get("/operations/analytics")

    assert activity.status_code == 503
    assert "Recent activity is not configured for this deployment" in activity.text
    assert run.status_code == 503
    assert "Pipeline run details are not configured for this deployment" in run.text
    assert detail.status_code == 503
    assert "Work item details are not configured for this deployment" in detail.text
    assert analytics.status_code == 503
    assert "Model spend analytics are not configured for this deployment" in analytics.text
    assert "Recent activity load failed: OperationsUnavailable" in caplog.text
    assert "Pipeline run detail load failed: OperationsUnavailable" in caplog.text
    assert "Work item detail load failed: OperationsUnavailable" in caplog.text
    assert "Model spend analytics load failed: OperationsUnavailable" in caplog.text


def test_the_activity_page_falls_back_to_the_first_page_for_a_broken_cursor() -> None:
    activity, captured = _activity_service(_activity_run_entry(value=1))
    client = _client(_queue(), activity=activity)

    response = client.get("/operations/runs?cursor=broken-cursor")

    assert response.status_code == 200
    assert "Open run →" in response.text
    assert captured[0].cursor is None


def test_the_activity_page_keeps_filters_on_the_next_page_link() -> None:
    activity, _ = _activity_service(cursor="next-cursor-token")
    client = _client(_queue(), activity=activity)

    listing = client.get("/operations/runs?status=failed&kind=work&from=2026-09-01")

    assert listing.status_code == 200
    assert (
        'href="/operations/runs?status=failed&amp;kind=work&amp;from=2026-09-01&amp;cursor=next-cursor-token"'
        in listing.text
    )


def test_the_work_item_page_shows_failure_context_and_actions() -> None:
    operations = OperationsService(
        load=lambda: _operations_snapshot(),
        work_detail=lambda _job: _work_item_detail(),
    )
    client = _client(_queue(), operations=operations)

    response = client.get(f"/operations/work/{UUID(int=31)}")

    assert response.status_code == 200
    assert "<h1>Job</h1>" in response.text
    assert "No pipeline decision has been recorded yet." in response.text
    assert 'class="schedule-state terminal_error"' in response.text
    assert ">Needs attention<" in response.text
    assert "provider_timeout: OpenRouter did not respond" in response.text
    assert "Attempt count" in response.text
    assert "Dismissed" in response.text
    assert "Recover terminal work" in response.text
    assert "Dismiss" in response.text
    assert 'action="/operations/recovery"' in response.text
    assert 'action="/operations/dismiss"' in response.text
    assert "attempt 2" in response.text
    assert "2 model calls · $0.2500" in response.text
    assert re.search(
        _shell_link("/operations/runs", "Recent activity", current=True), response.text
    )


def test_the_work_item_page_shows_model_answers_and_collapsed_raw_payloads() -> None:
    call = ModelCallDetail(
        prompt_name="role-fit",
        prompt_version_id="a" * 64,
        requested_model="test-model",
        status="accepted",
        input_tokens=120,
        output_tokens=35,
        cost_usd=Decimal("0.125"),
        latency_ms=420,
        parsed_output={"decision": "reject", "reason": "Outside the role scope"},
        request_messages=[{"role": "user", "content": "Evaluate this role"}],
        raw_response={"answer": "Outside the role scope"},
        error_summary=None,
    )
    operations = OperationsService(
        load=lambda: _operations_snapshot(),
        work_detail=lambda _job: _work_item_detail(calls=(call,)),
    )
    client = _client(_queue(), operations=operations)

    response = client.get(f"/operations/work/{UUID(int=31)}")

    assert response.status_code == 200
    assert "role-fit" in response.text
    assert "Version aaaaaaaaaaaa" in response.text
    assert "test-model" in response.text
    assert "120 input tokens · 35 output tokens · $0.1250 · 420 ms" in response.text
    assert '"decision": "reject"' in response.text
    assert "<details>" in response.text
    assert "Full request and response" in response.text
    assert "Evaluate this role" in response.text
    assert "Outside the role scope" in response.text


def test_the_work_item_page_leads_with_the_recorded_pipeline_verdict() -> None:
    operations = OperationsService(
        load=lambda: _operations_snapshot(),
        work_detail=lambda _job: _work_item_detail(
            verdict=JobVerdict(
                outcome="qualified",
                reason="Matches the backend role and location requirements.",
                matched_profile="Backend engineer",
                decided_at=NOW - timedelta(hours=1),
            )
        ),
    )
    client = _client(_queue(), operations=operations)

    response = client.get(f"/operations/work/{UUID(int=31)}")

    assert response.status_code == 200
    assert response.text.index("Pipeline verdict") < response.text.index("Current state")
    assert "Qualified" in response.text
    assert "Matches the backend role and location requirements." in response.text
    assert "Matched profile: Backend engineer" in response.text
    assert "Decided" in response.text


def test_the_work_item_page_offers_retry_for_failed_work() -> None:
    operations = OperationsService(
        load=lambda: _operations_snapshot(),
        work_detail=lambda _job: _work_item_detail(
            state="failed",
            retry_at=NOW + timedelta(minutes=9),
        ),
    )
    client = _client(_queue(), operations=operations)

    response = client.get(f"/operations/work/{UUID(int=31)}")

    assert response.status_code == 200
    assert ">Retrying<" in response.text
    assert "Retry now" in response.text
    assert "Recover terminal work" not in response.text
    assert ">Dismiss</button>" not in response.text


def test_the_work_item_page_offers_undo_for_dismissed_work() -> None:
    operations = OperationsService(
        load=lambda: _operations_snapshot(),
        work_detail=lambda _job: _work_item_detail(
            dismissed=True,
            dismissed_at=NOW - timedelta(hours=1),
            dismissed_by="owner",
        ),
    )
    client = _client(_queue(), operations=operations)

    response = client.get(f"/operations/work/{UUID(int=31)}")

    assert response.status_code == 200
    assert 'class="schedule-state dismissed"' in response.text
    assert ">Dismissed<" in response.text
    assert "Undo dismissal" in response.text
    assert "Dismiss<" not in response.text


def test_the_work_item_page_hides_actions_for_healthy_work() -> None:
    operations = OperationsService(
        load=lambda: _operations_snapshot(),
        work_detail=lambda _job: _work_item_detail(state="completed"),
    )
    client = _client(_queue(), operations=operations)

    response = client.get(f"/operations/work/{UUID(int=31)}")

    assert response.status_code == 200
    assert "Take action" not in response.text


def test_an_unknown_work_item_renders_the_not_found_page() -> None:
    def missing_work(_job: UUID) -> WorkItemDetail:
        raise WorkItemNotFound("Work item does not exist")

    client = _client(
        _queue(),
        operations=OperationsService(load=lambda: _operations_snapshot(), work_detail=missing_work),
    )

    missing = client.get(f"/operations/work/{UUID(int=99)}")
    malformed = client.get("/operations/work/not-a-uuid")

    assert missing.status_code == 404
    assert "Work item was not found" in missing.text
    assert malformed.status_code == 404


def test_recovery_from_the_detail_page_returns_to_the_detail_page() -> None:
    calls: list[WorkRecoveryCommand] = []

    def recover(command: WorkRecoveryCommand) -> WorkRecoveryResult:
        calls.append(command)
        return _applied_recovery(command)

    client = _client(
        _queue(),
        operations=OperationsService(load=lambda: _operations_snapshot(), recover=recover),
    )

    response = client.post(
        "/operations/recovery",
        data={
            "csrf_token": _csrf(client),
            "job_id": str(UUID(int=31)),
            "action": "retry_now",
            "expected_state": "failed",
            "expected_attempt_count": "2",
            "idempotency_key": "private-key",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert (
        response.headers["location"]
        == "/operations/work/00000000-0000-0000-0000-00000000001f?notice=work-retried"
    )
    assert calls[0].job_id == UUID(int=31)


def test_dismissal_from_the_detail_page_returns_to_the_detail_page() -> None:
    client = _client(
        _queue(),
        operations=OperationsService(
            load=lambda: _operations_snapshot(),
            dismiss=lambda command: WorkDismissalApplied(
                receipt=WorkDismissalReceipt(
                    idempotency_key=command.idempotency_key,
                    job_id=command.job_id,
                    action=command.action,
                    expected_attempt_count=command.expected_attempt_count,
                    actor=command.actor,
                    requested_at=command.requested_at,
                    outcome="applied",
                    prior_state="terminal_error",
                    prior_attempt_count=3,
                    resulting_attempt_count=None,
                ),
                replayed=False,
            ),
        ),
    )

    response = client.post(
        "/operations/dismiss",
        data={
            "csrf_token": _csrf(client),
            "job_id": str(UUID(int=31)),
            "action": "dismiss",
            "expected_attempt_count": "3",
            "idempotency_key": "private-key",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert (
        response.headers["location"]
        == "/operations/work/00000000-0000-0000-0000-00000000001f?notice=work-dismissed"
    )


def test_unconfigured_dismissal_does_not_report_a_dagster_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _client(_queue(), operations=OperationsService(load=lambda: _operations_snapshot()))

    response = client.post(
        "/operations/dismiss",
        data={
            "csrf_token": _csrf(client),
            "job_id": str(UUID(int=31)),
            "action": "dismiss",
            "expected_attempt_count": "3",
            "idempotency_key": "private-key",
        },
    )

    assert response.status_code == 503
    assert "Work dismissal is not configured for this deployment" in response.text
    assert "Dagster control is unavailable" not in response.text
    assert "Work dismissal failed: OperationsUnavailable" in caplog.text
    assert "private-key" not in caplog.text


def test_the_run_detail_page_links_attempts_to_work_detail() -> None:
    runs = RunsService(
        detail=lambda _run_id: RunDetail(
            item=_run_item(value=2, kind="discovery"),
            parameters={},
            unknown_cost_calls=0,
            keywords=(),
            attempts=(
                RunAttemptSummary(
                    job_id=UUID(int=31),
                    operation_key="evaluation",
                    attempt_number=1,
                    status="failed",
                    error_summary="provider_timeout: OpenRouter did not respond",
                ),
                RunAttemptSummary(
                    job_id=None,
                    operation_key="orchestration",
                    attempt_number=0,
                    status="completed",
                    error_summary=None,
                ),
            ),
            models=(),
            decisions=(),
        ),
    )
    client = _client(_queue(), runs=runs)

    response = client.get("/operations/runs/00000000-0000-0000-0000-000000000002")

    assert response.status_code == 200
    assert 'href="/operations/work/00000000-0000-0000-0000-00000000001f"' in response.text
    assert "Inspect work →" in response.text
    assert response.text.count("Inspect work →") == 1


def test_an_unknown_run_id_renders_the_not_found_page() -> None:
    client = _client(_queue(), runs=RunsService())

    response = client.get("/operations/runs/not-a-uuid")

    assert response.status_code == 404
    assert "Run not found" in response.text


def test_the_analytics_page_answers_spend_by_day_model_and_run() -> None:
    client = _client(_queue(), analytics=AnalyticsService(load=lambda: _spend_analytics()))

    response = client.get("/operations/analytics")

    assert response.status_code == 200
    assert "$1.2345" in response.text
    assert "Model spend only" in response.text
    assert "Jina (search and scrape) and Langfuse costs are not tracked." in response.text
    assert "4 accepted calls" in response.text
    assert "2 calls returned no usage" in response.text
    assert "1,200" in response.text
    assert "900 in / 300 out" in response.text
    assert "5,200 ms" in response.text
    assert "z-ai/glm-4.6" in response.text
    assert "$1.1000" in response.text
    assert "row-head" in response.text
    assert "up to 5200 ms" in response.text
    assert re.search(_shell_link("/operations/analytics", "Analytics", current=True), response.text)
    assert 'href="/operations" class="back-link"' not in response.text


def test_the_analytics_page_charts_spend_per_day_with_readable_dates() -> None:
    client = _client(_queue(), analytics=AnalyticsService(load=lambda: _spend_analytics()))

    response = client.get("/operations/analytics")

    assert response.status_code == 200
    library_match = re.search(r'src="(/static/frappe-charts[^"]+)"', response.text)
    init_match = re.search(r'src="(/static/spend-chart-init[^"]+)"', response.text)
    latency_init_match = re.search(r'src="(/static/latency-chart-init[^"]+)"', response.text)
    assert library_match is not None
    assert init_match is not None
    assert latency_init_match is not None
    assert re.fullmatch(
        r"/static/frappe-charts\.min\.umd\.[0-9a-f]{10}\.js", library_match.group(1)
    )
    assert re.fullmatch(r"/static/spend-chart-init\.[0-9a-f]{10}\.js", init_match.group(1))
    assert re.fullmatch(
        r"/static/latency-chart-init\.[0-9a-f]{10}\.js", latency_init_match.group(1)
    )
    assert 'id="spend-per-day-chart"' in response.text
    assert "Sep 9" in response.text
    assert "Sep 10" in response.text
    assert "Sep 10, 2026 · 3 accepted calls · 1 returned no usage" in response.text
    assert (
        '{"name": "z-ai/glm-4.6", "values": [0.2345, 0.9], "costs": ["$0.2345", "$0.9000"]}'
        in response.text
    )
    assert (
        '{"name": "openai/gpt-5-mini", "values": [0.0, 0.1], "costs": ["$0.0000", "$0.1000"]}'
        in response.text
    )
    script_tags: list[str] = re.findall(r"<script[^>]*>", response.text)
    executable_inline_scripts = [
        tag for tag in script_tags if "src=" not in tag and "application/json" not in tag
    ]
    assert executable_inline_scripts == []


def test_the_analytics_page_charts_p90_latency_per_model_per_day() -> None:
    client = _client(_queue(), analytics=AnalyticsService(load=lambda: _spend_analytics()))

    response = client.get("/operations/analytics")

    assert response.status_code == 200
    assert 'id="latency-per-day-chart"' in response.text
    assert "Call latency" in response.text
    assert "9 in 10 calls were faster than the bar" in response.text
    assert (
        '{"name": "z-ai/glm-4.6", "values": [4100, 3000]}, '
        + '{"name": "openai/gpt-5-mini", "values": [null, 1200]}'
        in response.text
    )


def test_the_analytics_chart_assets_are_served_as_static_files() -> None:
    client = _client(_queue(), analytics=AnalyticsService(load=lambda: _spend_analytics()))

    page = client.get("/operations/analytics")
    library_match = re.search(r'src="(/static/frappe-charts[^"]+)"', page.text)
    init_match = re.search(r'src="(/static/spend-chart-init[^"]+)"', page.text)
    latency_init_match = re.search(r'src="(/static/latency-chart-init[^"]+)"', page.text)
    assert library_match is not None
    assert init_match is not None
    assert latency_init_match is not None

    library = client.get(library_match.group(1))
    init_script = client.get(init_match.group(1))
    latency_init_script = client.get(latency_init_match.group(1))
    stale_url = client.get("/static/spend-chart-init.js")
    missing = client.get("/static/nope.js")

    assert library.status_code == 200
    assert "javascript" in library.headers["content-type"]
    assert library.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert init_script.status_code == 200
    assert init_script.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert "frappe.Chart" in init_script.text
    assert "stacked: true" in init_script.text
    assert latency_init_script.status_code == 200
    assert latency_init_script.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert "frappe.Chart" in latency_init_script.text
    assert "stacked" not in latency_init_script.text
    assert stale_url.status_code == 404
    assert missing.status_code == 404


def test_static_assets_require_the_owner_session() -> None:
    app = create_review_app(
        lambda: _queue(),
        SETTINGS,
        submit_review=_default_submit_review,
        owner_access_service=OWNER_ACCESS,
        now=lambda: NOW,
    )

    response = TestClient(app).get("/static/nope.js", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Fstatic%2Fnope.js"


def test_the_analytics_page_renders_an_empty_state_without_calls() -> None:
    empty = SpendAnalytics(
        known_usd=Decimal(0),
        calls=0,
        accepted=0,
        errors=0,
        input_tokens=0,
        output_tokens=0,
        max_latency_ms=0,
        days=(),
        models=(),
    )
    client = _client(_queue(), analytics=AnalyticsService(load=lambda: empty))

    response = client.get("/operations/analytics")

    assert response.status_code == 200
    assert "$0.0000" in response.text
    assert "No model calls were recorded in the last 30 days." in response.text
    assert "No model calls were recorded." in response.text
    assert "latency-per-day-chart" not in response.text


def test_the_analytics_page_degrades_when_the_database_is_unreachable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def broken() -> SpendAnalytics:
        raise psycopg.Error("connection refused")

    client = _client(_queue(), analytics=AnalyticsService(load=broken))

    response = client.get("/operations/analytics")

    assert response.status_code == 503
    assert "Model spend analytics are unavailable" in response.text
    assert "Model spend analytics load failed: Error" in caplog.text


def test_the_operations_pages_link_to_each_other() -> None:
    client = _client(_queue(), activity=_activity_service()[0])

    runs = client.get("/operations/runs")

    assert 'href="/operations/analytics"' in runs.text
    assert 'href="/operations/control"' in runs.text
    assert 'href="/operations/failures"' not in runs.text
