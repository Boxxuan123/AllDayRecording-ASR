from __future__ import annotations

import json
from typing import Any


def json_value(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {"raw": value}


def processing_run_payload(row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "recording_id": (
            int(row["recording_id"]) if row["recording_id"] is not None else None
        ),
        "session_id": int(row["session_id"]),
        "run_kind": row["run_kind"],
        "status": row["status"],
        "config_sha256": row["config_sha256"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "error": row["error"],
        "summary": json_value(row["summary_json"]),
        "artifacts": json_value(row["artifacts_json"]),
    }


def run_summary(row) -> dict[str, Any]:
    if row is None:
        return {}
    value = json_value(row["summary_json"])
    return value if isinstance(value, dict) else {}


def dashboard_run(row, summary: dict[str, Any]) -> dict[str, Any]:
    if row is None:
        return {"available": False, "summary": {}}
    return {
        "available": True,
        "id": int(row["id"]),
        "status": str(row["status"]),
        "started_at": str(row["started_at"]),
        "completed_at": row["completed_at"],
        "error": row["error"],
        "summary": summary,
    }


def action_payload(row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "recording_id": int(row["recording_id"]),
        "type": row["candidate_type"],
        "status": row["status"],
        "title": row["title"],
        "scheduled_at": row["scheduled_at"],
        "time_text": row["time_text"],
        "location": row["location"],
        "confidence": float(row["confidence"]),
        "source_segment_ids": json_value(row["source_segment_ids_json"]),
        "participants": json_value(row["participants_json"]),
        "evidence": json_value(row["evidence_json"]),
    }
