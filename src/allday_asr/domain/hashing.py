from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> str:
    """Serialize a protocol payload with the project's established JSON bytes."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_json_sha256(value: Any) -> str:
    """Hash canonical JSON without assigning meaning to the payload protocol."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
