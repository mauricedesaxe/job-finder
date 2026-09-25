from __future__ import annotations

import hmac
import secrets
from typing import cast

from starlette.datastructures import FormData
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_SECURITY_HEADERS = (
    (b"cache-control", b"no-store"),
    (
        b"content-security-policy",
        b"default-src 'self'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'",
    ),
    (b"referrer-policy", b"no-referrer"),
    (b"strict-transport-security", b"max-age=63072000; includeSubDomains"),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
)


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app: ASGIApp = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = cast(list[tuple[bytes, bytes]], message.setdefault("headers", []))
                present = {name.lower() for name, _value in headers}
                headers.extend(
                    (name, value) for name, value in _SECURITY_HEADERS if name not in present
                )
            await send(message)

        await self.app(scope, receive, send_with_headers)


def authenticate_session(request: Request) -> None:
    request.session.clear()
    request.session.update({"authenticated": True, "csrf_token": secrets.token_urlsafe(32)})


def ensure_csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if isinstance(token, str):
        return token
    token = secrets.token_urlsafe(32)
    request.session["csrf_token"] = token
    return token


def form_text(form: FormData, key: str) -> str:
    value = form.get(key)
    return value if isinstance(value, str) else ""


def valid_csrf(request: Request, supplied: str) -> bool:
    expected = request.session.get("csrf_token")
    return isinstance(expected, str) and hmac.compare_digest(supplied, expected)


def csrf_token(request: Request) -> str | None:
    value = request.session.get("csrf_token")
    return value if isinstance(value, str) else None


def verified_csrf_token(request: Request, form: FormData) -> str | None:
    supplied = form_text(form, "csrf_token")
    if not valid_csrf(request, supplied):
        return None
    return csrf_token(request)


def verified_control_csrf_token(request: Request, form: FormData) -> str | None:
    values = form.getlist("csrf_token")
    if len(values) != 1 or not isinstance(values[0], str):
        return None
    if not valid_csrf(request, values[0]):
        return None
    return csrf_token(request)
