from __future__ import annotations

from urllib.parse import ParseResult, urlparse


def parse_http_url(url: str) -> ParseResult | None:
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not hostname:
        return None
    return parsed
