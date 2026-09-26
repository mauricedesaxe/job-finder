"""The authenticated account attached by the web request guard."""

from __future__ import annotations

from job_finder.review.accounts import Account


def current_account(request: object) -> Account:
    principal = getattr(getattr(request, "state", None), "principal", None)
    if not isinstance(principal, Account):
        raise RuntimeError("authenticated account required")
    return principal


def actor_email(request: object) -> str:
    return current_account(request).email
