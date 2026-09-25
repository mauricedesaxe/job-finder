from __future__ import annotations

import hashlib
import json


def canonical_digest(value: object) -> str:
    content = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(content.encode()).hexdigest()
