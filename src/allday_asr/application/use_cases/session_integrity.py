from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from allday_asr.audio.tools import sha256_file
from allday_asr.storage.database import Database


def verify_session_inputs(database: Database, session_id: int) -> dict[str, Any]:
    """Re-hash immutable inputs and report mapping continuity without decoding."""
    session = database.get_recording_session(session_id)
    if str(session["status"]) != "closed":
        raise RuntimeError("V2 质量工作流只接受已经关闭并冻结的录音会话")
    rows = database.list_session_sources(session_id)
    if not rows:
        raise RuntimeError("录音会话没有原始音频实例")

    checked_at = datetime.now(timezone.utc).isoformat()
    instances: list[dict[str, Any]] = []
    for row in rows:
        path = Path(str(row["source_path"]))
        status = "verified"
        actual_size: int | None = None
        actual_sha256: str | None = None
        error: str | None = None
        if not path.is_file():
            status = "missing"
        else:
            try:
                actual_size = path.stat().st_size
                actual_sha256 = sha256_file(path)
                if (
                    actual_size != int(row["instance_byte_size"])
                    or actual_sha256 != str(row["sha256"])
                ):
                    status = "mismatch"
            except OSError as exc:
                status = "error"
                error = repr(exc)
        database.update_source_instance_integrity(
            int(row["source_instance_id"]),
            status=status,
            verified_at=checked_at if status == "verified" else None,
        )
        instances.append(
            {
                "source_instance_id": int(row["source_instance_id"]),
                "chunk_index": int(row["chunk_index"]),
                "status": status,
                "expected_byte_size": int(row["instance_byte_size"]),
                "actual_byte_size": actual_size,
                "expected_sha256": str(row["sha256"]),
                "actual_sha256": actual_sha256,
                "error": error,
            }
        )

    manifest_row = database.get_session_manifest(session_id)
    manifest_result: dict[str, Any]
    if manifest_row is None:
        manifest_result = {"status": "not_applicable", "reason": "legacy_session"}
    else:
        manifest_path = Path(str(manifest_row["manifest_path"]))
        manifest_status = "verified"
        actual_manifest_size: int | None = None
        actual_manifest_sha256: str | None = None
        manifest_error: str | None = None
        if not manifest_path.is_file():
            manifest_status = "missing"
        else:
            try:
                actual_manifest_size = manifest_path.stat().st_size
                actual_manifest_sha256 = sha256_file(manifest_path)
                if (
                    actual_manifest_size != int(manifest_row["byte_size"])
                    or actual_manifest_sha256
                    != str(manifest_row["manifest_sha256"])
                ):
                    manifest_status = "mismatch"
            except OSError as exc:
                manifest_status = "error"
                manifest_error = repr(exc)
        manifest_result = {
            "status": manifest_status,
            "expected_byte_size": int(manifest_row["byte_size"]),
            "actual_byte_size": actual_manifest_size,
            "expected_sha256": str(manifest_row["manifest_sha256"]),
            "actual_sha256": actual_manifest_sha256,
            "error": manifest_error,
        }

    gaps, overlaps = mapping_discontinuities(
        rows, duration_ms=int(session["duration_ms"])
    )
    return {
        "checked_at": checked_at,
        "instances": instances,
        "manifest": manifest_result,
        "gaps": [list(value) for value in gaps],
        "overlaps": [list(value) for value in overlaps],
    }


def mapping_discontinuities(
    rows: list[Any], *, duration_ms: int
) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    gaps: list[tuple[int, int]] = []
    overlaps: list[tuple[int, int]] = []
    cursor = 0
    for row in sorted(rows, key=lambda item: int(item["session_start_ms"])):
        start_ms = int(row["session_start_ms"])
        end_ms = int(row["session_end_ms"])
        if start_ms > cursor:
            gaps.append((cursor, start_ms))
        elif start_ms < cursor:
            overlaps.append((start_ms, min(cursor, end_ms)))
        cursor = max(cursor, end_ms)
    if cursor < duration_ms:
        gaps.append((cursor, duration_ms))
    return gaps, overlaps
