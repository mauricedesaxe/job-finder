"""Member administration and one-use account links."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

import psycopg
from fasthtml.common import (
    A,
    Button,
    Div,
    FastHTML,
    Fieldset,
    Form,
    H1,
    H2,
    Input,
    Label,
    Legend,
    Option,
    P,
    Request,
    Script,
    Select,
    Small,
)
from starlette.responses import HTMLResponse, RedirectResponse

from job_finder.access_policy import Capability, Preset, has_capability, preset_grants
from job_finder.review.accounts import Account, AccountRole, AccountService, AccountStatus
from job_finder.web.assets import static_url
from job_finder.web.principal import current_account
from job_finder.web.security import ensure_csrf_token, form_text, valid_csrf
from job_finder.web.shell import document, sidebar_page, state_response


def register_member_routes(app: FastHTML, *, accounts: AccountService) -> None:
    _register_account_link_routes(app, accounts)

    @app.route("/members", methods=["GET"], name="member_index")
    def member_index(request: Request) -> HTMLResponse:
        actor = current_account(request)
        can_manage = has_capability(actor.capabilities, Capability.ADMIN_MEMBERS)
        token = ensure_csrf_token(request)
        try:
            members = accounts.list_members()
        except psycopg.Error:
            return state_response("Members unavailable", "Reload and try again.", status_code=503)
        content = Div(
            Small("Administration", cls="eyebrow"),
            H1("Members"),
            P("Each person has their own account and access."),
            A("Invite a member", href="/members/invite") if can_manage else None,
            *(_member_card(member, actor, token, can_manage) for member in members),
            cls="review-shell member-page",
        )
        return HTMLResponse(
            document(sidebar_page("members", token, content, grants=actor.capabilities))
        )

    @app.route("/members/invite", methods=["GET"], name="member_invite_form")
    def invite_form(request: Request) -> HTMLResponse:
        actor = current_account(request)
        token = ensure_csrf_token(request)
        preset = _preset(request.query_params.get("preset", "Reviewer"))
        return _invite_response(actor, token, preset)

    @app.route("/members/invite", methods=["POST"], name="member_invite_submit")
    async def invite_submit(request: Request) -> HTMLResponse:
        form = await request.form()
        if not valid_csrf(request, form_text(form, "csrf_token")):
            return state_response("Invite failed", "Reload and try again.", status_code=403)
        actor = current_account(request)
        preset = _preset(form_text(form, "preset"))
        try:
            role = AccountRole.ADMIN if preset is Preset.ADMIN else AccountRole.MEMBER
            grants = (
                frozenset() if role is AccountRole.ADMIN else _form_grants(form.getlist("grants"))
            )
            link_token = accounts.issue_invite(
                actor.id, form_text(form, "email"), role=role, grants=grants
            )
        except (ValueError, PermissionError) as error:
            return _invite_response(actor, ensure_csrf_token(request), preset, str(error), 400)
        except psycopg.Error:
            return state_response("Invite unavailable", "Reload and try again.", status_code=503)
        url = str(request.url_for("member_accept_invite_form", token=link_token))
        return _link_response(request, "Invitation ready", url, "This link expires in 7 days.")

    @app.route("/members/{member_id}", methods=["POST"], name="member_update")
    async def member_update(request: Request, member_id: str) -> HTMLResponse | RedirectResponse:
        form = await request.form()
        if not valid_csrf(request, form_text(form, "csrf_token")):
            return state_response("Change failed", "Reload and try again.", status_code=403)
        actor = current_account(request)
        try:
            role = AccountRole(form_text(form, "role"))
            status = AccountStatus(form_text(form, "status"))
            grants = (
                frozenset() if role is AccountRole.ADMIN else _form_grants(form.getlist("grants"))
            )
            accounts.set_member(actor.id, UUID(member_id), role=role, grants=grants, status=status)
        except (ValueError, PermissionError) as error:
            return state_response("Change failed", str(error), status_code=400)
        except psycopg.errors.CheckViolation as error:
            if "last active admin" in str(error):
                return state_response(
                    "Keep an active admin",
                    "Assign another admin before disabling or demoting this account.",
                    status_code=409,
                    eyebrow="Administration",
                )
            return state_response("Change unavailable", "Reload and try again.", status_code=503)
        except psycopg.Error:
            return state_response("Change unavailable", "Reload and try again.", status_code=503)
        return RedirectResponse("/members", status_code=303)

    @app.route("/members/{member_id}/reset", methods=["POST"], name="member_reset_issue")
    async def reset_issue(request: Request, member_id: str) -> HTMLResponse:
        form = await request.form()
        if not valid_csrf(request, form_text(form, "csrf_token")):
            return state_response("Reset failed", "Reload and try again.", status_code=403)
        actor = current_account(request)
        try:
            link_token = accounts.issue_reset(actor.id, UUID(member_id))
        except (ValueError, PermissionError) as error:
            return state_response("Reset failed", str(error), status_code=400)
        except psycopg.Error:
            return state_response("Reset unavailable", "Reload and try again.", status_code=503)
        url = str(request.url_for("member_accept_reset_form", token=link_token))
        return _link_response(request, "Password reset ready", url, "This link expires in 1 hour.")


def _register_account_link_routes(app: FastHTML, accounts: AccountService) -> None:
    @app.route("/invite/{token}", methods=["GET"], name="member_accept_invite_form")
    def accept_invite_form(request: Request, token: str) -> HTMLResponse:
        return _password_form(request, token, "Accept invitation", "member_accept_invite_submit")

    @app.route("/invite/{token}", methods=["POST"], name="member_accept_invite_submit")
    async def accept_invite_submit(request: Request, token: str) -> HTMLResponse | RedirectResponse:
        form = await request.form()
        if not valid_csrf(request, form_text(form, "csrf_token")):
            return state_response("Invitation failed", "Reload and try again.", status_code=403)
        password = form_text(form, "password")
        if password != form_text(form, "password_confirmation"):
            return state_response("Invitation failed", "Passwords differ.", status_code=400)
        try:
            member = accounts.accept_invite(token, password)
        except ValueError:
            return state_response(
                "Invitation failed", "Use a password of 12 to 1024 characters.", status_code=400
            )
        except psycopg.Error:
            return state_response(
                "Invitation unavailable", "Reload and try again.", status_code=503
            )
        if member is None:
            return state_response(
                "Invitation unavailable", "The link expired or was already used.", status_code=410
            )
        return RedirectResponse("/login", status_code=303)

    @app.route("/reset/{token}", methods=["GET"], name="member_accept_reset_form")
    def accept_reset_form(request: Request, token: str) -> HTMLResponse:
        return _password_form(request, token, "Reset password", "member_accept_reset_submit")

    @app.route("/reset/{token}", methods=["POST"], name="member_accept_reset_submit")
    async def accept_reset_submit(request: Request, token: str) -> HTMLResponse | RedirectResponse:
        form = await request.form()
        if not valid_csrf(request, form_text(form, "csrf_token")):
            return state_response("Reset failed", "Reload and try again.", status_code=403)
        password = form_text(form, "password")
        if password != form_text(form, "password_confirmation"):
            return state_response("Reset failed", "Passwords differ.", status_code=400)
        try:
            accepted = accounts.accept_reset(token, password)
        except ValueError:
            return state_response(
                "Reset failed", "Use a password of 12 to 1024 characters.", status_code=400
            )
        except psycopg.Error:
            return state_response("Reset unavailable", "Reload and try again.", status_code=503)
        if not accepted:
            return state_response(
                "Reset unavailable", "The link expired or was already used.", status_code=410
            )
        return RedirectResponse("/login", status_code=303)


def _preset(value: str) -> Preset:
    try:
        return Preset(value)
    except ValueError:
        return Preset.REVIEWER


def _form_grants(values: Sequence[object]) -> frozenset[Capability]:
    return frozenset(Capability(str(value)) for value in values)


def _grant_fields(grants: frozenset[Capability]) -> object:
    areas = (
        ("review", "Review"),
        ("search", "Search setup"),
        ("activity", "Recent activity"),
        ("analytics", "Analytics"),
        ("control", "Control plane"),
        ("admin", "Administration"),
    )
    return Div(
        *(
            Fieldset(
                Legend(title),
                *(
                    Label(
                        Input(
                            type="checkbox",
                            name="grants",
                            value=capability.value,
                            checked=capability in grants,
                        ),
                        capability.value.split(".", 1)[1].replace("_", " ").title(),
                    )
                    for capability in Capability
                    if capability.value.startswith(f"{area}.")
                ),
                cls="grant-area",
            )
            for area, title in areas
        ),
        cls="grant-grid",
    )


def _invite_response(
    actor: Account, csrf: str, preset: Preset, error: str | None = None, status: int = 200
) -> HTMLResponse:
    options = (Preset.REVIEWER, Preset.SEARCH_MANAGER, Preset.OPERATOR)
    if actor.role is AccountRole.ADMIN:
        options += (Preset.ADMIN,)
    content = Div(
        Small("Administration", cls="eyebrow"),
        H1("Invite a member"),
        P("The link is proof of invitation. Share it only with the intended person."),
        P(error, role="alert", cls="form-error") if error else None,
        Div(
            *(
                A(
                    value.value,
                    href=f"/members/invite?preset={value.value}",
                    cls="preset-link",
                    aria_current="true" if value is preset else None,
                )
                for value in options
            ),
            cls="preset-list",
        ),
        Form(
            Input(type="hidden", name="csrf_token", value=csrf),
            Input(type="hidden", name="preset", value=preset.value),
            Label("Email", Input(type="email", name="email", required=True)),
            P(f"Starting preset: {preset.value}"),
            P("Admins can use every area and action. Their grants cannot be edited.")
            if preset is Preset.ADMIN
            else _grant_fields(preset_grants(preset)),
            Button("Create invite link", type="submit", cls="member-primary"),
            action="/members/invite",
            method="post",
            cls="member-form",
        ),
        cls="review-shell member-page",
    )
    return HTMLResponse(
        document(sidebar_page("members", csrf, content, grants=actor.capabilities)),
        status_code=status,
    )


def _member_card(member: Account, actor: Account, csrf: str, can_manage: bool) -> object:
    editable = can_manage and (actor.role is AccountRole.ADMIN or member.role is AccountRole.MEMBER)
    return Div(
        Div(
            H2(member.email),
            P(f"{member.role.value.title()} · {member.status.value.title()}"),
            cls="member-heading",
        ),
        Form(
            Input(type="hidden", name="csrf_token", value=csrf),
            Div(
                Label(
                    "Role",
                    Select(
                        Option(
                            "Member", value="member", selected=member.role is AccountRole.MEMBER
                        ),
                        Option("Admin", value="admin", selected=member.role is AccountRole.ADMIN)
                        if actor.role is AccountRole.ADMIN
                        else None,
                        name="role",
                    ),
                ),
                Label(
                    "Status",
                    Select(
                        Option(
                            "Active", value="active", selected=member.status is AccountStatus.ACTIVE
                        ),
                        Option(
                            "Disabled",
                            value="disabled",
                            selected=member.status is AccountStatus.DISABLED,
                        ),
                        name="status",
                    ),
                ),
                cls="member-controls",
            ),
            P(
                "Admin access includes every capability. To demote this account, choose Member and save; then select its grants."
            )
            if member.role is AccountRole.ADMIN
            else _grant_fields(member.grants),
            Button("Save access", type="submit", cls="member-primary"),
            action=f"/members/{member.id}",
            method="post",
            cls="member-form",
        )
        if editable
        else None,
        Form(
            Input(type="hidden", name="csrf_token", value=csrf),
            Button("Create password reset link", type="submit"),
            action=f"/members/{member.id}/reset",
            method="post",
            cls="member-reset",
        )
        if editable and member.status is AccountStatus.ACTIVE
        else None,
        cls="member-card",
    )


def _link_response(request: Request, title: str, url: str, expiry: str) -> HTMLResponse:
    actor = current_account(request)
    csrf = ensure_csrf_token(request)
    content = Div(
        Small("Administration", cls="eyebrow"),
        H1(title),
        P(expiry),
        P("Copy this link now. It will not be shown again."),
        Div(
            Label("One-use link", Input(type="url", value=url, readonly=True, id="account-link")),
            Button("Copy link", type="button", id="copy-account-link", cls="member-primary"),
            cls="link-copy",
        ),
        P("", id="copy-status", role="status"),
        A("Back to members", href="/members"),
        cls="review-shell member-page",
    )
    return HTMLResponse(
        document(
            sidebar_page("members", csrf, content, grants=actor.capabilities),
            scripts=(Script(src=static_url("member_links.js"), defer=True),),
        )
    )


def _password_form(request: Request, token: str, title: str, action_name: str) -> HTMLResponse:
    csrf = ensure_csrf_token(request)
    content = Div(
        H1(title),
        Form(
            Input(type="hidden", name="csrf_token", value=csrf),
            Label("Password", Input(type="password", name="password", required=True)),
            Label(
                "Confirm password",
                Input(type="password", name="password_confirmation", required=True),
            ),
            Button("Continue", type="submit"),
            action=str(request.url_for(action_name, token=token)),
            method="post",
        ),
        cls="review-shell",
    )
    return HTMLResponse(document(content, title=title))
