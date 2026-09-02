from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Any

from allday_asr.v3.interfaces.transfer.store import (
    UploadConflictError,
    UploadRecord,
    UploadStore,
    UploadStoreError,
)
from allday_asr.v3.adapters.files import ContentAddressedStore
from allday_asr.v3.adapters.transfer.trust import TransferDeviceTrustAdapter
from allday_asr.v3.domain.ids import stable_ulid
from allday_asr.v3.domain.models import (
    AudioAsset,
    AudioFormat,
    AudioReplica,
    AudioReplicaState,
    CaptureSegment,
    ChangeOperation,
    RecordingSession,
    RecordingSessionState,
    SessionManifest,
)
from allday_asr.v3.ports.repositories import UnitOfWork


UnitOfWorkFactory = Callable[[], UnitOfWork]
_MANIFEST_FORMAT = "AllDayRecording session manifest v1"
_MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_MAX_CHUNKS = 50_000


class V3UploadIngestAdapter:
    """Admit a completed Phone manifest and its verified audio into V3 Core."""

    def __init__(
        self,
        trust: TransferDeviceTrustAdapter,
        uow_factory: UnitOfWorkFactory,
        audio_store: ContentAddressedStore,
        artifact_store: ContentAddressedStore,
    ) -> None:
        self.trust = trust
        self._uow_factory = uow_factory
        self.audio_store = audio_store
        self.artifact_store = artifact_store

    def ingest_completed(
        self,
        key_id: str,
        record: UploadRecord,
        upload_store: UploadStore,
    ) -> dict[str, Any] | None:
        if record.status != "completed" or record.kind != "manifest":
            return None
        if record.size > _MAX_MANIFEST_BYTES:
            raise UploadStoreError("V3 会话清单超过 4 MiB 安全上限")
        manifest_path = upload_store.completed_path(record)
        try:
            raw = manifest_path.read_bytes()
            payload = json.loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UploadStoreError("V3 会话清单不是有效的 UTF-8 JSON") from exc
        manifest = _parse_manifest(payload)
        device_id = self.trust.domain_device_id(key_id)
        session_ref = (
            f"phone-upload:{self.trust.receiver_id}:{key_id}:"
            f"{manifest['sessionKey']}"
        )
        session_id = stable_ulid(session_ref)
        idempotency_key = f"phone-manifest:{session_id}"

        completed = {
            upload.relative_path: upload
            for upload in upload_store.list_uploads(
                kind="recording", status="completed"
            )
        }
        manifest_directory = PurePosixPath(record.relative_path).parent
        prepared: list[tuple[Mapping[str, Any], UploadRecord]] = []
        for chunk in manifest["chunks"]:
            relative_path = (manifest_directory / chunk["fileName"]).as_posix()
            upload = completed.get(relative_path)
            if upload is None:
                raise UploadConflictError(
                    f"会话清单引用的录音尚未完整上传：{relative_path}"
                )
            expected_size = 44 + (
                chunk["sampleCount"]
                * manifest["audio"]["channels"]
                * manifest["audio"]["bitsPerSample"]
                // 8
            )
            if upload.size != expected_size:
                raise UploadConflictError(
                    f"录音大小与会话清单不一致：{relative_path}"
                )
            prepared.append((chunk, upload))

        stored_manifest = self.artifact_store.put_file(
            manifest_path, expected_sha256=record.sha256
        )
        stored_audio = [
            self.audio_store.put_file(
                upload_store.completed_path(upload),
                expected_sha256=upload.sha256,
            )
            for _, upload in prepared
        ]
        started_at = datetime.fromtimestamp(
            manifest["sessionStartedAt"] / 1000, tz=timezone.utc
        )
        total_duration_ms = _samples_to_ms(
            manifest["totalSamples"], manifest["audio"]["sampleRate"]
        )
        now = datetime.now(timezone.utc)
        state = (
            RecordingSessionState.ADMISSION_PENDING
            if manifest["continuityValid"]
            else RecordingSessionState.ADMISSION_BLOCKED
        )
        session = RecordingSession(
            session_id=session_id,
            captured_start=started_at,
            captured_end=started_at + timedelta(milliseconds=total_duration_ms),
            timezone=manifest["timezone"],
            state=state,
            revision=1,
            status_code=(
                "backup_required" if manifest["continuityValid"] else "failed"
            ),
            current_stage="backup_admission",
            progress=0.0,
            blocking_reason=(
                "backup_restore_evidence_required"
                if manifest["continuityValid"]
                else "audio_continuity_gap"
            ),
            created_at=now,
            updated_at=now,
            legacy_ref=session_ref,
        )
        response = {
            "status": "ingested",
            "session_id": session_id,
            "asset_count": len(prepared),
            "manifest_sha256": record.sha256,
        }
        with self._uow_factory() as uow:
            existing = uow.catalog.find_session_by_legacy_ref(session_ref)
            canonical = uow.catalog.find_session_by_manifest_sha256(record.sha256)
            if canonical is not None and canonical.session_id != session_id:
                if existing is not None:
                    reason = f"duplicate_manifest:{canonical.session_id}"
                    revision = uow.catalog.tombstone_duplicate_session(
                        existing.session_id,
                        canonical.session_id,
                        now,
                    )
                    if revision is not None:
                        uow.tombstones.add(
                            "recording_session",
                            existing.session_id,
                            revision,
                            reason,
                        )
                        uow.changes.append(
                            "recording_session",
                            existing.session_id,
                            revision,
                            ChangeOperation.TOMBSTONE.value,
                            {
                                "session_id": existing.session_id,
                                "canonical_session_id": canonical.session_id,
                                "reason": reason,
                            },
                        )
                        uow.audit.append(
                            "phone.upload.deduplicate",
                            f"device:{device_id}",
                            "recording_session",
                            existing.session_id,
                            {
                                "canonical_session_id": canonical.session_id,
                                "manifest_sha256": record.sha256,
                            },
                        )
                return {
                    **response,
                    "status": "already_ingested",
                    "session_id": canonical.session_id,
                }
            if existing is not None:
                saved = uow.idempotency.response(idempotency_key)
                if saved is None or saved.get("manifest_sha256") != record.sha256:
                    raise UploadConflictError(
                        "同一 Phone 会话已提交不同的清单内容"
                    )
                return saved
            if not uow.idempotency.begin(idempotency_key, "phone.upload.ingest"):
                raise UploadConflictError("Phone 会话正在由另一个请求入库")
            if not uow.catalog.add_session(session):
                raise UploadConflictError("Phone 会话身份与现有目录冲突")
            for index, ((chunk, upload), stored) in enumerate(
                zip(prepared, stored_audio, strict=True)
            ):
                duration_ms = _samples_to_ms(
                    chunk["sampleCount"], manifest["audio"]["sampleRate"]
                )
                asset = AudioAsset(
                    asset_id=stable_ulid("audio-asset", upload.sha256),
                    sha256=upload.sha256,
                    size_bytes=upload.size,
                    duration_ms=duration_ms,
                    format=AudioFormat.WAV,
                    media_id=stored.media_id,
                    created_at=now,
                    legacy_ref=None,
                )
                existing_asset = uow.catalog.find_asset_by_sha256(upload.sha256)
                if existing_asset is not None and (
                    existing_asset.size_bytes != asset.size_bytes
                    or existing_asset.duration_ms != asset.duration_ms
                    or existing_asset.media_id != asset.media_id
                ):
                    raise UploadConflictError("相同音频摘要对应了不同元数据")
                canonical_asset = existing_asset or asset
                if existing_asset is None:
                    uow.catalog.add_asset(asset)
                replica_id = stable_ulid(
                    "phone-replica", device_id, session_id, chunk["index"]
                )
                uow.catalog.add_replica(
                    AudioReplica(
                        replica_id=replica_id,
                        asset_id=canonical_asset.asset_id,
                        device_id=device_id,
                        storage_key=stored.storage_key,
                        state=AudioReplicaState.AVAILABLE,
                        verified_at=now,
                        created_at=now,
                        legacy_ref=f"{session_ref}:chunk:{chunk['index']}",
                    )
                )
                start_ms = _sample_offset_to_ms(
                    chunk["firstSample"], manifest["audio"]["sampleRate"]
                )
                uow.catalog.add_segment(
                    CaptureSegment(
                        segment_id=stable_ulid(
                            "capture-segment", session_id, chunk["index"]
                        ),
                        session_id=session_id,
                        asset_id=canonical_asset.asset_id,
                        replica_id=replica_id,
                        sequence=index,
                        session_start_ms=start_ms,
                        session_end_ms=_sample_offset_to_ms(
                            chunk["firstSample"] + chunk["sampleCount"],
                            manifest["audio"]["sampleRate"],
                        ),
                        source_start_ms=0,
                        source_end_ms=duration_ms,
                        start_sample=chunk["firstSample"],
                        captured_at=started_at
                        + timedelta(milliseconds=start_ms),
                        legacy_ref=f"{session_ref}:segment:{chunk['index']}",
                    )
                )
                uow.changes.append(
                    "audio_asset",
                    canonical_asset.asset_id,
                    1,
                    ChangeOperation.UPSERT.value,
                    _asset_projection(canonical_asset, session_id, index),
                )
            uow.catalog.add_manifest(
                SessionManifest(
                    manifest_id=stable_ulid("session-manifest", session_id),
                    session_id=session_id,
                    schema_version=_MANIFEST_FORMAT,
                    sha256=record.sha256,
                    storage_ref=stored_manifest.storage_key,
                    entries=dict(payload),
                    created_at=now,
                    legacy_ref=f"{session_ref}:manifest",
                )
            )
            uow.changes.append(
                "recording_session",
                session_id,
                session.revision,
                ChangeOperation.UPSERT.value,
                _session_projection(session, manifest),
            )
            uow.audit.append(
                "phone.upload.ingest",
                f"device:{device_id}",
                "recording_session",
                session_id,
                {
                    "manifest_sha256": record.sha256,
                    "asset_count": len(prepared),
                    "continuity_valid": manifest["continuityValid"],
                },
                legacy_ref=f"phone-upload-ingest:{session_id}",
            )
            uow.idempotency.complete(idempotency_key, response)
        return response


def _parse_manifest(payload: object) -> dict[str, Any]:
    required = {
        "format",
        "sessionKey",
        "sessionStartedAt",
        "device",
        "timezone",
        "audio",
        "chunks",
        "completedSegments",
        "totalSamples",
        "continuityValid",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise UploadStoreError("V3 会话清单字段不符合协议")
    if payload["format"] != _MANIFEST_FORMAT:
        raise UploadStoreError("V3 会话清单版本不受支持")
    for key in ("sessionKey", "device", "timezone"):
        value = payload[key]
        if not isinstance(value, str) or not value or len(value) > 160:
            raise UploadStoreError(f"V3 会话清单 {key} 无效")
    for key in ("sessionStartedAt", "completedSegments", "totalSamples"):
        _nonnegative_int(payload[key], key)
    if payload["sessionStartedAt"] <= 0 or payload["totalSamples"] <= 0:
        raise UploadStoreError("V3 会话时间和样本总数必须为正数")
    if not isinstance(payload["continuityValid"], bool):
        raise UploadStoreError("V3 continuityValid 必须是布尔值")
    audio = payload["audio"]
    if not isinstance(audio, dict) or set(audio) != {
        "sampleRate",
        "channels",
        "bitsPerSample",
    }:
        raise UploadStoreError("V3 audio 字段无效")
    for key in ("sampleRate", "channels", "bitsPerSample"):
        _nonnegative_int(audio[key], key)
        if audio[key] <= 0:
            raise UploadStoreError(f"V3 audio.{key} 必须为正数")
    if audio["bitsPerSample"] % 8 != 0:
        raise UploadStoreError("V3 bitsPerSample 必须按整字节对齐")
    chunks = payload["chunks"]
    if not isinstance(chunks, list) or not 1 <= len(chunks) <= _MAX_CHUNKS:
        raise UploadStoreError("V3 chunks 数量无效")
    if payload["completedSegments"] != len(chunks):
        raise UploadStoreError("V3 completedSegments 与 chunks 不一致")
    seen_indexes: set[int] = set()
    highest_sample = 0
    previous_end = 0
    previous_index: int | None = None
    computed_continuity = True
    for chunk in chunks:
        if not isinstance(chunk, dict) or set(chunk) != {
            "index",
            "fileName",
            "firstSample",
            "sampleCount",
        }:
            raise UploadStoreError("V3 chunk 字段无效")
        for key in ("index", "firstSample", "sampleCount"):
            _nonnegative_int(chunk[key], key)
        name = chunk["fileName"]
        if (
            not isinstance(name, str)
            or not name
            or len(name) > 128
            or PurePosixPath(name).name != name
            or "\\" in name
            or not name.lower().endswith(".wav")
        ):
            raise UploadStoreError("V3 chunk fileName 必须是 WAV 基础文件名")
        if chunk["index"] in seen_indexes or chunk["sampleCount"] <= 0:
            raise UploadStoreError("V3 chunk index 重复或 sampleCount 无效")
        if chunk["firstSample"] != previous_end:
            computed_continuity = False
        if previous_index is not None and chunk["index"] != previous_index + 1:
            computed_continuity = False
        seen_indexes.add(chunk["index"])
        previous_end = chunk["firstSample"] + chunk["sampleCount"]
        previous_index = chunk["index"]
        highest_sample = max(
            highest_sample, chunk["firstSample"] + chunk["sampleCount"]
        )
    if highest_sample != payload["totalSamples"]:
        raise UploadStoreError("V3 totalSamples 与 chunks 不一致")
    if payload["continuityValid"] and not computed_continuity:
        payload = dict(payload)
        payload["continuityValid"] = False
    return payload


def _nonnegative_int(value: object, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise UploadStoreError(f"V3 {label} 必须是非负整数")


def _samples_to_ms(samples: int, sample_rate: int) -> int:
    return max(1, round(samples * 1000 / sample_rate))


def _sample_offset_to_ms(samples: int, sample_rate: int) -> int:
    return round(samples * 1000 / sample_rate)


def _session_projection(
    session: RecordingSession, manifest: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "session_id": session.session_id,
        # Phone owns the original audio files under this stable manifest key.
        # Publishing it once lets the device bind local facts to the V3 session
        # without guessing from capture timestamps or file names.
        "session_key": manifest["sessionKey"],
        "captured_start": session.captured_start.isoformat().replace(
            "+00:00", "Z"
        ),
        "captured_end": session.captured_end.isoformat().replace(
            "+00:00", "Z"
        )
        if session.captured_end is not None
        else None,
        "timezone": session.timezone,
        "state": session.state.value,
        "revision": session.revision,
        "status_code": session.status_code,
        "progress": session.progress,
        "device_name": manifest["device"],
    }


def _asset_projection(
    asset: AudioAsset, session_id: str, sequence: int
) -> dict[str, Any]:
    return {
        "asset_id": asset.asset_id,
        "session_id": session_id,
        "sequence": sequence,
        "sha256": asset.sha256,
        "size_bytes": asset.size_bytes,
        "duration_ms": asset.duration_ms,
        "format": asset.format.value,
    }


__all__ = ["V3UploadIngestAdapter"]
