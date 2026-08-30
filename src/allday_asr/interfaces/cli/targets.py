from __future__ import annotations

import typer

from allday_asr.storage.database import Database


def resolve_v2_target(
    database: Database,
    recording_id: int | None,
    session_id: int | None,
) -> tuple[int | None, int]:
    if recording_id is None and session_id is None:
        raise typer.BadParameter(
            "必须提供 recording_id，或使用 --session 指定会话"
        )
    if session_id is None:
        session = database.get_session_for_recording(int(recording_id))
        return recording_id, int(session["id"])
    session = database.get_recording_session(session_id)
    legacy = session["legacy_recording_id"]
    if recording_id is not None and legacy is not None and int(legacy) != recording_id:
        raise typer.BadParameter("recording_id 与 --session 不属于同一会话")
    return recording_id, session_id


__all__ = ["resolve_v2_target"]
