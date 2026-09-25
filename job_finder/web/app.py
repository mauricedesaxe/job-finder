# pyright: reportMissingTypeStubs=false
from __future__ import annotations

from collections.abc import Callable
from hashlib import sha256
from pathlib import Path

import psycopg
from fasthtml.common import Beforeware, FastHTML
from starlette.requests import Request
from starlette.responses import FileResponse, PlainTextResponse, Response

from job_finder.config import ReviewAppSettings
from job_finder.web.security import SecurityHeadersMiddleware

ReadinessProbe = Callable[[], None]
RequestGuard = Callable[[Request], Response | None]

_SESSION_COOKIE = "job_finder_review_session"
_SESSION_MAX_AGE = 14 * 24 * 60 * 60
_STATIC_DIR = Path(__file__).parent / "static"
_ASSET_HASH_LENGTH = 10


def _hashed_static_assets(directory: Path) -> dict[str, Path]:
    assets: dict[str, Path] = {}
    for path in sorted(directory.iterdir()):
        if path.is_file():
            digest = sha256(path.read_bytes()).hexdigest()[:_ASSET_HASH_LENGTH]
            assets[f"{path.stem}.{digest}{path.suffix}"] = path
    return assets


def _hashed_static_urls(assets: dict[str, Path]) -> dict[str, str]:
    urls: dict[str, str] = {}
    for hashed_name, path in assets.items():
        original = f"{path.stem}{path.suffix}"
        urls[original] = f"/static/{hashed_name}"
    return urls


_STATIC_ASSETS = _hashed_static_assets(_STATIC_DIR)
_STATIC_URLS = _hashed_static_urls(_STATIC_ASSETS)


def create_web_app(
    settings: ReviewAppSettings,
    *,
    guard: RequestGuard,
    readiness: ReadinessProbe,
) -> FastHTML:
    app = FastHTML(
        before=Beforeware(
            guard,
            skip=[r"/healthz", r"/readyz", r"/favicon.ico"],
        ),
        default_hdrs=False,
        htmx=False,
        surreal=False,
        secret_key=settings.session_secret,
        session_cookie=_SESSION_COOKIE,
        max_age=_SESSION_MAX_AGE,
        same_site="lax",
        sess_https_only=settings.cookie_secure,
    )

    @app.route("/healthz", methods=["GET"], name="create_review_app_healthz")
    def healthz() -> PlainTextResponse:
        return PlainTextResponse("ok")

    @app.route("/readyz", methods=["GET"], name="create_review_app_readyz")
    def readyz() -> PlainTextResponse:
        try:
            readiness()
        except psycopg.Error:
            return PlainTextResponse("database unavailable", status_code=503)
        return PlainTextResponse("ready")

    @app.route("/favicon.ico", methods=["GET"], name="create_review_app_favicon")
    def favicon() -> Response:
        return Response(status_code=204)

    @app.route("/static/{name}", methods=["GET"], name="create_review_app_static_asset")
    def static_asset(name: str) -> Response:
        path = _STATIC_ASSETS.get(name)
        if path is None or not path.is_file():
            return Response(status_code=404)
        return FileResponse(path, headers={"Cache-Control": "public, max-age=31536000, immutable"})

    app.add_middleware(SecurityHeadersMiddleware)
    return app


def static_url(name: str) -> str:
    return _STATIC_URLS[name]
