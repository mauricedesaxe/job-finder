# pyright: reportMissingTypeStubs=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false
from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from fasthtml.common import (
    A,
    Aside,
    Body,
    Button,
    Div,
    Form,
    H1,
    Head,
    Html,
    Input,
    Main,
    Meta,
    Nav,
    P,
    Small,
    Span,
    Strong,
    Style,
    Title,
    to_xml,
)
from starlette.responses import HTMLResponse

ShellSection = Literal["review", "operations", "configuration"]
OperationsPage = Literal["activity", "analytics", "control"]

_SECTIONS: tuple[tuple[ShellSection, str, str], ...] = (
    ("review", "Review", "/"),
    ("operations", "Operations", "/operations/runs"),
    ("configuration", "Search setup", "/configuration"),
)

_OPERATIONS_PAGES: tuple[tuple[OperationsPage, str, str], ...] = (
    ("activity", "Recent activity", "/operations/runs"),
    ("analytics", "Analytics", "/operations/analytics"),
    ("control", "Control plane", "/operations/control"),
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


def operations_sidebar_page(current: OperationsPage, csrf_token: str, *content: object) -> object:
    return Div(
        sidebar(csrf_token, current="operations"),
        operations_sub_sidebar(current),
        Main(*content, cls="app-content"),
        cls="app-shell operations-shell",
    )


def operations_sub_sidebar(current: OperationsPage) -> object:
    return Aside(
        Small("Operations", cls="shell-label sub-label"),
        Nav(
            *(
                A(
                    label,
                    href=href,
                    cls="shell-link",
                    aria_current="page" if key is current else None,
                )
                for key, label, href in _OPERATIONS_PAGES
            ),
            aria_label="Operations",
            cls="shell-nav",
        ),
        cls="sub-sidebar",
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


def document(
    content: object,
    *,
    title: str = "Daily job review",
    scripts: tuple[object, ...] = (),
    refresh: tuple[int, str] | None = None,
) -> str:
    return str(
        to_xml(
            Html(
                Head(
                    Meta(charset="utf-8"),
                    Meta(name="viewport", content="width=device-width, initial-scale=1"),
                    Meta(name="color-scheme", content="light dark"),
                    Meta(http_equiv="refresh", content=f"{refresh[0]};url={refresh[1]}")
                    if refresh is not None
                    else None,
                    Title(title),
                    Style(_CSS),
                ),
                Body(content, *scripts),
                lang="en",
            )
        )
    )


def state_response(
    title: str,
    detail: str,
    *,
    action: object | None = None,
    status_code: int,
) -> HTMLResponse:
    content = Main(
        Small("Review queue", cls="eyebrow"),
        Div(H1(title), P(detail), action, cls="state"),
        cls="review-shell state-shell",
    )
    return HTMLResponse(document(content), status_code=status_code)


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


_CSS = (
    """
:root {
  --ink: #151515;
  --paper: #f3f0e7;
  --panel: #fffdf5;
  --panel-muted: #ebe7dc;
  --panel-subtle: #f1eee5;
  --surface-raised: #f7f4eb;
  --muted: #5d5b54;
  --line: #151515;
  --grid-line: rgb(21 21 21 / 0.06);
  --grid-line-strong: rgb(21 21 21 / 0.08);
  --inverse-bg: #151515;
  --inverse-text: #fffdf5;
  --shadow: #151515;
  --focus: #315cff;
  --acid: #dfff00;
  --caution: #ffd86b;
  --accent-ink: #151515;
  --reviewed: #dedbd1;
  font-family: Arial, Helvetica, ui-sans-serif, system-ui, sans-serif;
  color: var(--ink);
  background: var(--paper);
  color-scheme: light dark;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  min-width: 320px;
  min-height: 100vh;
  background-color: var(--paper);
  background-image: linear-gradient(var(--grid-line) 1px, transparent 1px), linear-gradient(90deg, var(--grid-line) 1px, transparent 1px);
  background-size: 24px 24px;
}
a { color: inherit; text-underline-offset: 0.2em; }
button, select, textarea, input { font: inherit; }
h1, h2 { margin: 0; font-family: Georgia, 'Times New Roman', serif; letter-spacing: -0.04em; }
h1 { max-width: 14ch; font-size: clamp(2.4rem, 7vw, 5.2rem); line-height: 0.9; }
h2 { font-size: clamp(1.55rem, 3vw, 2.35rem); line-height: 1; }
.eyebrow, .status-kicker, .shell-label, .why-label, .metadata-strip small {
  text-transform: uppercase;
  letter-spacing: 0.12em;
  font-size: 0.72rem;
  font-weight: 900;
}
.eyebrow { display: block; margin-bottom: 0.7rem; }
.review-shell { width: min(100% - 2rem, 1180px); margin: 0 auto; padding: 1.25rem 0 5rem; }
.reevaluation-form { margin-top: 1.25rem; }
.reevaluation-form > .decision { width: 100%; }
.app-shell { display: grid; grid-template-columns: 240px minmax(0, 1fr); min-height: 100vh; }
.app-content { min-width: 0; }
.sidebar { position: sticky; top: 0; display: flex; flex-direction: column; align-self: start; height: 100vh; border-right: 2px solid var(--line); background: var(--panel); }
.sidebar-head { display: flex; align-items: center; min-height: 56px; border-bottom: 2px solid var(--line); }
.wordmark { display: grid; place-items: center; align-self: stretch; min-width: 58px; padding: 0.6rem; background: var(--inverse-bg); color: var(--acid); font-size: 1.35rem; }
.shell-label { padding: 0 0.85rem; }
.shell-nav { display: grid; gap: 0.35rem; padding: 0.75rem; }
.shell-link { display: flex; align-items: center; min-height: 44px; padding: 0 0.75rem; border: 2px solid transparent; font-weight: 900; text-decoration: none; }
.shell-link:hover, .shell-link:focus-visible { border-color: var(--line); background: var(--acid); color: var(--accent-ink); }
.shell-link[aria-current="page"] { border-color: var(--line); background: var(--inverse-bg); color: var(--acid); }
.sidebar > form { margin-top: auto; padding: 0.75rem; }
.logout { width: 100%; min-height: 44px; border: 2px solid var(--line); background: var(--panel); color: var(--ink); cursor: pointer; font-weight: 900; }
.logout:hover, .logout:focus-visible { background: var(--acid); color: var(--accent-ink); }
.app-shell.operations-shell { grid-template-columns: 240px 200px minmax(0, 1fr); }
.sub-sidebar { position: sticky; top: 0; align-self: start; height: 100vh; border-right: 2px solid var(--line); background: var(--panel-muted); }
.sub-sidebar .sub-label { display: flex; align-items: center; min-height: 56px; border-bottom: 2px solid var(--line); }
.review-header { padding: clamp(2rem, 6vw, 5rem) 0 1.5rem; }
.review-header .eyebrow { width: fit-content; padding: 0.25rem 0.4rem; background: var(--acid); color: var(--accent-ink); }
.day-section { margin-top: 2.4rem; }
.day-summary { margin: 0.55rem 0 0; color: var(--muted); font-weight: 700; }
.job-list { list-style: none; padding: 0; margin: 0.8rem 0 0; border: 2px solid var(--line); border-bottom: 0; }
.job-list-item { border-bottom: 2px solid var(--line); }
.job-row, .reviewed-row { min-height: 92px; background: var(--panel); color: inherit; }
.job-row { display: block; padding: 0.9rem 1rem; text-decoration: none; }
.job-row:hover, .job-row:focus-visible { background: var(--acid); color: var(--accent-ink); }
.reviewed-row { display: grid; grid-template-columns: auto 1fr auto; align-items: center; gap: 1rem; padding-left: 1rem; background: var(--reviewed); }
.row-copy { padding: 0.8rem 0; }
.change-link { display: inline-flex; align-items: center; justify-content: center; align-self: stretch; min-width: 84px; min-height: 44px; border-left: 2px solid var(--line); font-weight: 900; }
.chip { display: inline-block; width: fit-content; padding: 0.22rem 0.45rem; border: 2px solid var(--line); font-size: 0.7rem; font-weight: 900; letter-spacing: 0.08em; text-transform: uppercase; }
.lane-new { background: var(--acid); color: var(--accent-ink); }
.lane-second-look { background: var(--caution); color: var(--accent-ink); }
.decision-chip { background: var(--inverse-bg); color: var(--inverse-text); }
.job-title { display: block; margin-top: 0.35rem; font-size: 1.08rem; }
.job-subline { display: block; margin-top: 0.18rem; color: var(--muted); font-size: 0.92rem; }
.item-topbar { display: grid; grid-template-columns: 1fr auto 1fr; align-items: center; gap: 1rem; margin-top: 0.75rem; padding: 0.65rem 0; border-bottom: 2px solid var(--line); }
.back-link { min-height: 44px; display: inline-flex; align-items: center; font-weight: 900; text-decoration: none; }
.position-marker { font-weight: 900; text-transform: uppercase; letter-spacing: 0.06em; }
.item-nav { display: flex; justify-content: end; gap: 0.5rem; }
.item-nav-link { min-width: 72px; min-height: 44px; display: inline-flex; align-items: center; justify-content: center; padding: 0 0.6rem; border: 2px solid var(--line); background: var(--panel); font-weight: 900; text-decoration: none; }
.item-nav-off { opacity: 0.45; border-style: dashed; }
.workbench { display: grid; grid-template-columns: minmax(0, 1.55fr) minmax(320px, 0.85fr); margin-top: 1.25rem; border: 2px solid var(--line); background: var(--panel); box-shadow: 8px 8px 0 var(--shadow); }
.evidence-panel, .decision-panel { min-width: 0; padding: clamp(1rem, 3vw, 2rem); }
.decision-panel { border-left: 2px solid var(--line); background: var(--panel-muted); }
.audit-card { box-shadow: 8px 8px 0 var(--caution); }
.card-topline { display: flex; justify-content: space-between; align-items: center; gap: 1rem; margin-bottom: 1.4rem; }
.status-kicker { padding: 0.25rem 0.4rem; background: var(--acid); color: var(--accent-ink); }
.audit-card .status-kicker { background: var(--caution); }
.job-meta { margin: 0.75rem 0 1.1rem; color: var(--muted); font-size: 1.05rem; }
.company { color: var(--ink); font-weight: 900; }
.metadata-strip { display: grid; grid-template-columns: repeat(3, 1fr); margin-bottom: 2rem; border: 2px solid var(--line); }
.metadata-strip > div { min-width: 0; padding: 0.6rem; border-right: 2px solid var(--line); }
.metadata-strip > div:last-child { border-right: 0; }
.metadata-strip small, .metadata-strip span { display: block; }
.metadata-strip span { margin-top: 0.3rem; overflow-wrap: anywhere; font-size: 0.88rem; }
.why-label { display: block; margin-bottom: 0.4rem; color: var(--muted); }
.evaluation-reason { margin: 1.25rem 0; padding: 0.85rem 1rem; border-left: 6px solid var(--acid); background: var(--panel-subtle); font-weight: 750; }
.compensation-card { margin: 1.25rem 0; padding: 0.8rem 1rem; border: 2px solid var(--line); background: var(--acid); color: var(--accent-ink); }
.compensation-card .why-label { color: var(--accent-ink); }
.compensation-value { margin: 0.25rem 0 0; font-size: 1.1rem; font-weight: 900; }
.description-block { margin-top: 1.5rem; }
.job-description { max-height: 52vh; overflow: auto; white-space: pre-wrap; margin: 0; padding: 1rem; border: 2px solid var(--line); background: var(--surface-raised); color: var(--ink); font: 1rem/1.65 Arial, Helvetica, ui-sans-serif, system-ui, sans-serif; }
.decision-panel h2 { margin-bottom: 1.5rem; }
.note-field { display: grid; gap: 0.45rem; margin: 0 0 1rem; font-weight: 800; }
.note-field textarea { width: 100%; min-height: 112px; padding: 0.75rem; border: 2px solid var(--line); border-radius: 0; background: var(--panel); color: var(--ink); }
.block-company { display: flex; align-items: center; min-height: 56px; margin: 1rem 0; padding: 0.6rem; border: 2px solid var(--line); background: var(--caution); color: var(--accent-ink); font-weight: 900; }
.block-company input { width: 22px; height: 22px; margin-right: 0.65rem; accent-color: var(--accent-ink); }
.decision-row { display: grid; grid-template-columns: 1fr; gap: 0.65rem; padding: 0; border: 0; }
.decision-row legend { margin-bottom: 0.75rem; font-weight: 900; }
.decision { min-height: 52px; border: 2px solid var(--line); background: var(--panel); color: var(--ink); cursor: pointer; font-weight: 900; }
.decision:hover, .decision:focus-visible { background: var(--acid); color: var(--accent-ink); box-shadow: 4px 4px 0 var(--shadow); }
.decision[aria-pressed="true"] { background: var(--inverse-bg); color: var(--acid); box-shadow: 4px 4px 0 var(--acid); }
.decision.reject[aria-pressed="true"] { color: var(--caution); }
.decision:focus-visible, a:focus-visible, textarea:focus-visible, input:focus-visible { outline: 3px solid var(--focus); outline-offset: 3px; }
.state-shell { min-height: 100vh; display: grid; align-content: center; }
.state { margin-top: 1.25rem; padding: clamp(1.5rem, 5vw, 3rem); border: 2px solid var(--line); background-color: var(--panel); background-image: linear-gradient(var(--grid-line-strong) 1px, transparent 1px), linear-gradient(90deg, var(--grid-line-strong) 1px, transparent 1px); background-size: 20px 20px; box-shadow: 8px 8px 0 var(--shadow); }
.state h1, .state h2 { max-width: 14ch; }
.state p { max-width: 48ch; color: var(--muted); font-size: 1.08rem; line-height: 1.6; }
.retry { display: inline-flex; min-height: 48px; align-items: center; margin-top: 0.5rem; padding: 0 1.1rem; border: 2px solid var(--line); background: var(--acid); color: var(--accent-ink); font-weight: 900; }
.login-shell { display: grid; grid-template-columns: minmax(0, 1.25fr) minmax(320px, 0.75fr); min-height: 100vh; }
.login-editorial { display: grid; align-content: center; padding: clamp(2rem, 7vw, 7rem); background: var(--inverse-bg); color: var(--inverse-text); }
.login-editorial h1 { color: var(--acid); }
.route-line { display: flex; flex-wrap: wrap; gap: 0.55rem; margin: 1.8rem 0 0; font-weight: 900; text-transform: uppercase; letter-spacing: 0.06em; }
.route-line .route-divider { color: var(--acid); }
.login-intro { max-width: 40ch; line-height: 1.5; }
.login-card { align-self: center; width: min(100% - 2rem, 430px); margin: 2rem auto; padding: 2rem; border: 2px solid var(--line); background: var(--panel); box-shadow: 8px 8px 0 var(--acid); }
.login-card h2 { margin-bottom: 1.5rem; }
.login-card label { display: grid; gap: 0.5rem; font-weight: 800; }
.login-card input { width: 100%; min-height: 48px; padding: 0.75rem; border: 2px solid var(--line); border-radius: 0; background: var(--surface-raised); color: var(--ink); }
.login-card button { width: 100%; min-height: 48px; margin-top: 1rem; border: 2px solid var(--line); background: var(--acid); color: var(--accent-ink); cursor: pointer; font-weight: 900; }
.error { padding: 0.75rem; border: 2px solid var(--line); background: var(--caution); color: var(--accent-ink); font-weight: 800; }
.operations-header { margin-top: 1.25rem; padding: clamp(1.5rem, 5vw, 3rem); border: 2px solid var(--line); background: var(--panel); box-shadow: 8px 8px 0 var(--shadow); }
.operations-notice { margin: 1.25rem 0 0; padding: 0.8rem 1rem; border: 2px solid var(--line); background: var(--acid); color: var(--accent-ink); font-weight: 900; }
.operations-alert { margin: 1rem 0 0; padding: 0.8rem 1rem; border: 2px solid var(--line); background: var(--caution); color: var(--accent-ink); }
.operations-alert p { margin: 0.4rem 0 0; line-height: 1.5; }
.pipeline-steps { list-style: none; counter-reset: pipeline-step; margin: 1rem 0 0; padding: 0; border: 2px solid var(--line); border-bottom: 0; }
.pipeline-steps > li { counter-increment: pipeline-step; padding: 0.8rem; border-bottom: 2px solid var(--line); background: var(--surface-raised); }
.pipeline-steps > li::before { content: counter(pipeline-step); display: inline-block; margin-right: 0.5rem; padding: 0 0.45rem; border: 2px solid var(--line); background: var(--acid); color: var(--accent-ink); font-weight: 900; }
.pipeline-steps p { margin: 0.35rem 0 0; color: var(--muted); line-height: 1.5; }
.operations-intro { max-width: 54ch; margin: 1rem 0 0; font-size: 1.08rem; line-height: 1.55; }
.operations-metrics { display: grid; grid-template-columns: repeat(5, 1fr); margin-top: 2rem; border: 2px solid var(--line); background: var(--panel); }
.operations-metrics > div { min-width: 0; padding: 0.8rem; border-right: 2px solid var(--line); }
.operations-metrics > div:last-child { border-right: 0; }
.operations-metrics small, .operations-metrics strong, .operations-metrics span { display: block; }
.operations-metrics small { text-transform: uppercase; letter-spacing: 0.08em; font-weight: 900; }
.operations-metrics strong { margin-top: 0.25rem; font: 700 2rem Georgia, 'Times New Roman', serif; }
.operations-metrics span { margin-top: 0.15rem; color: var(--muted); font-size: 0.8rem; }
.operations-section { padding: 1.25rem; border: 2px solid var(--line); background: var(--panel); }
.spend-chart { margin-top: 1.25rem; }
.spend-row-head { display: flex; align-items: baseline; justify-content: space-between; gap: 1rem; }
.operations-muted, .operations-empty { color: var(--muted); line-height: 1.5; }
.operations-section { margin-top: 1.25rem; }
.operations-list { list-style: none; margin: 1rem 0 0; padding: 0; border: 2px solid var(--line); border-bottom: 0; }
.operations-list li { padding: 0.8rem; border-bottom: 2px solid var(--line); background: var(--surface-raised); }
.row-head { display: flex; align-items: baseline; justify-content: space-between; gap: 1rem; }
.activity-filters { margin-top: 1.25rem; padding: 1rem; border: 2px solid var(--line); background: var(--panel); }
.filter-checks { display: flex; flex-wrap: wrap; gap: 0.4rem 1rem; }
.filter-check { display: inline-flex; align-items: center; gap: 0.4rem; font-weight: 700; }
.filter-check input { width: 18px; height: 18px; accent-color: var(--accent-ink); }
.filter-controls { display: flex; flex-wrap: wrap; align-items: end; gap: 0.75rem; margin-top: 0.85rem; }
.filter-kind, .filter-date { display: grid; gap: 0.25rem; font-size: 0.8rem; font-weight: 900; text-transform: uppercase; letter-spacing: 0.08em; }
.filter-kind select, .filter-date input { min-height: 42px; padding: 0 0.5rem; border: 2px solid var(--line); background: var(--panel); color: var(--ink); font: inherit; }
.filter-controls .operation-button { width: auto; min-width: 132px; }
.filter-clear { display: inline-flex; align-items: center; min-height: 42px; padding: 0 0.6rem; font-weight: 900; }
.activity-next { margin-top: 1.25rem; }
.work-status-row { display: grid; gap: 0.3rem; }
.work-actions { grid-template-columns: 1fr 1fr; max-width: 560px; }
@media (max-width: 760px) {
  .work-actions { grid-template-columns: 1fr; }
}
.operations-list li > small { display: block; margin-top: 0.4rem; color: var(--muted); }
.run-status { text-transform: uppercase; letter-spacing: 0.08em; font-size: 0.72rem; font-weight: 900; }
.run-link { display: block; text-decoration: none; }
.run-link:hover, .run-link:focus-visible { background: var(--acid); color: var(--accent-ink); }
.run-row { display: grid; gap: 0.2rem; }
.run-link-hint { font-weight: 900; font-size: 0.8rem; }
.discovery-row > div { display: flex; justify-content: space-between; gap: 1rem; }
.operations-list .operations-muted p { margin-bottom: 0; overflow-wrap: anywhere; }
.schedule-list { list-style: none; margin: 1rem 0 0; padding: 0; border: 2px solid var(--line); border-bottom: 0; }
.schedule-list > li { padding: 0.8rem; border-bottom: 2px solid var(--line); background: var(--surface-raised); }
.schedule-list > li > div > div:first-child { display: flex; align-items: center; justify-content: space-between; gap: 0.75rem; }
.schedule-state { padding: 0.18rem 0.35rem; border: 2px solid var(--line); font-size: 0.68rem; font-weight: 900; letter-spacing: 0.08em; text-transform: uppercase; }
.schedule-state.running { background: var(--acid); color: var(--accent-ink); }
.schedule-state.stopped, .schedule-state.unavailable { background: var(--caution); color: var(--accent-ink); }
.schedule-cadence, .schedule-next { margin: 0.45rem 0 0; color: var(--muted); font-size: 0.86rem; }
.schedule-actions { display: grid !important; grid-template-columns: 1fr 1fr; gap: 0.5rem; margin-top: 0.75rem; }
.operation-button { width: 100%; min-height: 42px; padding: 0 0.6rem; border: 2px solid var(--line); background: var(--acid); color: var(--accent-ink); cursor: pointer; font-weight: 900; }
.operation-button.secondary { background: var(--panel); color: var(--ink); }
.operation-button:disabled { cursor: not-allowed; opacity: 0.5; }
.recovery-id { display: block; margin-top: 0.35rem; overflow-wrap: anywhere; color: var(--muted); }
@media (prefers-color-scheme: dark) {
  :root {
    --ink: #f3f0e7;
    --paper: #11120f;
    --panel: #1b1c18;
    --panel-muted: #24251f;
    --panel-subtle: #272821;
    --surface-raised: #171814;
    --muted: #b9b7ae;
    --line: #e7e2d5;
    --grid-line: rgb(243 240 231 / 0.07);
    --grid-line-strong: rgb(243 240 231 / 0.1);
    --inverse-bg: #050604;
    --inverse-text: #f3f0e7;
    --shadow: #050604;
    --focus: #8ca9ff;
    --reviewed: #292a25;
  }
}
@media (max-width: 760px) {
  .review-shell { width: min(100% - 1rem, 1180px); padding-top: 0.5rem; }
  .app-shell { grid-template-columns: 1fr; }
  .app-shell.operations-shell { grid-template-columns: 1fr; }
  .sidebar { position: static; height: auto; flex-direction: row; align-items: center; gap: 0.5rem; border-right: 0; border-bottom: 2px solid var(--line); }
  .sub-sidebar { position: static; height: auto; border-right: 0; border-bottom: 2px solid var(--line); }
  .sub-sidebar .sub-label { display: none; }
  .sub-sidebar .shell-nav { display: flex; overflow-x: auto; }
  .sub-sidebar .shell-link { white-space: nowrap; }
  .sidebar-head { border-bottom: 0; }
  .shell-label { display: none; }
  .shell-nav { display: flex; flex: 1; gap: 0.4rem; padding: 0.5rem; overflow-x: auto; }
  .shell-link { white-space: nowrap; }
  .sidebar > form { margin: 0 0.5rem 0 0; padding: 0; }
  .logout { width: auto; min-width: 84px; }
  .login-shell { grid-template-columns: 1fr; }
  .login-editorial { min-height: 48vh; padding: 2rem 1rem; }
  .workbench { grid-template-columns: 1fr; box-shadow: 5px 5px 0 var(--shadow); }
  .decision-panel { border-top: 2px solid var(--line); border-left: 0; }
  .item-topbar { grid-template-columns: 1fr auto; }
  .position-marker { grid-column: 1 / -1; grid-row: 1; }
  .back-link, .item-nav { grid-row: 2; }
  .metadata-strip { grid-template-columns: 1fr; }
  .metadata-strip > div { border-right: 0; border-bottom: 2px solid var(--line); }
  .metadata-strip > div:last-child { border-bottom: 0; }
  .reviewed-row { grid-template-columns: 1fr auto; padding-left: 0.75rem; }
  .reviewed-row .decision-chip { grid-column: 1; margin-top: 0.7rem; }
  .row-copy { grid-column: 1; }
  .change-link { grid-column: 2; grid-row: 1 / 3; }
  .card-topline { align-items: flex-start; flex-direction: column; }
  .job-description { max-height: none; overflow: visible; }
  .operations-metrics { grid-template-columns: 1fr; }
  .operations-metrics > div { border-right: 0; border-bottom: 2px solid var(--line); }
  .operations-metrics > div:last-child { border-bottom: 0; }
  .schedule-actions { grid-template-columns: 1fr; }
}
@media (max-width: 360px) {
  .evidence-panel, .decision-panel, .login-card { padding: 1rem; }
  .item-nav-link { min-width: 64px; padding: 0 0.35rem; }
}
@media (prefers-reduced-motion: reduce) {
  .job-row:hover, .job-row:focus-visible, .decision:hover, .decision:focus-visible { transform: none; }
}
"""
    + """
.configuration-header { padding: clamp(2rem, 6vw, 5rem) 0 1.5rem; }
.configuration-header h1 { max-width: 17ch; }
.configuration-intro { max-width: 64ch; font-size: 1.08rem; line-height: 1.6; }
.configuration-layout { display: grid; grid-template-columns: 210px minmax(0, 1fr); gap: 1.5rem; align-items: start; }
.section-index { position: sticky; top: 1rem; display: grid; border: 2px solid var(--line); background: var(--panel); }
.section-index strong, .section-index a { min-height: 44px; display: flex; align-items: center; padding: 0.6rem 0.75rem; border-bottom: 2px solid var(--line); }
.section-index a:last-child { border-bottom: 0; }
.section-index a:hover, .section-index a:focus-visible { background: var(--acid); color: var(--accent-ink); }
.configuration-state, .preview-counts { display: grid; grid-template-columns: repeat(3, 1fr); margin-bottom: 1.5rem; border: 2px solid var(--line); background: var(--panel); }
.configuration-state > div, .preview-counts > div { min-width: 0; padding: 0.75rem; border-right: 2px solid var(--line); border-bottom: 2px solid var(--line); }
.configuration-state > div:nth-child(3n), .preview-counts > div:last-child { border-right: 0; }
.configuration-state > div:nth-last-child(-n + 3), .preview-counts > div { border-bottom: 0; }
.configuration-state small, .configuration-state strong, .configuration-state span, .preview-counts small, .preview-counts strong, .preview-counts span { display: block; }
.configuration-state small, .preview-counts small { margin-bottom: 0.35rem; text-transform: uppercase; letter-spacing: 0.08em; font-weight: 900; }
.mono { overflow-wrap: anywhere; font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 0.76rem; }
.configuration-form { display: grid; gap: 1.5rem; }
.editor-section { min-width: 0; margin: 0; padding: clamp(1rem, 3vw, 1.6rem); border: 2px solid var(--line); background: var(--panel); box-shadow: 6px 6px 0 var(--shadow); }
.editor-section legend { padding: 0 0.45rem; font: 700 1.55rem Georgia, 'Times New Roman', serif; }
.field-help { margin-top: 0; color: var(--muted); }
.keyword-list-field, .named-card-body label { display: grid; gap: 0.35rem; font-weight: 800; }
.keyword-list-field textarea, .named-card input, .named-card textarea { width: 100%; min-height: 48px; padding: 0.7rem; border: 2px solid var(--line); border-radius: 0; background: var(--surface-raised); color: var(--ink); }
.keyword-list-field textarea, .named-card textarea { line-height: 1.5; resize: vertical; }
.row-actions { display: flex; flex-wrap: wrap; gap: 0.4rem; }
.row-actions button { min-width: 48px; min-height: 48px; border: 2px solid var(--line); background: var(--panel-muted); color: var(--ink); cursor: pointer; font-weight: 900; }
.row-actions button:disabled { opacity: 0.35; cursor: not-allowed; }
.row-actions .remove { background: var(--caution); color: var(--accent-ink); }
.source-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 0.65rem; }
.source-choice { min-height: 52px; display: flex; align-items: center; padding: 0.65rem; border: 2px solid var(--line); background: var(--surface-raised); font-weight: 900; }
.source-choice input { width: 22px; height: 22px; margin-right: 0.65rem; accent-color: var(--accent-ink); }
.invalid-sources { margin-top: 0.75rem; padding: 0.75rem; border: 2px solid var(--line); background: var(--caution); color: var(--accent-ink); }
.invalid-source-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 0.75rem; align-items: end; margin-top: 0.75rem; }
.invalid-source-row label { display: grid; gap: 0.35rem; font-weight: 800; }
.invalid-source-row input { width: 100%; min-height: 48px; padding: 0.7rem; border: 2px solid var(--line); border-radius: 0; }
.invalid-source-row button { min-height: 48px; border: 2px solid var(--line); background: var(--panel); font-weight: 900; }
.named-card { margin-top: 0.75rem; border: 2px solid var(--line); background: var(--surface-raised); }
.named-card summary { min-height: 58px; display: grid; grid-template-columns: auto minmax(0, 1fr) auto; gap: 0.75rem; align-items: center; padding: 0.65rem; cursor: pointer; }
.row-number { display: grid; place-items: center; width: 36px; height: 36px; background: var(--inverse-bg); color: var(--acid); font-weight: 900; }
.row-key { overflow-wrap: anywhere; color: var(--muted); font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 0.78rem; }
.named-card-body { display: grid; grid-template-columns: 0.7fr 1.3fr; gap: 1rem; padding: 1rem; border-top: 2px solid var(--line); }
.instructions-field, .named-card-body .row-actions { grid-column: 1 / -1; }
.affected { box-shadow: inset 6px 0 0 var(--caution); }
.button { min-height: 48px; padding: 0.65rem 1rem; border: 2px solid var(--line); color: var(--ink); cursor: pointer; font-weight: 900; }
.button.primary { background: var(--acid); color: var(--accent-ink); }
.button.secondary, .button.add { background: var(--panel); }
.button.add { margin-top: 0.9rem; }
.editor-actions { display: flex; justify-content: end; gap: 0.75rem; }
.validation-alert, .configuration-alert, .notice { margin: 0 0 1.5rem; padding: 1rem; border: 2px solid var(--line); background: var(--caution); color: var(--accent-ink); font-weight: 800; }
.notice { background: var(--acid); }
.field-error { color: #b22121; font-weight: 800; }
.configuration-preview, .release-panel { margin-top: 2rem; padding: clamp(1rem, 3vw, 1.6rem); border: 2px solid var(--line); background: var(--panel); box-shadow: 6px 6px 0 var(--shadow); }
.sample-list { padding-left: 1.3rem; line-height: 1.7; }
.prompt-summary { list-style: none; padding: 0; border: 2px solid var(--line); }
.prompt-summary li { display: flex; justify-content: space-between; gap: 1rem; padding: 0.7rem; border-bottom: 2px solid var(--line); }
.prompt-summary li:last-child { border-bottom: 0; }
.advanced-preview { margin-top: 1rem; border: 2px solid var(--line); }
.advanced-preview > summary { min-height: 48px; padding: 0.75rem; cursor: pointer; font-weight: 900; }
.compiled-prompt { padding: 1rem; border-top: 2px solid var(--line); }
.compiled-prompt pre { max-height: 360px; overflow: auto; white-space: pre-wrap; padding: 0.75rem; background: var(--surface-raised); color: var(--ink); }
.published-state, .active-state { padding: 0.8rem; border-left: 6px solid var(--acid); background: var(--panel-subtle); font-weight: 900; }
.masthead-nav { display: flex; align-self: stretch; margin-left: auto; }
.masthead-nav a { min-height: 52px; display: flex; align-items: center; padding: 0 0.8rem; border-left: 2px solid var(--line); text-decoration: none; font-weight: 900; }
.masthead-nav a[aria-current="page"] { background: var(--inverse-bg); color: var(--acid); }
@media (max-width: 760px) {
  .configuration-layout { grid-template-columns: 1fr; }
  .section-index { position: static; grid-template-columns: repeat(2, 1fr); }
  .section-index strong { grid-column: 1 / -1; }
  .section-index a { border-right: 2px solid var(--line); }
  .configuration-state { grid-template-columns: 1fr; }
  .configuration-state > div, .configuration-state > div:nth-child(3n), .configuration-state > div:nth-last-child(-n + 3) { border-right: 0; border-bottom: 2px solid var(--line); }
  .configuration-state > div:last-child { border-bottom: 0; }
  .named-card-body { grid-template-columns: 1fr; }
  .invalid-source-row { grid-template-columns: 1fr; }
  .instructions-field, .named-card-body .row-actions { grid-column: 1; }
  .named-card summary { grid-template-columns: auto minmax(0, 1fr); }
  .row-key { grid-column: 2; }
  .editor-actions { flex-direction: column; }
  .masthead { flex-wrap: wrap; }
  .masthead-nav { order: 3; width: 100%; border-top: 2px solid var(--line); }
  .masthead-nav a { flex: 1; justify-content: center; }
}
@media (max-width: 420px) {
  .source-grid, .preview-counts { grid-template-columns: 1fr; }
  .preview-counts > div { border-right: 0; border-bottom: 2px solid var(--line); }
  .preview-counts > div:last-child { border-bottom: 0; }
  .row-actions button { flex: 1; }
}
"""
)
