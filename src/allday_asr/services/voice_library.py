from __future__ import annotations

import importlib.metadata
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import soundfile as sf

from allday_asr.asr.funasr_backend import SPEAKER_MODEL_ID, FunASRBackend
from allday_asr.audio.tools import extract_clip, sha256_file
from allday_asr.paths import OUTPUT_DIR, STATE_DIR, recording_output_dir
from allday_asr.services.enrollment import l2_normalize, normalize_speech_level
from allday_asr.storage.database import Database


LIBRARY_ROOT = STATE_DIR / "voice-library"


@dataclass(frozen=True)
class PersonLibraryStatus:
    person_id: int
    display_name: str
    profile_type: str
    status: str
    effective_speech_seconds: float
    enrollment_sources: int
    accepted_conversation_samples: int
    holdout_samples: int
    active_embeddings: int
    accepted_sessions: int
    threshold: float | None
    manifest_path: Path


@dataclass(frozen=True)
class EnrollmentSyncSummary:
    people: int
    source_rows: int
    manifest_path: Path


@dataclass(frozen=True)
class AccumulationSummary:
    recording_id: int
    requested_split: str
    person_samples: int
    negative_samples: int
    mixed_samples: int
    uncertain_samples: int
    embedded_samples: int
    profile_embeddings_added: int
    manifest_path: Path


def sync_all_enrollments(database: Database) -> EnrollmentSyncSummary:
    source_rows = 0
    people = 0
    for profile in database.list_person_profiles():
        if not profile["embedding_path"] or not Path(profile["embedding_path"]).is_file():
            continue
        source_rows += sync_person_enrollment(database, int(profile["id"]))
        people += 1
    global_manifest = write_library_manifests(database)
    return EnrollmentSyncSummary(people, source_rows, global_manifest)


def sync_person_enrollment(database: Database, person_id: int) -> int:
    profile = database.get_person_profile(person_id)
    voiceprint_path = Path(profile["embedding_path"] or "")
    if not voiceprint_path.is_file():
        raise RuntimeError(f"人物 {person_id} 没有可用声纹文件")
    payload = np.load(voiceprint_path)
    metadata = json.loads(str(payload["metadata_json"]))
    count = 0
    for source in metadata.get("source_files", []):
        source_path = Path(source["path"])
        source_hash = source.get("sha256") or (
            sha256_file(source_path) if source_path.is_file() else None
        )
        session_key = _source_session_key(source_path)
        database.upsert_voice_library_sample(
            {
                "sample_key": f"enrollment:{person_id}:{source_hash or source_path.name}",
                "person_id": person_id,
                "identity_label": profile["display_name"],
                "sample_type": "enrollment_source",
                "split": "accepted",
                "source_path": str(source_path.resolve()),
                "source_sha256": source_hash,
                "session_key": session_key,
                "duration_ms": round(float(source.get("duration_seconds", 0)) * 1000),
                "speech_ms": round(float(source.get("speech_seconds", 0)) * 1000),
                "embedding_model": profile["embedding_model"],
                "embedding_version": profile["embedding_version"],
                "human_confirmed": True,
                "metadata": {"chunks_used": source.get("chunks", 0)},
            }
        )
        count += 1
    return count


def accumulate_reviewed_samples(
    database: Database,
    recording_id: int,
    *,
    split: str = "auto",
    device: str = "auto",
    max_session_seconds: float = 60.0,
) -> AccumulationSummary:
    if split not in {"auto", "accepted", "holdout"}:
        raise ValueError("split 必须是 auto、accepted 或 holdout")
    recording = database.get_recording(recording_id)
    annotations = database.list_segment_annotations(recording_id)
    if not annotations:
        raise RuntimeError("当前录音没有已导入的人工身份标注")
    self_profile = database.get_self_profile()
    if self_profile is None:
        raise RuntimeError("尚未登记本人声纹")
    actual_person_split = _automatic_session_split(recording_id) if split == "auto" else split
    source_path = Path(recording["source_path"])
    normalized_path = Path(recording["normalized_path"] or "")
    if not source_path.is_file() or not normalized_path.is_file():
        raise RuntimeError("录音源文件或标准化音频不存在")

    score_path = recording_output_dir(recording_id) / "self-candidates" / "scores.json"
    scores: dict[int, float] = {}
    if score_path.is_file():
        score_payload = json.loads(score_path.read_text(encoding="utf-8"))
        scores = {
            int(item["segment_id"]): float(item["score_min"])
            for item in score_payload.get("segments", [])
        }

    prepared: list[dict] = []
    accepted_person_ms = 0
    session_key = f"recording:{recording_id}"
    for annotation in annotations:
        segment = database.get_segment(int(annotation["segment_id"]))
        duration_ms = int(segment["end_ms"]) - int(segment["start_ms"])
        label = annotation["identity_label"]
        is_self = label == "self"
        sample_split = actual_person_split if is_self else "negative"
        if (
            is_self
            and sample_split == "accepted"
            and accepted_person_ms + duration_ms > round(max_session_seconds * 1000)
        ):
            sample_split = "holdout"
        if is_self and sample_split == "accepted":
            accepted_person_ms += duration_ms

        if is_self:
            person_id = int(self_profile["id"])
            identity_label = self_profile["display_name"]
            category_root = _person_root(person_id) / sample_split
            sample_type = "confirmed_conversation"
        else:
            person_id = None
            identity_label = label
            category_root = LIBRARY_ROOT / "negatives" / label
            sample_type = label if label in {"mixed", "uncertain"} else "hard_negative"
        destination = category_root / f"recording-{recording_id:06d}-segment-{segment['id']}.wav"
        extract_clip(
            source_path,
            destination,
            int(segment["start_ms"]),
            int(segment["end_ms"]),
        )
        prepared.append(
            {
                "annotation": annotation,
                "segment": segment,
                "duration_ms": duration_ms,
                "person_id": person_id,
                "identity_label": identity_label,
                "sample_type": sample_type,
                "split": sample_split,
                "destination": destination,
                "session_key": session_key,
                "score": scores.get(int(segment["id"])),
            }
        )

    embedding_candidates = [
        item for item in prepared if 800 <= item["duration_ms"] <= 8_000
    ]
    if embedding_candidates:
        waveforms = []
        for item in embedding_candidates:
            samples, sample_rate = sf.read(
                item["destination"], dtype="float32", always_2d=False
            )
            if sample_rate != 16_000 or samples.ndim != 1:
                raise RuntimeError(f"样本格式异常：{item['destination']}")
            waveforms.append(normalize_speech_level(samples))
        backend = FunASRBackend(device=device)
        embeddings = l2_normalize(backend.extract_speaker_embeddings(waveforms))
        for item, embedding in zip(embedding_candidates, embeddings, strict=True):
            item["embedding"] = embedding

    for item in prepared:
        embedding = item.get("embedding")
        segment = item["segment"]
        annotation = item["annotation"]
        database.upsert_voice_library_sample(
            {
                "sample_key": (
                    f"review:{recording_id}:{segment['id']}:{annotation['identity_label']}"
                ),
                "person_id": item["person_id"],
                "identity_label": item["identity_label"],
                "sample_type": item["sample_type"],
                "split": item["split"],
                "source_path": str(source_path.resolve()),
                "stored_path": str(item["destination"].resolve()),
                "source_sha256": sha256_file(item["destination"]),
                "recording_id": recording_id,
                "segment_id": int(segment["id"]),
                "session_key": item["session_key"],
                "duration_ms": item["duration_ms"],
                "speech_ms": item["duration_ms"],
                "embedding_blob": (
                    np.asarray(embedding, dtype="<f4").tobytes()
                    if embedding is not None
                    else None
                ),
                "embedding_dim": int(len(embedding)) if embedding is not None else None,
                "embedding_model": SPEAKER_MODEL_ID if embedding is not None else None,
                "embedding_version": (
                    importlib.metadata.version("funasr") if embedding is not None else None
                ),
                "match_score": item["score"],
                "human_confirmed": True,
                "metadata": {
                    "raw_label": annotation["raw_label"],
                    "confidence": annotation["confidence"],
                    "note": annotation["note"],
                    "text": segment["text_display"],
                },
            }
        )

    added = rebuild_person_voiceprint(database, int(self_profile["id"]))
    manifest_path = write_library_manifests(database)
    return AccumulationSummary(
        recording_id=recording_id,
        requested_split=split,
        person_samples=sum(item["person_id"] is not None for item in prepared),
        negative_samples=sum(item["sample_type"] == "hard_negative" for item in prepared),
        mixed_samples=sum(item["sample_type"] == "mixed" for item in prepared),
        uncertain_samples=sum(item["sample_type"] == "uncertain" for item in prepared),
        embedded_samples=sum("embedding" in item for item in prepared),
        profile_embeddings_added=added,
        manifest_path=manifest_path,
    )


def rebuild_person_voiceprint(
    database: Database, person_id: int, *, max_embeddings: int = 100
) -> int:
    profile = database.get_person_profile(person_id)
    voiceprint_path = Path(profile["embedding_path"] or "")
    if not voiceprint_path.is_file():
        return 0
    payload = np.load(voiceprint_path)
    embeddings = l2_normalize(payload["embeddings"])
    metadata = json.loads(str(payload["metadata_json"]))
    already_added = set(metadata.get("library_sample_keys_added", []))
    additions: list[np.ndarray] = []
    addition_keys: list[str] = []
    for row in database.list_voice_library_samples(person_id=person_id):
        if (
            row["split"] != "accepted"
            or row["sample_type"] != "confirmed_conversation"
            or row["embedding_blob"] is None
            or not 2_000 <= int(row["duration_ms"]) <= 8_000
            or row["sample_key"] in already_added
        ):
            continue
        embedding = np.frombuffer(row["embedding_blob"], dtype="<f4").copy()
        if len(embedding) != int(row["embedding_dim"]):
            continue
        additions.append(embedding)
        addition_keys.append(row["sample_key"])
    if not additions:
        return 0
    combined = l2_normalize(np.vstack([embeddings, *additions]))
    if len(combined) > max_embeddings:
        combined = _select_representative_embeddings(combined, max_embeddings)
    centroid = l2_normalize(combined.mean(axis=0, keepdims=True))[0]
    metadata["library_sample_keys_added"] = sorted(already_added | set(addition_keys))
    metadata["accepted_embeddings"] = len(combined)
    metadata["last_library_update"] = datetime.now(timezone.utc).isoformat()
    temporary = voiceprint_path.with_suffix(".update.npz")
    np.savez_compressed(
        temporary,
        embeddings=combined.astype(np.float32),
        centroid=centroid.astype(np.float32),
        metadata_json=np.asarray(json.dumps(metadata, ensure_ascii=False)),
    )
    temporary.replace(voiceprint_path)
    return len(additions)


def get_library_status(database: Database) -> list[PersonLibraryStatus]:
    calibration_by_person: dict[int, float] = {}
    self_profile = database.get_self_profile()
    if self_profile is not None:
        calibration_paths = sorted(
            OUTPUT_DIR.glob("recording-*/self-candidates/calibration.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if calibration_paths:
            calibration_by_person[int(self_profile["id"])] = float(
                json.loads(calibration_paths[0].read_text(encoding="utf-8"))["threshold"]
            )
    statuses: list[PersonLibraryStatus] = []
    for profile in database.list_person_profiles():
        person_id = int(profile["id"])
        rows = database.list_voice_library_samples(person_id=person_id)
        enrollment = [row for row in rows if row["sample_type"] == "enrollment_source"]
        accepted_conversation = [
            row
            for row in rows
            if row["sample_type"] == "confirmed_conversation" and row["split"] == "accepted"
        ]
        holdout = [row for row in rows if row["split"] == "holdout"]
        effective_ms = sum(int(row["speech_ms"]) for row in enrollment + accepted_conversation)
        sessions = {row["session_key"] for row in enrollment + accepted_conversation}
        active_embeddings = 0
        voiceprint_path = Path(profile["embedding_path"] or "")
        if voiceprint_path.is_file():
            active_embeddings = int(np.load(voiceprint_path)["embeddings"].shape[0])
        if effective_ms >= 300_000 and len(sessions) >= 5 and active_embeddings >= 30:
            status = "stable"
        elif effective_ms >= 120_000 and active_embeddings >= 10:
            status = "active"
        else:
            status = "provisional"
        statuses.append(
            PersonLibraryStatus(
                person_id=person_id,
                display_name=profile["display_name"],
                profile_type=profile["profile_type"],
                status=status,
                effective_speech_seconds=effective_ms / 1000,
                enrollment_sources=len(enrollment),
                accepted_conversation_samples=len(accepted_conversation),
                holdout_samples=len(holdout),
                active_embeddings=active_embeddings,
                accepted_sessions=len(sessions),
                threshold=calibration_by_person.get(person_id),
                manifest_path=_person_root(person_id) / "manifest.json",
            )
        )
    return statuses


def write_library_manifests(database: Database) -> Path:
    LIBRARY_ROOT.mkdir(parents=True, exist_ok=True)
    statuses = get_library_status(database)
    for status in statuses:
        person_root = _person_root(status.person_id)
        person_root.mkdir(parents=True, exist_ok=True)
        samples = database.list_voice_library_samples(person_id=status.person_id)
        payload = {
            "format": "AllDayRecording voice library manifest v1",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "status": {**asdict(status), "manifest_path": str(status.manifest_path.resolve())},
            "samples": [
                {
                    "sample_key": row["sample_key"],
                    "sample_type": row["sample_type"],
                    "split": row["split"],
                    "session_key": row["session_key"],
                    "duration_ms": row["duration_ms"],
                    "speech_ms": row["speech_ms"],
                    "source_path": row["source_path"],
                    "stored_path": row["stored_path"],
                    "segment_id": row["segment_id"],
                    "match_score": row["match_score"],
                    "has_embedding": row["embedding_blob"] is not None,
                }
                for row in samples
            ],
        }
        status.manifest_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    all_samples = database.list_voice_library_samples()
    negative_counts: dict[str, int] = {}
    for row in all_samples:
        if row["split"] == "negative":
            negative_counts[row["identity_label"]] = negative_counts.get(row["identity_label"], 0) + 1
    lines = ["# 本地人物声纹库", ""]
    for status in statuses:
        relative = status.manifest_path.relative_to(LIBRARY_ROOT).as_posix()
        lines.extend(
            [
                f"## {status.display_name}（person-{status.person_id:06d}）",
                "",
                f"- 状态：{status.status}",
                f"- 有效语音：{status.effective_speech_seconds:.1f}s",
                f"- 活跃 embedding：{status.active_embeddings}",
                f"- 独立会话：{status.accepted_sessions}",
                f"- 留出样本：{status.holdout_samples}",
                f"- [manifest.json]({relative})",
                "",
            ]
        )
    lines.extend(["## 负样本", ""])
    for label, count in sorted(negative_counts.items()):
        lines.append(f"- {label}: {count}")
    lines.append("")
    manifest = LIBRARY_ROOT / "README.md"
    manifest.write_text("\n".join(lines), encoding="utf-8")
    return manifest


def _person_root(person_id: int) -> Path:
    return LIBRARY_ROOT / f"person-{person_id:06d}"


def _source_session_key(path: Path) -> str:
    try:
        day = datetime.fromtimestamp(path.stat().st_mtime).date().isoformat()
    except OSError:
        day = "unknown-date"
    return f"enrollment:{day}"


def _automatic_session_split(recording_id: int) -> str:
    return "holdout" if recording_id % 5 == 0 else "accepted"


def _select_representative_embeddings(
    embeddings: np.ndarray, count: int
) -> np.ndarray:
    normalized = l2_normalize(embeddings)
    centroid = l2_normalize(normalized.mean(axis=0, keepdims=True))[0]
    selected = [int(np.argmax(normalized @ centroid))]
    best_similarity = normalized @ normalized[selected[0]]
    while len(selected) < count:
        candidate = int(np.argmin(best_similarity))
        selected.append(candidate)
        best_similarity = np.maximum(best_similarity, normalized @ normalized[candidate])
    return normalized[selected]
