# pyright: reportMissingTypeStubs=false, reportUnknownVariableType=false, reportUnknownMemberType=false
from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from fasthtml.common import A, Aside, Button, Div, Form, Input, Main, Nav, Small, Span, Strong

ShellSection = Literal["review", "operations", "configuration"]

_SECTIONS: tuple[tuple[ShellSection, str, str], ...] = (
    ("review", "Review", "/"),
    ("operations", "Operations", "/operations"),
    ("configuration", "Search setup", "/configuration"),
)

_MINUTE = 60
_HOUR = 60 * _MINUTE
_DAY = 24 * _HOUR
_MONTH = 30 * _DAY
_YEAR = 365 * _DAY


def sidebar_page(current: ShellSection, csrf_token: str, *content: object) -> object:
    return Div(
        sidebar(csrf_token, current=current),
        Main(*content, cls="app-content"),
        cls="app-shell",
    )


def sidebar(csrf_token: str, *, current: ShellSection) -> object:
    return Aside(
        Div(
            Strong("JF", cls="wordmark"),
            Small("Owner workbench", cls="shell-label"),
            cls="sidebar-head",
        ),
        Nav(
            *(
                A(
                    label,
                    href=href,
                    cls="shell-link",
                    aria_current="page" if key is current else None,
                )
                for key, label, href in _SECTIONS
            ),
            aria_label="Owner workbench",
            cls="shell-nav",
        ),
        Form(
            Input(type="hidden", name="csrf_token", value=csrf_token),
            Button("Sign out", type="submit", cls="logout"),
            action="/logout",
            method="post",
        ),
        cls="sidebar",
    )


def relative_time(observed: datetime, *, now: datetime) -> str:
    seconds = (now - observed).total_seconds()
    if 0 <= seconds < _MINUTE:
        return "just now"
    magnitude = abs(seconds)
    count, unit = _largest_unit(magnitude)
    plural = "" if count == 1 else "s"
    if seconds < 0:
        return f"in {count} {unit}{plural}"
    return f"{count} {unit}{plural} ago"


def absolute_time(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def timestamp(value: datetime, *, now: datetime) -> object:
    return Span(relative_time(value, now=now), title=absolute_time(value))


def _largest_unit(seconds: float) -> tuple[int, str]:
    if seconds < _HOUR:
        return max(1, int(seconds // _MINUTE)), "minute"
    if seconds < _DAY:
        return max(1, int(seconds // _HOUR)), "hour"
    if seconds < _MONTH:
        return max(1, int(seconds // _DAY)), "day"
    if seconds < _YEAR:
        return max(1, int(seconds // _MONTH)), "month"
    return max(1, int(seconds // _YEAR)), "year"
