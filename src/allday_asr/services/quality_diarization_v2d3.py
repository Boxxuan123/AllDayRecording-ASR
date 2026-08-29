from __future__ import annotations

import hashlib
import json
import math
import os
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

from allday_asr.audio.tools import sha256_file
from allday_asr.diarization.quality_backends import (
    PYANNOTE_COMMUNITY_MODEL_ID,
    PyannoteCommunityBackend,
)
from allday_asr.paths import OUTPUT_DIR
from allday_asr.services.enrollment import l2_normalize, normalize_speech_level
from allday_asr.storage.database import Database

EMBEDDING_WINDOW_MS = 3_000
EMBEDDING_SAMPLE_RATE = 16_000
EMBEDDING_WINDOW_SAMPLES = EMBEDDING_SAMPLE_RATE * EMBEDDING_WINDOW_MS // 1_000
IGNORED_NEGATIVE_IDENTITIES = {"unknown", "uncertain", "mixed"}


@dataclass(frozen=True)
class V2D3Settings:
    min_candidate_ms: int = 1_200
    max_candidate_ms: int = 3_000
    merge_gap_ms: int = 350
    truth_guard_ms: int = 500
    diversity_gap_ms: int = 2_000
    max_candidates: int = 12
    embedding_batch_size: int = 32

    def validate(self) -> None:
        if self.min_candidate_ms < 400:
            raise ValueError("min_candidate_ms 不能小于 400")
        if self.max_candidate_ms < self.min_candidate_ms:
            raise ValueError("max_candidate_ms 不能小于 min_candidate_ms")
        if self.max_candidate_ms > EMBEDDING_WINDOW_MS:
            raise ValueError("max_candidate_ms 不能超过 3 秒 embedding 窗口")
        if self.merge_gap_ms < 0 or self.truth_guard_ms < 0:
            raise ValueError("时间间隔参数不能为负数")
        if self.diversity_gap_ms < 0:
            raise ValueError("diversity_gap_ms 不能为负数")
        if self.max_candidates <= 0 or self.embedding_batch_size <= 0:
            raise ValueError("候选数和 batch size 必须大于 0")


@dataclass(frozen=True)
class V2D3Summary:
    run_id: int
    diarization_run_id: int
    truth_set_id: int
    target_identity: str
    seed_quality: str
    target_truth_ms: int
    target_embedding_count: int
    negative_identities: list[str]
    scored_candidate_windows: int
    selected_candidates: int
    manifest_path: Path


def review_identity_candidate(
    database: Database,
    run_id: int,
    *,
    candidate_id: str,
    status: str,
    note: str | None = None,
) -> dict[str, Any]:
    """Persist a human review overlay without changing the immutable V2-D.3 run."""
    run = database.get_processing_run(run_id)
    if str(run["run_kind"]) != "quality_diarization_v2d3":
        raise ValueError("身份候选审核只适用于 V2-D.3 run")
    if str(run["status"]) != "completed":
        raise ValueError("只能审核已完成的 V2-D.3 run")
    summary = _json_object(run["summary_json"])
    candidate = next(
        (
            item
            for item in summary.get("candidates") or []
            if str(item.get("id")) == candidate_id
        ),
        None,
    )
    if candidate is None:
        raise ValueError("V2-D.3 run 中不存在这个候选")
    row = database.upsert_identity_candidate_review(
        run_id,
        {
            "candidate_id": candidate_id,
            "target_identity": str(summary["target_identity"]),
            "session_start_ms": int(candidate["start_ms"]),
            "session_end_ms": int(candidate["end_ms"]),
            "status": status,
            "note": note.strip() if note and note.strip() else None,
        },
    )
    return {
        "run_id": run_id,
        "candidate_id": candidate_id,
        "target_identity": str(row["target_identity"]),
        "start_ms": int(row["session_start_ms"]),
        "end_ms": int(row["session_end_ms"]),
        "status": str(row["status"]),
        "note": row["note"],
        "updated_at": row["updated_at"],
    }


def carry_forward_identity_candidate_reviews(
    database: Database, run_id: int
) -> int:
    """Carry human interval decisions to an equivalent newer candidate run."""
    run = database.get_processing_run(run_id)
    if str(run["run_kind"]) != "quality_diarization_v2d3":
        raise ValueError("审核继承只适用于 V2-D.3 run")
    if str(run["status"]) != "completed":
        raise ValueError("审核继承需要已完成的 V2-D.3 run")
    summary = _json_object(run["summary_json"])
    current_candidates = {
        str(item["id"]): item for item in summary.get("candidates") or []
    }
    existing = {
        str(row["candidate_id"])
        for row in database.list_identity_candidate_reviews(run_id)
    }
    copied = 0
    previous_runs = reversed(
        [
            row
            for row in database.list_processing_runs(int(run["recording_id"]))
            if int(row["id"]) < run_id
            and str(row["run_kind"]) == "quality_diarization_v2d3"
            and str(row["status"]) == "completed"
            and int(row["parent_run_id"] or 0) == int(run["parent_run_id"] or 0)
            and str(row["input_fingerprint"]) == str(run["input_fingerprint"])
        ]
    )
    for previous_run in previous_runs:
        previous_summary = _json_object(previous_run["summary_json"])
        if (
            str(previous_summary.get("target_identity"))
            != str(summary.get("target_identity"))
            or int(previous_summary.get("truth_set_id") or 0)
            != int(summary.get("truth_set_id") or 0)
        ):
            continue
        previous_candidates = {
            str(item["id"]): item
            for item in previous_summary.get("candidates") or []
        }
        for review in database.list_identity_candidate_reviews(
            int(previous_run["id"])
        ):
            candidate_id = str(review["candidate_id"])
            if candidate_id in existing or candidate_id not in current_candidates:
                continue
            current = current_candidates[candidate_id]
            previous = previous_candidates.get(candidate_id)
            if previous is None or (
                int(previous["start_ms"]), int(previous["end_ms"])
            ) != (int(current["start_ms"]), int(current["end_ms"])):
                continue
            database.upsert_identity_candidate_review(
                run_id,
                {
                    "candidate_id": candidate_id,
                    "target_identity": str(summary["target_identity"]),
                    "session_start_ms": int(current["start_ms"]),
                    "session_end_ms": int(current["end_ms"]),
                    "status": str(review["status"]),
                    "note": review["note"],
                },
            )
            existing.add(candidate_id)
            copied += 1
    return copied


def run_identity_candidate_mining(
    database: Database,
    recording_id: int,
    *,
    diarization_run_id: int,
    truth_set_id: int,
    target_identity: str,
    settings: V2D3Settings | None = None,
    device: str = "auto",
    model_path: Path | None = None,
    embedding_backend: Any | None = None,
) -> V2D3Summary:
    """Rank identity expansion candidates without assigning any identity."""
    settings = settings or V2D3Settings()
    settings.validate()
    target_identity = target_identity.strip()
    if not target_identity:
        raise ValueError("目标身份不能为空")

    diarization_run = database.get_processing_run(diarization_run_id)
    if int(diarization_run["recording_id"]) != recording_id:
        raise ValueError("V2-D run does not belong to the selected recording")
    if str(diarization_run["run_kind"]) != "quality_diarization_v2d":
        raise ValueError("V2-D.3 requires a V2-D diarization parent run")
    if str(diarization_run["status"]) != "completed":
        raise ValueError("V2-D.3 requires a completed V2-D parent run")

    truth_set = database.get_truth_set(truth_set_id)
    if str(truth_set["status"]) != "frozen":
        raise ValueError("V2-D.3 requires a frozen truth set")
    if int(truth_set["session_id"]) != int(diarization_run["session_id"]):
        raise ValueError("identity truth belongs to a different recording session")
    annotations = [
        row
        for row in database.list_truth_annotations(
            truth_set_id, annotation_kind="speaker"
        )
        if str(row["label"] or "").strip()
    ]
    grouped_annotations: dict[str, list[Any]] = defaultdict(list)
    for row in annotations:
        grouped_annotations[str(row["label"]).strip()].append(row)
    if target_identity not in grouped_annotations:
        raise ValueError(f"真值集没有目标身份：{target_identity}")
    negative_identities = sorted(
        identity
        for identity in grouped_annotations
        if identity != target_identity
        and identity.lower() not in IGNORED_NEGATIVE_IDENTITIES
    )
    if not negative_identities:
        raise ValueError("至少需要一个已标注的负对照身份")

    recording = database.get_recording(recording_id)
    normalized_path = Path(str(recording["normalized_path"] or ""))
    if not normalized_path.is_file():
        raise RuntimeError("V2-D.3 需要现有的 16 kHz 单声道派生音频")
    normalized_sha256 = sha256_file(normalized_path)

    backend = embedding_backend or PyannoteCommunityBackend(
        device=device, model_path=model_path
    )
    owns_backend = embedding_backend is None
    backend.ensure_loaded()
    model_revision = getattr(backend, "model_revision", None)
    backend_name = str(
        getattr(backend, "backend_name", "pyannote-audio-community-1")
    )
    model_id = str(getattr(backend, "model_id", PYANNOTE_COMMUNITY_MODEL_ID))
    model_parameters = (
        backend.parameters() if callable(getattr(backend, "parameters", None)) else {}
    )
    config = {
        "diarization_run_id": diarization_run_id,
        "truth_set_id": truth_set_id,
        "truth_sha256": str(truth_set["truth_sha256"]),
        "target_identity": target_identity,
        "negative_identities": negative_identities,
        "embedding_window_ms": EMBEDDING_WINDOW_MS,
        "derived_audio_sha256": normalized_sha256,
        "derived_audio_format": "pcm-float32-read-from-16khz-mono-wav",
        "min_candidate_ms": settings.min_candidate_ms,
        "max_candidate_ms": settings.max_candidate_ms,
        "merge_gap_ms": settings.merge_gap_ms,
        "truth_guard_ms": settings.truth_guard_ms,
        "diversity_gap_ms": settings.diversity_gap_ms,
        "max_candidates": settings.max_candidates,
        "decision_policy": "rank-for-review-only; never auto-assign identity",
    }
    run_id = database.start_processing_run(
        recording_id,
        run_kind="quality_diarization_v2d3",
        config=config,
        config_sha256=_sha256_mapping(config),
        model_manifest={
            "backend": backend_name,
            "model_id": model_id,
            "model_revision": model_revision,
            "embedding_model": "Community-1 WeSpeakerResNet34",
            "parameters": model_parameters,
            "biometric_embeddings_persisted": False,
        },
        pipeline_version="v2-d.3",
        parent_run_id=diarization_run_id,
    )
    try:
        with sf.SoundFile(normalized_path) as audio:
            if audio.samplerate != EMBEDDING_SAMPLE_RATE or audio.channels != 1:
                raise RuntimeError("V2-D.3 派生音频必须是 16 kHz 单声道")
            seed_waveforms: list[np.ndarray] = []
            seed_layout: list[tuple[str, int]] = []
            seed_stats: dict[str, dict[str, Any]] = {}
            for identity in [target_identity, *negative_identities]:
                identity_audio = [
                    _read_audio_range(
                        audio,
                        int(row["session_start_ms"]),
                        int(row["session_end_ms"]),
                    )
                    for row in grouped_annotations[identity]
                ]
                waveforms = pack_seed_waveforms(identity_audio)
                if not waveforms:
                    continue
                seed_layout.append((identity, len(waveforms)))
                seed_waveforms.extend(waveforms)
                truth_ms = sum(
                    int(row["session_end_ms"]) - int(row["session_start_ms"])
                    for row in grouped_annotations[identity]
                )
                seed_stats[identity] = {
                    "truth_ms": truth_ms,
                    "annotation_regions": len(grouped_annotations[identity]),
                    "embedding_count": len(waveforms),
                }
            if target_identity not in seed_stats:
                raise RuntimeError("目标身份的真值音频过短，无法生成弱种子")
            missing_negatives = [
                value for value in negative_identities if value not in seed_stats
            ]
            if missing_negatives:
                raise RuntimeError(
                    "负对照真值音频过短：" + ", ".join(missing_negatives)
                )

            seed_embeddings = l2_normalize(
                backend.extract_speaker_embeddings(
                    seed_waveforms, batch_size=settings.embedding_batch_size
                )
            )
            centroids: dict[str, np.ndarray] = {}
            cursor = 0
            for identity, count in seed_layout:
                values = seed_embeddings[cursor : cursor + count]
                cursor += count
                centroid = l2_normalize(values.mean(axis=0, keepdims=True))[0]
                centroids[identity] = centroid
                seed_stats[identity]["within_identity_median_similarity"] = round(
                    float(np.median(values @ centroid)), 6
                )

            raw_candidates = build_candidate_windows(
                database.list_diarization_turns(
                    diarization_run_id, turn_kind="exclusive"
                ),
                annotations,
                settings=settings,
            )
            candidate_embeddings: list[np.ndarray] = []
            for offset in range(0, len(raw_candidates), settings.embedding_batch_size):
                batch_rows = raw_candidates[
                    offset : offset + settings.embedding_batch_size
                ]
                waveforms = [
                    _fit_embedding_window(
                        _read_audio_range(
                            audio, int(item["start_ms"]), int(item["end_ms"])
                        )
                    )
                    for item in batch_rows
                ]
                candidate_embeddings.append(
                    backend.extract_speaker_embeddings(
                        waveforms, batch_size=settings.embedding_batch_size
                    )
                )

        stacked_candidates = (
            l2_normalize(np.vstack(candidate_embeddings))
            if candidate_embeddings
            else np.empty((0, seed_embeddings.shape[1]), dtype=np.float32)
        )
        scored = score_identity_candidates(
            raw_candidates,
            stacked_candidates,
            centroids,
            target_identity=target_identity,
        )
        selected = select_diverse_candidates(
            scored,
            max_candidates=settings.max_candidates,
            diversity_gap_ms=settings.diversity_gap_ms,
        )
        tokens = _committed_tokens(database, diarization_run)
        for rank, item in enumerate(selected, start=1):
            item["rank"] = rank
            item["title"] = f"{_identity_display(target_identity)}弱种子候选 #{rank:02d}"
            item["preview"] = _token_preview(
                tokens, int(item["start_ms"]), int(item["end_ms"])
            )
            item["token_count"] = sum(
                int(row["end_ms"]) > int(item["start_ms"])
                and int(row["start_ms"]) < int(item["end_ms"])
                for row in tokens
            )

        target_seed = seed_stats[target_identity]
        enrollment_ready = bool(
            int(target_seed["truth_ms"]) >= 30_000
            and int(target_seed["embedding_count"]) >= 6
        )
        seed_quality = "enrollment_ready" if enrollment_ready else "weak"
        summary_payload = {
            "diarization_run_id": diarization_run_id,
            "truth_set_id": truth_set_id,
            "target_identity": target_identity,
            "seed_quality": seed_quality,
            "enrollment_ready": enrollment_ready,
            "target_truth_ms": int(target_seed["truth_ms"]),
            "target_embedding_count": int(target_seed["embedding_count"]),
            "negative_identities": negative_identities,
            "seed_stats": seed_stats,
            "scored_candidate_windows": len(scored),
            "selected_candidates": len(selected),
            "candidates": selected,
            "identity_policy": (
                "uncalibrated contrastive ranking only; human review is required; "
                "no identity or voiceprint is written"
            ),
        }
        manifest_path = _write_manifest(
            run_id,
            int(diarization_run["session_id"]),
            config,
            summary_payload,
            model={
                "backend": backend_name,
                "model_id": model_id,
                "model_revision": model_revision,
                "embedding_model": "Community-1 WeSpeakerResNet34",
            },
        )
        database.finish_processing_run(
            run_id,
            status="completed",
            summary=summary_payload,
            artifacts={"manifest": str(manifest_path.resolve())},
        )
        carry_forward_identity_candidate_reviews(database, run_id)
        return V2D3Summary(
            run_id=run_id,
            manifest_path=manifest_path,
            **{
                key: summary_payload[key]
                for key in (
                    "diarization_run_id",
                    "truth_set_id",
                    "target_identity",
                    "seed_quality",
                    "target_truth_ms",
                    "target_embedding_count",
                    "negative_identities",
                    "scored_candidate_windows",
                    "selected_candidates",
                )
            },
        )
    except Exception as exc:
        database.finish_processing_run(run_id, status="failed", error=repr(exc))
        raise
    finally:
        if owns_backend:
            backend.close()


def pack_seed_waveforms(parts: Sequence[np.ndarray]) -> list[np.ndarray]:
    """Concatenate only truth-covered audio, then form equal 3 s embedding inputs."""
    usable = [np.asarray(item, dtype=np.float32) for item in parts if len(item)]
    if not usable:
        return []
    speech = np.concatenate(usable)
    minimum_samples = round(1.0 * EMBEDDING_SAMPLE_RATE)
    if len(speech) < minimum_samples:
        return []
    chunk_count = max(1, math.ceil(len(speech) / EMBEDDING_WINDOW_SAMPLES))
    while chunk_count > 1 and len(speech) / chunk_count < minimum_samples:
        chunk_count -= 1
    return [
        normalize_speech_level(_fit_embedding_window(chunk))
        for chunk in np.array_split(speech, chunk_count)
    ]


def build_candidate_windows(
    exclusive_turns: Sequence[Any],
    truth_annotations: Sequence[Any],
    *,
    settings: V2D3Settings,
) -> list[dict[str, Any]]:
    """Merge short same-speaker turns, split long turns, and exclude seed leakage."""
    ordered = sorted(
        exclusive_turns,
        key=lambda row: (
            int(row["session_start_ms"]),
            int(row["session_end_ms"]),
            str(row["speaker_label"]),
        ),
    )
    merged: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for row in ordered:
        start_ms = int(row["session_start_ms"])
        end_ms = int(row["session_end_ms"])
        speaker = str(row["speaker_label"])
        turn_id = int(row["id"])
        if (
            current is not None
            and current["speaker"] == speaker
            and start_ms - int(current["end_ms"]) <= settings.merge_gap_ms
            and end_ms - int(current["start_ms"]) <= settings.max_candidate_ms
        ):
            current["end_ms"] = max(int(current["end_ms"]), end_ms)
            current["source_turn_ids"].append(turn_id)
            continue
        if current is not None:
            merged.append(current)
        current = {
            "start_ms": start_ms,
            "end_ms": end_ms,
            "speaker": speaker,
            "source_turn_ids": [turn_id],
        }
    if current is not None:
        merged.append(current)

    truth_ranges = [
        (
            int(row["session_start_ms"]) - settings.truth_guard_ms,
            int(row["session_end_ms"]) + settings.truth_guard_ms,
        )
        for row in truth_annotations
    ]
    output: list[dict[str, Any]] = []
    seen: set[tuple[int, int, str]] = set()
    for row in merged:
        for start_ms, end_ms in _split_range(
            int(row["start_ms"]),
            int(row["end_ms"]),
            min_ms=settings.min_candidate_ms,
            max_ms=settings.max_candidate_ms,
        ):
            if any(end_ms > truth_start and start_ms < truth_end for truth_start, truth_end in truth_ranges):
                continue
            key = (start_ms, end_ms, str(row["speaker"]))
            if key in seen:
                continue
            seen.add(key)
            output.append(
                {
                    "id": f"identity-expansion-{start_ms}-{end_ms}-{row['speaker']}",
                    "kind": "identity_expansion",
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "speaker": str(row["speaker"]),
                    "speakers": [str(row["speaker"])],
                    "speaker_count": 1,
                    "speech_ms": end_ms - start_ms,
                    "speaker_switches": 0,
                    "overlap_ms": 0,
                    "unassigned_tokens": 0,
                    "identity_labels": [],
                    "identity_truth_ms": 0,
                    "source_turn_ids": list(row["source_turn_ids"]),
                }
            )
    return output


def score_identity_candidates(
    candidates: Sequence[dict[str, Any]],
    embeddings: np.ndarray,
    centroids: dict[str, np.ndarray],
    *,
    target_identity: str,
) -> list[dict[str, Any]]:
    if len(candidates) != len(embeddings):
        raise ValueError("候选窗口与 embedding 数量不一致")
    target_centroid = centroids[target_identity]
    negative_centroids = {
        identity: value
        for identity, value in centroids.items()
        if identity != target_identity
    }
    output: list[dict[str, Any]] = []
    for candidate, embedding in zip(candidates, embeddings, strict=True):
        target_similarity = float(embedding @ target_centroid)
        negative_scores = {
            identity: float(embedding @ centroid)
            for identity, centroid in negative_centroids.items()
        }
        negative_identity, negative_similarity = max(
            negative_scores.items(), key=lambda item: (item[1], item[0])
        )
        margin = target_similarity - negative_similarity
        if target_similarity >= 0.40 and margin >= 0.08:
            tier = "high_contrast"
        elif target_similarity >= 0.30 and margin >= 0.03:
            tier = "medium_contrast"
        else:
            tier = "exploratory"
        output.append(
            {
                **candidate,
                "target_identity": target_identity,
                "target_similarity": round(target_similarity, 6),
                "negative_identity": negative_identity,
                "negative_similarity": round(negative_similarity, 6),
                "contrastive_margin": round(margin, 6),
                "score": round(margin, 3),
                "review_tier": tier,
                "auto_assigned": False,
            }
        )
    tier_priority = {"high_contrast": 0, "medium_contrast": 1, "exploratory": 2}
    return sorted(
        output,
        key=lambda item: (
            tier_priority[str(item["review_tier"])],
            -float(item["contrastive_margin"]),
            -float(item["target_similarity"]),
            int(item["start_ms"]),
        ),
    )


def select_diverse_candidates(
    candidates: Sequence[dict[str, Any]],
    *,
    max_candidates: int,
    diversity_gap_ms: int,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for candidate in candidates:
        midpoint = (int(candidate["start_ms"]) + int(candidate["end_ms"])) // 2
        if any(
            abs(
                midpoint
                - (int(item["start_ms"]) + int(item["end_ms"])) // 2
            )
            < diversity_gap_ms
            for item in selected
        ):
            continue
        selected.append(dict(candidate))
        if len(selected) >= max_candidates:
            break
    return selected


def _split_range(
    start_ms: int, end_ms: int, *, min_ms: int, max_ms: int
) -> list[tuple[int, int]]:
    duration_ms = end_ms - start_ms
    if duration_ms < min_ms:
        return []
    if duration_ms <= max_ms:
        return [(start_ms, end_ms)]
    output: list[tuple[int, int]] = []
    cursor = start_ms
    while cursor < end_ms:
        chunk_end = min(end_ms, cursor + max_ms)
        if chunk_end - cursor < min_ms:
            cursor = max(start_ms, end_ms - max_ms)
            chunk_end = end_ms
        if not output or output[-1] != (cursor, chunk_end):
            output.append((cursor, chunk_end))
        if chunk_end >= end_ms:
            break
        cursor = chunk_end
    return output


def _read_audio_range(
    audio: sf.SoundFile, start_ms: int, end_ms: int
) -> np.ndarray:
    start_frame = round(start_ms * audio.samplerate / 1_000)
    frame_count = round((end_ms - start_ms) * audio.samplerate / 1_000)
    audio.seek(start_frame)
    return np.asarray(
        audio.read(frame_count, dtype="float32", always_2d=False),
        dtype=np.float32,
    )


def _fit_embedding_window(samples: np.ndarray) -> np.ndarray:
    value = np.asarray(samples, dtype=np.float32)
    if not len(value):
        return np.zeros(EMBEDDING_WINDOW_SAMPLES, dtype=np.float32)
    if len(value) >= EMBEDDING_WINDOW_SAMPLES:
        offset = (len(value) - EMBEDDING_WINDOW_SAMPLES) // 2
        return normalize_speech_level(
            value[offset : offset + EMBEDDING_WINDOW_SAMPLES]
        )
    repeats = math.ceil(EMBEDDING_WINDOW_SAMPLES / len(value))
    tiled = np.tile(value, repeats)[:EMBEDDING_WINDOW_SAMPLES]
    return normalize_speech_level(tiled)


def _committed_tokens(database: Database, diarization_run) -> list[dict[str, Any]]:
    summary = _json_object(diarization_run["summary_json"])
    asr_run_id = int(summary.get("asr_run_id") or diarization_run["parent_run_id"] or 0)
    if not asr_run_id:
        return []
    return [
        {
            "text": str(row["text"]),
            "start_ms": int(row["session_start_ms"]),
            "end_ms": int(row["session_end_ms"]),
        }
        for row in database.list_committed_asr_tokens(asr_run_id)
    ]


def _token_preview(
    tokens: Sequence[dict[str, Any]], start_ms: int, end_ms: int
) -> str:
    text = "".join(
        str(row["text"])
        for row in tokens
        if int(row["end_ms"]) > start_ms and int(row["start_ms"]) < end_ms
    ).strip()
    return text[:80] + ("…" if len(text) > 80 else "")


def _identity_display(identity: str) -> str:
    return {
        "father": "父亲",
        "mother": "母亲",
        "self": "本人",
        "me": "本人",
        "tv": "电视",
    }.get(identity, identity)


def _write_manifest(
    run_id: int,
    session_id: int,
    config: dict[str, Any],
    summary: dict[str, Any],
    *,
    model: dict[str, Any],
) -> Path:
    directory = OUTPUT_DIR / f"session-{session_id:06d}" / "diarization-v2d3"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"run-{run_id:06d}.json"
    payload = {
        "format": "AllDayRecording V2-D.3 weak-seed identity candidate mining v1",
        "run_id": run_id,
        "session_id": session_id,
        "config": config,
        "model": model,
        "summary": summary,
        "safety": {
            "original_audio_immutable": True,
            "derived_audio_read_only": True,
            "biometric_embeddings_persisted": False,
            "identity_assignments_written": False,
        },
    }
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return path


def _sha256_mapping(value: dict[str, Any]) -> str:
    canonical = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _json_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    parsed = json.loads(value)
    return parsed if isinstance(parsed, dict) else {}
