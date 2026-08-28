from __future__ import annotations

import importlib.metadata
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import soundfile as sf

from allday_asr.asr.funasr_backend import SPEAKER_MODEL_ID, FunASRBackend
from allday_asr.audio.tools import normalize_to_wav, sha256_file
from allday_asr.paths import STATE_DIR
from allday_asr.storage.database import Database


AUDIO_EXTENSIONS = {".wav", ".m4a", ".mp3", ".aac", ".flac", ".ogg", ".opus"}


@dataclass(frozen=True)
class EnrollmentSummary:
    profile_id: int
    source_files: int
    duplicate_files: int
    source_duration_seconds: float
    speech_seconds: float
    candidate_embeddings: int
    accepted_embeddings: int
    median_centroid_similarity: float
    pairwise_p10_similarity: float
    voiceprint_path: Path


def enroll_self(
    database: Database,
    inputs: list[Path],
    *,
    display_name: str = "我",
    device: str = "auto",
    target_chunk_seconds: float = 5.0,
    min_chunk_seconds: float = 2.0,
    max_chunks_per_source: int = 12,
) -> EnrollmentSummary:
    return _enroll_person(
        database,
        inputs,
        display_name=display_name,
        profile_type="self",
        device=device,
        target_chunk_seconds=target_chunk_seconds,
        min_chunk_seconds=min_chunk_seconds,
        max_chunks_per_source=max_chunks_per_source,
    )


def enroll_known_person(
    database: Database,
    inputs: list[Path],
    *,
    display_name: str,
    device: str = "auto",
    target_chunk_seconds: float = 5.0,
    min_chunk_seconds: float = 2.0,
    max_chunks_per_source: int = 12,
) -> EnrollmentSummary:
    if not display_name.strip():
        raise ValueError("人物名称不能为空")
    return _enroll_person(
        database,
        inputs,
        display_name=display_name.strip(),
        profile_type="known_person",
        device=device,
        target_chunk_seconds=target_chunk_seconds,
        min_chunk_seconds=min_chunk_seconds,
        max_chunks_per_source=max_chunks_per_source,
    )


def _enroll_person(
    database: Database,
    inputs: list[Path],
    *,
    display_name: str,
    profile_type: str,
    device: str = "auto",
    target_chunk_seconds: float = 5.0,
    min_chunk_seconds: float = 2.0,
    max_chunks_per_source: int = 12,
) -> EnrollmentSummary:
    sources, duplicate_count = collect_unique_audio_files(inputs)
    if not sources:
        raise RuntimeError("没有找到可用音频文件")

    backend = FunASRBackend(device=device)
    chunks: list[np.ndarray] = []
    chunk_sources: list[str] = []
    source_metadata: list[dict] = []
    source_duration_seconds = 0.0
    speech_seconds = 0.0
    cache_root = STATE_DIR / "enrollment-cache"

    for source in sources:
        info = sf.info(source)
        source_duration_seconds += float(info.duration)
        model_source = source
        if info.samplerate != 16_000 or info.channels != 1:
            cache_path = cache_root / f"{sha256_file(source)}.wav"
            model_source = normalize_to_wav(source, cache_path)
        samples, sample_rate = sf.read(model_source, dtype="float32", always_2d=False)
        if samples.ndim != 1 or sample_rate != 16_000:
            raise RuntimeError(f"标准化后音频格式无效：{source}")

        vad_segments = backend.detect_speech(model_source)
        source_speech = sum(end_ms - start_ms for start_ms, end_ms in vad_segments) / 1000
        speech_seconds += source_speech
        source_chunks = make_speech_chunks(
            samples,
            sample_rate,
            vad_segments,
            target_seconds=target_chunk_seconds,
            min_seconds=min_chunk_seconds,
        )
        if len(source_chunks) > max_chunks_per_source:
            indices = np.linspace(0, len(source_chunks) - 1, max_chunks_per_source)
            source_chunks = [source_chunks[round(float(index))] for index in indices]
        chunks.extend(normalize_speech_level(chunk) for chunk in source_chunks)
        chunk_sources.extend([str(source.resolve())] * len(source_chunks))
        source_metadata.append(
            {
                "path": str(source.resolve()),
                "sha256": sha256_file(source),
                "duration_seconds": float(info.duration),
                "speech_seconds": source_speech,
                "chunks": len(source_chunks),
            }
        )

    if speech_seconds < 30:
        raise RuntimeError(f"有效语音只有 {speech_seconds:.1f}s，至少需要 30s")
    if len(chunks) < 6:
        raise RuntimeError(f"可用声纹片段只有 {len(chunks)} 个，至少需要 6 个")

    raw_embeddings = backend.extract_speaker_embeddings(chunks)
    normalized = l2_normalize(raw_embeddings)
    accepted_mask, initial_scores = robust_embedding_filter(normalized)
    accepted = normalized[accepted_mask]
    minimum_accepted = max(6, math.ceil(len(normalized) * 0.6))
    if len(accepted) < minimum_accepted:
        raise RuntimeError(
            f"声纹一致性不足：{len(accepted)}/{len(normalized)} 个 embedding 通过质量检查"
        )

    centroid = l2_normalize(accepted.mean(axis=0, keepdims=True))[0]
    centroid_scores = accepted @ centroid
    pairwise = accepted @ accepted.T
    pairwise_values = pairwise[np.triu_indices(len(accepted), k=1)]
    pairwise_p10 = float(np.percentile(pairwise_values, 10)) if len(pairwise_values) else 1.0

    voiceprint_dir = STATE_DIR / "voiceprints"
    voiceprint_dir.mkdir(parents=True, exist_ok=True)
    temporary_path = voiceprint_dir / f"{profile_type}-enrollment.pending.npz"
    metadata = {
        "format": "AllDayRecording person voiceprint v1",
        "display_name": display_name,
        "profile_type": profile_type,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "speaker_model": SPEAKER_MODEL_ID,
        "speaker_model_version": importlib.metadata.version("funasr"),
        "source_files": source_metadata,
        "duplicate_files_skipped": duplicate_count,
        "source_duration_seconds": source_duration_seconds,
        "speech_seconds": speech_seconds,
        "candidate_embeddings": len(normalized),
        "accepted_embeddings": len(accepted),
        "initial_centroid_scores": initial_scores.tolist(),
        "accepted_mask": accepted_mask.tolist(),
        "accepted_chunk_sources": [
            source for source, keep in zip(chunk_sources, accepted_mask, strict=True) if keep
        ],
        "median_centroid_similarity": float(np.median(centroid_scores)),
        "pairwise_p10_similarity": pairwise_p10,
    }
    np.savez_compressed(
        temporary_path,
        embeddings=accepted.astype(np.float32),
        centroid=centroid.astype(np.float32),
        metadata_json=np.asarray(json.dumps(metadata, ensure_ascii=False)),
    )
    upsert_profile = (
        database.upsert_self_profile
        if profile_type == "self"
        else database.upsert_known_person_profile
    )
    profile = upsert_profile(
        display_name=display_name,
        embedding_model=SPEAKER_MODEL_ID,
        embedding_version=importlib.metadata.version("funasr"),
        embedding_path=str(temporary_path.resolve()),
    )
    final_path = voiceprint_dir / f"person-{profile['id']}.npz"
    temporary_path.replace(final_path)
    profile = upsert_profile(
        display_name=display_name,
        embedding_model=SPEAKER_MODEL_ID,
        embedding_version=importlib.metadata.version("funasr"),
        embedding_path=str(final_path.resolve()),
    )
    return EnrollmentSummary(
        profile_id=int(profile["id"]),
        source_files=len(sources),
        duplicate_files=duplicate_count,
        source_duration_seconds=source_duration_seconds,
        speech_seconds=speech_seconds,
        candidate_embeddings=len(normalized),
        accepted_embeddings=len(accepted),
        median_centroid_similarity=float(np.median(centroid_scores)),
        pairwise_p10_similarity=pairwise_p10,
        voiceprint_path=final_path,
    )


def collect_unique_audio_files(inputs: list[Path]) -> tuple[list[Path], int]:
    candidates: list[Path] = []
    for raw_path in inputs:
        path = raw_path.resolve(strict=True)
        if path.is_dir():
            candidates.extend(
                candidate
                for candidate in sorted(path.rglob("*"))
                if candidate.is_file() and candidate.suffix.lower() in AUDIO_EXTENSIONS
            )
        elif path.suffix.lower() in AUDIO_EXTENSIONS:
            candidates.append(path)
    unique: list[Path] = []
    seen_hashes: set[str] = set()
    duplicates = 0
    for candidate in candidates:
        digest = sha256_file(candidate)
        if digest in seen_hashes:
            duplicates += 1
            continue
        seen_hashes.add(digest)
        unique.append(candidate)
    return unique, duplicates


def make_speech_chunks(
    samples: np.ndarray,
    sample_rate: int,
    vad_segments: list[tuple[int, int]],
    *,
    target_seconds: float = 5.0,
    min_seconds: float = 2.0,
) -> list[np.ndarray]:
    speech_parts: list[np.ndarray] = []
    for start_ms, end_ms in vad_segments:
        start = max(0, round(start_ms * sample_rate / 1000))
        end = min(len(samples), round(end_ms * sample_rate / 1000))
        if end > start:
            speech_parts.append(np.asarray(samples[start:end], dtype=np.float32))
    if not speech_parts:
        return []
    speech = np.concatenate(speech_parts)
    target_samples = max(1, round(target_seconds * sample_rate))
    min_samples = max(1, round(min_seconds * sample_rate))
    chunks = [speech[index : index + target_samples] for index in range(0, len(speech), target_samples)]
    if chunks and len(chunks[-1]) < min_samples:
        remainder = chunks.pop()
        if chunks:
            chunks[-1] = np.concatenate([chunks[-1], remainder])
    return [chunk for chunk in chunks if len(chunk) >= min_samples]


def normalize_speech_level(
    samples: np.ndarray, *, target_dbfs: float = -24.0, max_gain_db: float = 24.0
) -> np.ndarray:
    value = np.asarray(samples, dtype=np.float32)
    rms = float(np.sqrt(np.mean(np.square(value), dtype=np.float64)))
    if rms <= 1e-8:
        return value
    current_dbfs = 20 * math.log10(rms)
    gain_db = min(max_gain_db, target_dbfs - current_dbfs)
    gain = 10 ** (gain_db / 20)
    peak = float(np.max(np.abs(value)))
    if peak > 0:
        gain = min(gain, 0.95 / peak)
    return np.asarray(value * gain, dtype=np.float32)


def l2_normalize(values: np.ndarray) -> np.ndarray:
    array = np.asarray(values, dtype=np.float32)
    norms = np.linalg.norm(array, axis=1, keepdims=True)
    return array / np.maximum(norms, 1e-8)


def robust_embedding_filter(embeddings: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    centroid = l2_normalize(embeddings.mean(axis=0, keepdims=True))[0]
    scores = embeddings @ centroid
    median = float(np.median(scores))
    mad = float(np.median(np.abs(scores - median)))
    adaptive_floor = median - 3 * 1.4826 * mad
    floor = max(0.35, adaptive_floor)
    return scores >= floor, scores
