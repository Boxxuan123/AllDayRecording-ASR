from __future__ import annotations

from typing import Any

from allday_asr.storage.database import Database


def review_candidate(
    database: Database,
    recording_id: int | None,
    candidate_id: int,
    *,
    session_id: int | None = None,
    status: str,
    title: str | None = None,
    body: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    candidate = database.get_semantic_candidate(candidate_id)
    run = database.get_processing_run(int(candidate["run_id"]))
    if session_id is not None:
        if int(run["session_id"]) != session_id:
            raise ValueError("语义候选不属于当前录音会话")
    elif recording_id is None or int(run["recording_id"]) != recording_id:
        raise ValueError("语义候选不属于当前录音")
    revision = database.create_semantic_candidate_revision(
        candidate_id,
        status=status,
        title=title,
        body=body,
        note=note,
    )
    return {
        "candidate_id": candidate_id,
        "run_id": int(candidate["run_id"]),
        "status": str(revision["status"]),
        "title": str(revision["title"]),
        "body": str(revision["body"]),
        "note": revision["note"],
        "revision_index": int(revision["revision_index"]),
        "created_at": str(revision["created_at"]),
    }
