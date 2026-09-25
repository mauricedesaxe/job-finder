from __future__ import annotations

from hashlib import sha256
from pathlib import Path

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


def static_url(name: str) -> str:
    return _STATIC_URLS[name]


def static_asset_path(name: str) -> Path | None:
    return _STATIC_ASSETS.get(name)
