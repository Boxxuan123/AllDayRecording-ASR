from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Sequence


def canonical_source_fingerprint(rows: Sequence[sqlite3.Row]) -> str:
    payload = [
        {
            "position": position,
            "source_object_id": int(row["source_object_id"]),
            "source_instance_id": int(row["source_instance_id"]),
            "instance_key": str(row["instance_key"]),
            "sha256": str(row["sha256"]),
            "session_start_ms": int(row["session_start_ms"]),
            "session_end_ms": int(row["session_end_ms"]),
            "source_start_ms": int(row["source_start_ms"]),
            "source_end_ms": int(row["source_end_ms"]),
            "session_start_sample": row["session_start_sample"],
            "session_end_sample": row["session_end_sample"],
            "source_start_sample": row["source_start_sample"],
            "source_end_sample": row["source_end_sample"],
            "timeline_sample_rate": row["timeline_sample_rate"],
        }
        for position, row in enumerate(rows)
    ]
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
