from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, TypedDict

if TYPE_CHECKING:
    from allday_asr.services.daily import DailyRunSummary


class ProcessingRunPayload(TypedDict):
    id: int
    recording_id: int | None
    session_id: int
    run_kind: str
    status: str
    config_sha256: str
    started_at: str
    completed_at: str | None
    error: str | None
    summary: Any
    artifacts: Any


class _DashboardRunOptionalPayload(TypedDict, total=False):
    id: int
    status: str
    started_at: str
    completed_at: str | None
    error: str | None


class DashboardRunPayload(_DashboardRunOptionalPayload):
    available: bool
    summary: dict[str, Any]


class ActionPayload(TypedDict):
    id: int
    recording_id: int
    type: str
    status: str
    title: str
    scheduled_at: str | None
    time_text: str | None
    location: str | None
    confidence: float
    source_segment_ids: Any
    participants: Any
    evidence: Any


class DailyStepPayload(TypedDict):
    name: str
    status: str
    detail: str


class DailyRunPayload(TypedDict):
    run_id: int
    recording_id: int
    status: str
    steps: list[DailyStepPayload]
    review_actions: list[str]
    manifest_markdown_path: str


def json_value(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {"raw": value}


def processing_run_payload(row) -> ProcessingRunPayload:
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


def dashboard_run(row, summary: dict[str, Any]) -> DashboardRunPayload:
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


def action_payload(row) -> ActionPayload:
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


def daily_summary_payload(summary: DailyRunSummary) -> DailyRunPayload:
    return {
        "run_id": summary.run_id,
        "recording_id": summary.recording_id,
        "status": summary.status,
        "steps": [
            {"name": step.name, "status": step.status, "detail": step.detail}
            for step in summary.steps
        ],
        "review_actions": list(summary.review_actions),
        "manifest_markdown_path": str(summary.manifest_markdown_path.resolve()),
    }


__all__ = [
    "ActionPayload",
    "DailyRunPayload",
    "DailyStepPayload",
    "DashboardRunPayload",
    "ProcessingRunPayload",
    "action_payload",
    "daily_summary_payload",
    "dashboard_run",
    "json_value",
    "processing_run_payload",
    "run_summary",
]
