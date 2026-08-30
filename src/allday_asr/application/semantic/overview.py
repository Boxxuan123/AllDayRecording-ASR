from __future__ import annotations

from typing import Any

from allday_asr.application.semantic.contracts import (
    DEFAULT_SEMANTIC_VERSION,
    SemanticVersion,
    require_semantic_version,
)
from allday_asr.application.semantic.versions import overview_for
from allday_asr.storage.database import Database


def overview(
    database: Database,
    recording_id: int | None = None,
    *,
    session_id: int | None = None,
    version: SemanticVersion | str | None = None,
) -> dict[str, Any]:
    recording_id, session_id = _resolve_target(
        database,
        recording_id,
        session_id,
    )
    latest = _latest_version(database, session_id)
    resolved = require_semantic_version(version) if version is not None else latest
    if version is not None and resolved is not latest:
        raise ValueError(
            f"最新 semantic run 是 {latest.value}，不能按 {resolved.value} 展示"
        )
    presenter = overview_for(resolved)
    if resolved is SemanticVersion.V2_E_0_2:
        return presenter(
            database,
            recording_id,
            session_id=session_id,
        )
    if recording_id is None:
        raise ValueError(f"{resolved.value} overview 需要 legacy recording_id")
    return presenter(database, recording_id)


def _latest_version(
    database: Database,
    session_id: int,
) -> SemanticVersion:
    completed = [
        row
        for row in database.list_session_processing_runs(session_id)
        if str(row["run_kind"]) == "semantic_v2e0"
        and str(row["status"]) == "completed"
    ]
    if not completed:
        return DEFAULT_SEMANTIC_VERSION
    return require_semantic_version(str(completed[-1]["pipeline_version"]))


def _resolve_target(
    database: Database,
    recording_id: int | None,
    session_id: int | None,
) -> tuple[int | None, int]:
    if session_id is None:
        if recording_id is None:
            raise ValueError("必须指定 recording_id 或 session_id")
        database.get_recording(recording_id)
        session = database.get_session_for_recording(recording_id)
        return recording_id, int(session["id"])
    session = database.get_recording_session(session_id)
    legacy_recording_id = (
        int(session["legacy_recording_id"])
        if session["legacy_recording_id"] is not None
        else None
    )
    if recording_id is not None and recording_id != legacy_recording_id:
        raise ValueError("recording_id 与 session_id 不属于同一录音会话")
    return legacy_recording_id, session_id
