# pyright: reportMissingTypeStubs=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false
from __future__ import annotations

from datetime import UTC, datetime

from fasthtml.common import to_xml

from job_finder.web.shell import (
    absolute_time,
    operations_sub_sidebar,
    relative_time,
    sidebar,
    timestamp,
)

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


def _relative(seconds: float) -> str:
    return relative_time(NOW, now=datetime.fromtimestamp(NOW.timestamp() + seconds, tz=UTC))


def test_relative_time_reports_just_now_under_a_minute() -> None:
    assert _relative(0) == "just now"
    assert _relative(59) == "just now"


def test_relative_time_reports_minutes() -> None:
    assert _relative(60) == "1 minute ago"
    assert _relative(90) == "1 minute ago"
    assert _relative(120) == "2 minutes ago"
    assert _relative(3540) == "59 minutes ago"


def test_relative_time_reports_hours_and_days() -> None:
    assert _relative(3600) == "1 hour ago"
    assert _relative(7200) == "2 hours ago"
    assert _relative(86400) == "1 day ago"
    assert _relative(7 * 86400) == "7 days ago"


def test_relative_time_reports_months_and_years() -> None:
    assert _relative(30 * 86400) == "1 month ago"
    assert _relative(5 * 30 * 86400) == "5 months ago"
    assert _relative(365 * 86400) == "1 year ago"
    assert _relative(2 * 365 * 86400) == "2 years ago"


def test_relative_time_reports_future_timestamps() -> None:
    assert _relative(-90) == "in 1 minute"
    assert _relative(-7200) == "in 2 hours"
    assert _relative(-2 * 86400) == "in 2 days"


def test_absolute_time_renders_utc() -> None:
    observed = datetime(2026, 9, 23, 18, 4, tzinfo=UTC)

    assert absolute_time(observed) == "2026-09-23 18:04 UTC"


def test_timestamp_carries_the_absolute_time_as_its_title() -> None:
    observed = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)

    markup = to_xml(timestamp(observed, now=NOW))

    assert "1 day ago" in markup
    assert 'title="2026-09-09 12:00 UTC"' in markup


def test_sidebar_marks_the_current_section() -> None:
    markup = to_xml(sidebar("token", current="operations"))

    assert 'href="/"' in markup
    assert 'href="/operations/runs" aria-current="page"' in markup
    assert 'href="/configuration"' in markup
    assert 'action="/logout"' in markup


def test_operations_sub_sidebar_marks_the_current_page() -> None:
    markup = to_xml(operations_sub_sidebar("control"))

    assert 'href="/operations/runs"' in markup
    assert 'href="/operations/analytics"' in markup
    assert 'href="/operations/control" aria-current="page"' in markup
    assert "/operations/failures" not in markup
    assert 'aria-label="Operations"' in markup
