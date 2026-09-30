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
from allday_asr.v3.adapters.transfer.completion import (
    confirm_duplicate_completion,
    record_completion_confirmation,
)
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
_MANIFEST_FORMAT = "AllDayRecording session manifest v2"
_LEGACY_MANIFEST_FORMAT = "AllDayRecording session manifest v1"
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
        if isinstance(payload, dict) and payload.get("format") == _LEGACY_MANIFEST_FORMAT:
            # Already admitted V1 sessions remain replayable, but V1 can no
            # longer admit new audio without an explicit finalization record.
            old_key = payload.get("sessionKey")
            if isinstance(old_key, str) and old_key:
                old_ref = (
                    f"phone-upload:{self.trust.receiver_id}:{key_id}:{old_key}"
                )
                old_id = stable_ulid(old_ref)
                with self._uow_factory() as uow:
                    saved = uow.idempotency.response(f"phone-manifest:{old_id}")
                    if saved is not None and saved.get("manifest_sha256") == record.sha256:
                        return saved
            raise UploadStoreError("旧版清单缺少录音结束证明；请在手机明确确认旧录音完整")
        manifest = _parse_manifest(payload)
        device_id = self.trust.domain_device_id(key_id)
        session_ref = (
            f"phone-upload:{self.trust.receiver_id}:{key_id}:"
            f"{manifest['sessionKey']}"
        )
        session_id = stable_ulid(session_ref)
        idempotency_key = f"phone-manifest:{session_id}"

        # A retried manifest must not copy and hash every audio asset again.
        with self._uow_factory() as uow:
            saved = uow.idempotency.response(idempotency_key)
            if saved is not None and saved.get("manifest_sha256") == record.sha256:
                return saved
            confirmed = uow.idempotency.response(
                f"phone-manifest-confirmation:{session_id}:{record.sha256}"
            )
            if confirmed is not None:
                return confirmed
            revision = uow.catalog.connection.execute(
                "SELECT input_revision, entries_json FROM session_manifest_revisions "
                "WHERE session_id = ? AND sha256 = ?",
                (session_id, record.sha256),
            ).fetchone()
            if revision is not None:
                entries = json.loads(revision["entries_json"])
                return {"status": "already_ingested", "session_id": session_id,
                        "asset_count": len(entries["chunks"]),
                        "input_revision": int(revision["input_revision"]),
                        "manifest_sha256": record.sha256}

        manifest_directory = PurePosixPath(record.relative_path).parent
        referenced_paths = {
            (manifest_directory / chunk["fileName"]).as_posix()
            for chunk in manifest["chunks"]
        }
        completed: dict[str, UploadRecord] = {}
        for upload in upload_store.list_uploads(
            kind="recording", status="completed", relative_paths=referenced_paths
        ):
            previous = completed.get(upload.relative_path)
            if previous is not None and previous.sha256 != upload.sha256:
                raise UploadConflictError(
                    f"同一分片路径存在不同哈希版本：{upload.relative_path}"
                )
            completed[upload.relative_path] = upload
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
            "input_revision": 1,
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
                    return self._append_existing_manifest(
                        uow, existing, manifest, prepared, record, manifest_path,
                        upload_store, device_id, session_ref, now,
                    )
                return saved
            if not uow.idempotency.begin(idempotency_key, "phone.upload.ingest"):
                raise UploadConflictError("Phone 会话正在由另一个请求入库")
            if not uow.catalog.add_session(session):
                raise UploadConflictError("Phone 会话身份与现有目录冲突")
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

    def _append_existing_manifest(
        self, uow: UnitOfWork, existing: RecordingSession,
        manifest: Mapping[str, Any],
        prepared: list[tuple[Mapping[str, Any], UploadRecord]],
        record: UploadRecord, manifest_path: Any, upload_store: UploadStore,
        device_id: str, session_ref: str, now: datetime,
    ) -> dict[str, Any]:
        connection = uow.catalog.connection
        latest = connection.execute(
            "SELECT input_revision, sha256, entries_json FROM session_manifest_revisions "
            "WHERE session_id = ? ORDER BY input_revision DESC LIMIT 1",
            (existing.session_id,),
        ).fetchone()
        original = connection.execute(
            "SELECT sha256, entries_json FROM session_manifests WHERE session_id = ?",
            (existing.session_id,),
        ).fetchone()
        if original is None:
            return confirm_duplicate_completion(
                uow, existing, manifest, prepared, record, manifest_path,
                self.artifact_store, device_id,
            )
        previous = json.loads(latest["entries_json"] if latest else original["entries_json"])
        previous_revision = int(latest["input_revision"]) if latest else 1
        old_chunks = previous["chunks"]
        if any(previous.get(key) != manifest[key] for key in
               ("sessionKey", "sessionStartedAt", "device", "timezone", "audio")):
            raise UploadConflictError("清单身份或音频格式改变，不能追加到原会话")
        if manifest["chunks"][:len(old_chunks)] != old_chunks:
            raise UploadConflictError("旧分片的位置或长度改变，不能追加到原会话")
        confirmation_only = (
            previous.get("format") == _LEGACY_MANIFEST_FORMAT
            and len(manifest["chunks"]) == len(old_chunks)
            and all(previous.get(key) == manifest[key] for key in
                    ("completedSegments", "totalSamples", "continuityValid"))
        )
        if len(manifest["chunks"]) <= len(old_chunks) and not confirmation_only:
            raise UploadConflictError("新清单没有连续追加分片，不能替换已确认版本")
        old_assets = connection.execute(
            "SELECT s.sequence, a.sha256, a.size_bytes FROM capture_segments s "
            "JOIN audio_assets a ON a.asset_id = s.asset_id "
            "WHERE s.session_id = ? ORDER BY s.sequence",
            (existing.session_id,),
        ).fetchall()
        if len(old_assets) != len(old_chunks) or any(
            int(asset["sequence"]) != index or
            asset["sha256"] != prepared[index][1].sha256 or
            int(asset["size_bytes"]) != prepared[index][1].size
            for index, asset in enumerate(old_assets)
        ):
            raise UploadConflictError("旧分片哈希与已入库音频不一致，不能追加")

        if confirmation_only:
            # Completion metadata does not change the audio input or invalidate
            # existing processing. Keep the original manifest and its backups.
            return record_completion_confirmation(
                uow, existing, existing.session_id, manifest, record, manifest_path,
                self.artifact_store, device_id, previous_revision,
                latest["sha256"] if latest else original["sha256"],
            )

        next_revision = previous_revision + 1
        stored_manifest = self.artifact_store.put_file(
            manifest_path, expected_sha256=record.sha256,
        )
        started_at = datetime.fromtimestamp(
            manifest["sessionStartedAt"] / 1000, tz=timezone.utc,
        )
        for index in range(len(old_chunks), len(prepared)):
            chunk, upload = prepared[index]
            stored = self.audio_store.put_file(
                upload_store.completed_path(upload), expected_sha256=upload.sha256,
            )
            duration_ms = _samples_to_ms(
                chunk["sampleCount"], manifest["audio"]["sampleRate"],
            )
            asset = AudioAsset(
                asset_id=stable_ulid("audio-asset", upload.sha256),
                sha256=upload.sha256, size_bytes=upload.size,
                duration_ms=duration_ms, format=AudioFormat.WAV,
                media_id=stored.media_id, created_at=now, legacy_ref=None,
            )
            existing_asset = uow.catalog.find_asset_by_sha256(upload.sha256)
            if existing_asset is not None and (
                existing_asset.size_bytes != asset.size_bytes or
                existing_asset.duration_ms != asset.duration_ms or
                existing_asset.media_id != asset.media_id
            ):
                raise UploadConflictError("新增分片摘要对应了不同音频元数据")
            canonical_asset = existing_asset or asset
            if existing_asset is None and not uow.catalog.add_asset(asset):
                raise UploadConflictError("新增音频资产身份冲突")
            replica_id = stable_ulid(
                "phone-replica", device_id, existing.session_id, chunk["index"],
            )
            if not uow.catalog.add_replica(AudioReplica(
                replica_id=replica_id, asset_id=canonical_asset.asset_id,
                device_id=device_id, storage_key=stored.storage_key,
                state=AudioReplicaState.AVAILABLE, verified_at=now,
                created_at=now,
                legacy_ref=f"{session_ref}:chunk:{chunk['index']}",
            )):
                raise UploadConflictError("新增分片副本身份冲突")
            start_ms = _sample_offset_to_ms(
                chunk["firstSample"], manifest["audio"]["sampleRate"],
            )
            if not uow.catalog.add_segment(CaptureSegment(
                segment_id=stable_ulid(
                    "capture-segment", existing.session_id, chunk["index"],
                ),
                session_id=existing.session_id,
                asset_id=canonical_asset.asset_id, replica_id=replica_id,
                sequence=index, session_start_ms=start_ms,
                session_end_ms=_sample_offset_to_ms(
                    chunk["firstSample"] + chunk["sampleCount"],
                    manifest["audio"]["sampleRate"],
                ),
                source_start_ms=0, source_end_ms=duration_ms,
                start_sample=chunk["firstSample"],
                captured_at=started_at + timedelta(milliseconds=start_ms),
                legacy_ref=f"{session_ref}:segment:{chunk['index']}",
            )):
                raise UploadConflictError("新增分片身份冲突")
            uow.changes.append(
                "audio_asset", canonical_asset.asset_id, 1,
                ChangeOperation.UPSERT.value,
                _asset_projection(canonical_asset, existing.session_id, index),
            )

        connection.execute(
            "INSERT INTO session_manifest_revisions "
            "(session_id,input_revision,sha256,storage_ref,entries_json,reason,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (existing.session_id, next_revision, record.sha256,
             stored_manifest.storage_key,
             json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
             "verified_append", now.isoformat()),
        )
        end = started_at + timedelta(milliseconds=_samples_to_ms(
            manifest["totalSamples"], manifest["audio"]["sampleRate"],
        ))
        connection.execute(
            "UPDATE recording_sessions SET captured_end=?, revision=?, "
            "state='admission_pending', status_code='backup_required', "
            "current_stage='backup_admission', progress=0, "
            "blocking_reason='backup_restore_evidence_required', updated_at=? "
            "WHERE session_id=?",
            (end.isoformat().replace("+00:00", "Z"), existing.revision + 1,
             now.isoformat().replace("+00:00", "Z"), existing.session_id),
        )
        refreshed = uow.catalog.get_session(existing.session_id)
        uow.changes.append(
            "recording_session", existing.session_id, refreshed.revision,
            ChangeOperation.UPSERT.value, _session_projection(refreshed, manifest),
        )
        uow.audit.append(
            "phone.upload.append_manifest", f"device:{device_id}",
            "recording_session", existing.session_id,
            {"previous_revision": previous_revision,
             "input_revision": next_revision,
             "old_segments": len(old_chunks),
             "new_segments": len(manifest["chunks"]),
             "previous_manifest_sha256": latest["sha256"] if latest else original["sha256"],
             "manifest_sha256": record.sha256},
        )
        return {"status": "ingested", "session_id": existing.session_id,
                "asset_count": len(prepared), "input_revision": next_revision,
                "manifest_sha256": record.sha256}


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
        "completion",
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
    completion = payload["completion"]
    if not isinstance(completion, dict) or set(completion) != {
        "source", "completedSegments", "totalSamples", "confirmedAt"
    }:
        raise UploadStoreError("V3 会话清单缺少明确的结束证明")
    if not isinstance(completion["source"], str) or completion["source"] not in {
        "watch_stop", "legacy_user_confirmed"
    }:
        raise UploadStoreError("V3 结束证明来源无效")
    for key in ("completedSegments", "totalSamples", "confirmedAt"):
        _nonnegative_int(completion[key], f"completion.{key}")
    if completion["confirmedAt"] <= 0 or (
        completion["completedSegments"] != payload["completedSegments"]
        or completion["totalSamples"] != payload["totalSamples"]
    ):
        raise UploadStoreError("V3 结束证明与最终清单不一致")
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
    if not computed_continuity or not payload["continuityValid"]:
        raise UploadStoreError("V3 结束清单中的分片不连续")
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
