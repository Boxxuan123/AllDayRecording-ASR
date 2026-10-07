"""Confirm existing audio without replacing inputs or reviving duplicate sessions."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from allday_asr.v3.domain.models import RecordingSession, RecordingSessionState
from allday_asr.v3.interfaces.transfer.store import UploadConflictError, UploadRecord
from allday_asr.v3.ports.repositories import UnitOfWork
from allday_asr.v3.ports.stores import StoredContent


def record_completion_confirmation(
    uow: UnitOfWork, session: RecordingSession, alias_session_id: str,
    manifest: Mapping[str, Any], record: UploadRecord,
    stored: StoredContent, device_id: str,
    input_revision: int, previous_sha256: str,
) -> dict[str, Any]:
    response = {
        "status": "already_ingested", "session_id": session.session_id,
        "asset_count": len(manifest["chunks"]), "input_revision": input_revision,
        "manifest_sha256": record.sha256, "input_unchanged": True,
    }
    key = f"phone-manifest-confirmation:{alias_session_id}:{record.sha256}"
    if not uow.idempotency.begin(key, "phone.upload.confirm_manifest"):
        raise UploadConflictError("旧录音确认正在由另一个请求处理")
    uow.audit.append(
        "phone.upload.confirm_manifest", f"device:{device_id}",
        "recording_session", session.session_id,
        {"input_revision": input_revision, "alias_session_id": alias_session_id,
         "previous_manifest_sha256": previous_sha256,
         "manifest_sha256": record.sha256, "storage_ref": stored.storage_key,
         "completion": manifest.get("completion"),
         "verified_segments": len(manifest["chunks"])},
    )
    uow.idempotency.complete(key, response)
    return response


def confirm_duplicate_completion(
    uow: UnitOfWork, alias: RecordingSession, manifest: Mapping[str, Any],
    prepared: list[tuple[Mapping[str, Any], UploadRecord]], record: UploadRecord,
    stored: StoredContent, device_id: str,
) -> dict[str, Any]:
    reason = alias.blocking_reason or ""
    if alias.state != RecordingSessionState.QUARANTINED or not reason.startswith("duplicate_manifest:"):
        raise UploadConflictError("原会话缺少可核验的清单，请人工修复")
    canonical_id = reason.removeprefix("duplicate_manifest:")
    canonical = uow.catalog.get_session(canonical_id)
    if canonical is None or canonical.state == RecordingSessionState.QUARANTINED:
        raise UploadConflictError("重复录音的正式记录不可用，请人工修复")
    started_at = datetime.fromtimestamp(manifest["sessionStartedAt"] / 1000, tz=timezone.utc)
    if canonical.captured_start != started_at:
        raise UploadConflictError("重复录音的开始时间与正式记录不同")
    connection = uow.catalog.connection
    latest = connection.execute(
        "SELECT input_revision, sha256, entries_json FROM session_manifest_revisions "
        "WHERE session_id=? ORDER BY input_revision DESC LIMIT 1", (canonical_id,),
    ).fetchone()
    original = connection.execute(
        "SELECT sha256, entries_json FROM session_manifests WHERE session_id=?", (canonical_id,),
    ).fetchone()
    if original is None:
        raise UploadConflictError("重复录音的正式记录缺少可核验的清单")
    previous = json.loads((latest or original)["entries_json"])
    if previous.get("continuityValid", previous.get("declared_continuity_valid")) is not True:
        raise UploadConflictError("重复录音的正式清单未通过连续性校验")
    expected_audio = previous.get("audio") or {
        "sampleRate": previous.get("sample_rate"),
        "channels": previous.get("channels"),
        "bitsPerSample": previous.get("bits_per_sample"),
    }
    if (manifest["audio"] != expected_audio
            or manifest["totalSamples"] != previous.get("totalSamples", previous.get("duration_samples"))
            or manifest["completedSegments"] != previous.get("completedSegments", previous.get("chunk_count"))):
        raise UploadConflictError("重复录音的格式或样本总量与正式记录不同")
    segments = connection.execute(
        "SELECT s.sequence,s.start_sample,s.session_start_ms,s.session_end_ms,a.sha256,a.size_bytes "
        "FROM capture_segments s JOIN audio_assets a ON a.asset_id=s.asset_id "
        "WHERE s.session_id=? ORDER BY s.sequence", (canonical_id,),
    ).fetchall()
    rate = manifest["audio"]["sampleRate"]
    if len(segments) != len(prepared) or any(
        int(segment["sequence"]) != index
        or segment["sha256"] != upload.sha256 or int(segment["size_bytes"]) != upload.size
        or segment["start_sample"] != chunk["firstSample"]
        or int(segment["session_start_ms"]) != round(chunk["firstSample"] * 1000 / rate)
        or int(segment["session_end_ms"]) != round((chunk["firstSample"] + chunk["sampleCount"]) * 1000 / rate)
        for index, (segment, (chunk, upload)) in enumerate(zip(segments, prepared, strict=True))
    ):
        raise UploadConflictError("重复录音的分片哈希或采样位置与正式记录不一致")
    return record_completion_confirmation(
        uow, canonical, alias.session_id, manifest, record, stored,
        device_id, int(latest["input_revision"]) if latest else 1,
        (latest or original)["sha256"],
    )
