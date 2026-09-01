from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import soundfile as sf

from allday_asr.asr.funasr_backend import FunASRBackend
from allday_asr.audio.embeddings import l2_normalize, normalize_speech_level
from allday_asr.domain.hashing import canonical_json_sha256
from allday_asr.v3.domain.identity import (
    IdentityAcceptancePolicy,
    IdentityHoldoutSample,
    SelfIdentity,
    evaluate_voiceprint_calibration,
)
from allday_asr.v3.domain.identity_calibration import (
    CalibrationWindow,
    ThresholdMetrics,
    calculate_threshold_metrics,
    calibration_blockers,
    merge_calibration_windows,
    select_not_self_threshold,
    select_self_threshold,
    split_adaptation_windows,
)


IDENTITY_CALIBRATION_FORMAT = "AllDayRecording V3.1 identity calibration v1"
IDENTITY_POLICY_FORMAT = "AllDayRecording V3.1 self identity policy v1"
IDENTITY_RECEIPT_FORMAT = "AllDayRecording V3.1 identity receipt v1"


class _EmbeddingBackend(Protocol):
    def extract_speaker_embeddings(
        self, samples: list[np.ndarray], *, batch_size: int = 16
    ) -> np.ndarray: ...


@dataclass(frozen=True)
class IdentityCalibrationSummary:
    accepted: bool
    blockers: tuple[str, ...]
    calibration_path: Path
    policy_path: Path
    receipt_path: Path
    calibration_sha256: str
    policy_sha256: str
    receipt_sha256: str
    metrics: ThresholdMetrics
    self_threshold: float
    not_self_threshold: float
    candidate_voiceprint_path: Path | None = None


def run_identity_calibration(
    bundle_path: Path,
    progress_path: Path,
    database_path: Path,
    *,
    output_dir: Path,
    device: str = "auto",
    acceptance: IdentityAcceptancePolicy | None = None,
    adaptation_session_ids: frozenset[int] = frozenset(),
    voiceprint_path_override: Path | None = None,
    backend_factory: Callable[[], _EmbeddingBackend] | None = None,
) -> IdentityCalibrationSummary:
    acceptance = acceptance or IdentityAcceptancePolicy()
    bundle = _read_json(bundle_path.resolve(strict=True))
    progress = _read_json(progress_path.resolve(strict=True))
    database = database_path.resolve(strict=True)
    windows = merge_calibration_windows(bundle, progress)
    if not windows:
        raise ValueError("没有可用于本人评估的干净人工标注")
    adaptation_windows, holdout_windows = split_adaptation_windows(
        windows,
        adaptation_session_ids,
    )
    if adaptation_session_ids and not adaptation_windows:
        raise ValueError("指定的适配录音中没有本人片段")
    positives = [
        value for value in holdout_windows if value.identity is SelfIdentity.SELF
    ]
    negatives = [
        value for value in holdout_windows if value.identity is SelfIdentity.NOT_SELF
    ]
    if not positives or not negatives:
        raise ValueError("本人评估必须同时包含本人和真人非本人片段")

    connection = _open_read_only(database)
    try:
        waveforms = [
            normalize_speech_level(_read_session_window(connection, value))
            for value in windows
        ]
        if voiceprint_path_override is None:
            profile = connection.execute(
                """
                SELECT embedding_path FROM person_profiles
                WHERE profile_type = 'self'
                ORDER BY id LIMIT 1
                """
            ).fetchone()
            if profile is None or not str(profile["embedding_path"] or ""):
                raise ValueError("没有找到本人声纹档案")
            voiceprint_path = Path(str(profile["embedding_path"])).resolve(
                strict=True
            )
            enrollment_sessions = frozenset(
                str(row["session_key"])
                for row in connection.execute(
                    """
                    SELECT DISTINCT session_key FROM voice_library_samples
                    WHERE split = 'accepted'
                    """
                )
            )
        else:
            voiceprint_path = voiceprint_path_override.resolve(strict=True)
            enrollment_sessions = frozenset()
    finally:
        connection.close()

    voiceprint = np.load(voiceprint_path)
    references = l2_normalize(np.asarray(voiceprint["embeddings"], dtype=np.float32))
    centroid = np.asarray(voiceprint["centroid"], dtype=np.float32)
    backend = (
        backend_factory()
        if backend_factory is not None
        else FunASRBackend(device=device)
    )
    all_embeddings = l2_normalize(
        backend.extract_speaker_embeddings(waveforms, batch_size=16)
    )
    embedding_by_id = {
        value.sample_id: embedding
        for value, embedding in zip(windows, all_embeddings, strict=True)
    }
    candidate_voiceprint_path: Path | None = None
    created_at = datetime.now(timezone.utc).isoformat()
    source_voiceprint_sha256 = _sha256_file(voiceprint_path)
    if adaptation_windows:
        adapted_embeddings = np.vstack(
            [references]
            + [embedding_by_id[value.sample_id] for value in adaptation_windows]
        )
        references = l2_normalize(adapted_embeddings)
        centroid = l2_normalize(references.mean(axis=0, keepdims=True))[0]
        adaptation_sha256 = canonical_json_sha256(
            {
                "source_voiceprint_sha256": source_voiceprint_sha256,
                "adaptation_windows": [
                    _window_public(value) for value in adaptation_windows
                ],
            }
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        candidate_voiceprint_path = (
            output_dir / f"self-voiceprint-adapted-{adaptation_sha256[:12]}.npz"
        )
        metadata = {
            "format": "AllDayRecording V3.1 adapted self voiceprint v1",
            "created_at": created_at,
            "source_voiceprint": str(voiceprint_path),
            "source_voiceprint_sha256": source_voiceprint_sha256,
            "adaptation_session_ids": sorted(adaptation_session_ids),
            "adaptation_sample_ids": [
                value.sample_id for value in adaptation_windows
            ],
            "source_embeddings": int(len(voiceprint["embeddings"])),
            "adaptation_embeddings": len(adaptation_windows),
            "accepted_embeddings": len(references),
        }
        _write_voiceprint_atomically(
            candidate_voiceprint_path,
            references,
            centroid,
            metadata,
        )
        enrollment_sessions = enrollment_sessions.union(
            value.session_key for value in adaptation_windows
        )
    embeddings = np.vstack(
        [embedding_by_id[value.sample_id] for value in holdout_windows]
    )
    scores = combined_voiceprint_scores(embeddings, centroid, references)
    positive_scores = [
        float(score)
        for score, value in zip(scores, holdout_windows, strict=True)
        if value.identity is SelfIdentity.SELF
    ]
    negative_scores = [
        float(score)
        for score, value in zip(scores, holdout_windows, strict=True)
        if value.identity is SelfIdentity.NOT_SELF
    ]
    self_threshold = select_self_threshold(
        positive_scores,
        negative_scores,
        max_false_accept_rate=acceptance.max_false_accept_rate,
    )
    not_self_threshold = select_not_self_threshold(
        positive_scores,
        self_threshold=self_threshold,
    )
    samples = [
        IdentityHoldoutSample(
            score=float(score),
            identity=value.identity,
            session_id=value.session_key,
        )
        for score, value in zip(scores, holdout_windows, strict=True)
    ]
    input_sha256 = canonical_json_sha256(
        {
            "bundle_sha256": str(bundle.get("bundle_sha256") or ""),
            "progress_updated_at": str(progress.get("updated_at") or ""),
            "source_voiceprint_sha256": source_voiceprint_sha256,
            "evaluated_voiceprint_sha256": _sha256_file(
                candidate_voiceprint_path or voiceprint_path
            ),
            "adaptation_windows": [
                _window_public(value) for value in adaptation_windows
            ],
            "holdout_windows": [
                _window_public(value) for value in holdout_windows
            ],
        }
    )
    policy_version = f"v3.1-self-{input_sha256[:12]}"
    calibration = evaluate_voiceprint_calibration(
        samples,
        policy_version=policy_version,
        self_threshold=self_threshold,
        not_self_threshold=not_self_threshold,
        enrollment_session_ids=enrollment_sessions,
    )
    metrics = calculate_threshold_metrics(
        positive_scores,
        negative_scores,
        self_threshold=self_threshold,
        not_self_threshold=not_self_threshold,
    )
    blockers = calibration_blockers(calibration, acceptance)
    accepted = not blockers
    calibration_body: dict[str, Any] = {
        "format": IDENTITY_CALIBRATION_FORMAT,
        "created_at": created_at,
        "input_sha256": input_sha256,
        "source": {
            "bundle": str(bundle_path.resolve()),
            "bundle_sha256": str(bundle.get("bundle_sha256") or ""),
            "progress": str(progress_path.resolve()),
            "progress_updated_at": str(progress.get("updated_at") or ""),
            "database": str(database),
            "source_voiceprint": str(voiceprint_path),
            "source_voiceprint_sha256": source_voiceprint_sha256,
            "evaluated_voiceprint": str(candidate_voiceprint_path or voiceprint_path),
            "evaluated_voiceprint_sha256": _sha256_file(
                candidate_voiceprint_path or voiceprint_path
            ),
            "adaptation_session_ids": sorted(adaptation_session_ids),
            "adaptation_samples": len(adaptation_windows),
        },
        "policy_version": policy_version,
        "acceptance": asdict(acceptance),
        "calibration": asdict(calibration),
        "metrics": asdict(metrics),
        "accepted": accepted,
        "blockers": list(blockers),
        "score_summary": {
            "self": _score_summary(positive_scores),
            "not_self": _score_summary(negative_scores),
        },
        "samples": [
            {
                **_window_public(value),
                "score": float(score),
            }
            for value, score in zip(holdout_windows, scores, strict=True)
        ],
    }
    calibration_sha256 = canonical_json_sha256(calibration_body)
    calibration_document = {
        **calibration_body,
        "calibration_sha256": calibration_sha256,
    }
    policy_body = {
        "format": IDENTITY_POLICY_FORMAT,
        "created_at": created_at,
        "policy_version": policy_version,
        "accepted": accepted,
        "blockers": list(blockers),
        "self_threshold": self_threshold,
        "not_self_threshold": not_self_threshold,
        "positive_holdout": len(positive_scores),
        "negative_holdout": len(negative_scores),
        "false_accept_rate": metrics.false_accept_rate,
        "false_reject_rate": metrics.false_reject_rate,
        "disjoint_holdout": calibration.disjoint_holdout,
        "voiceprint": str(candidate_voiceprint_path or voiceprint_path),
        "voiceprint_sha256": _sha256_file(
            candidate_voiceprint_path or voiceprint_path
        ),
        "calibration_sha256": calibration_sha256,
    }
    policy_sha256 = canonical_json_sha256(policy_body)
    policy_document = {**policy_body, "policy_sha256": policy_sha256}
    receipt_body = {
        "format": IDENTITY_RECEIPT_FORMAT,
        "created_at": created_at,
        "accepted": accepted,
        "blockers": list(blockers),
        "input_sha256": input_sha256,
        "calibration_sha256": calibration_sha256,
        "policy_sha256": policy_sha256,
        "metrics": asdict(metrics),
    }
    receipt_sha256 = canonical_json_sha256(receipt_body)
    receipt_document = {**receipt_body, "receipt_sha256": receipt_sha256}
    output_dir.mkdir(parents=True, exist_ok=True)
    calibration_path = output_dir / f"identity-calibration-{calibration_sha256[:12]}.json"
    policy_path = output_dir / f"self-identity-policy-{policy_sha256[:12]}.json"
    receipt_path = output_dir / f"identity-receipt-{receipt_sha256[:12]}.json"
    _write_json_atomically(calibration_path, calibration_document)
    _write_json_atomically(policy_path, policy_document)
    _write_json_atomically(receipt_path, receipt_document)
    return IdentityCalibrationSummary(
        accepted=accepted,
        blockers=blockers,
        calibration_path=calibration_path,
        policy_path=policy_path,
        receipt_path=receipt_path,
        calibration_sha256=calibration_sha256,
        policy_sha256=policy_sha256,
        receipt_sha256=receipt_sha256,
        metrics=metrics,
        self_threshold=self_threshold,
        not_self_threshold=not_self_threshold,
        candidate_voiceprint_path=candidate_voiceprint_path,
    )


def combined_voiceprint_scores(
    embeddings: np.ndarray,
    centroid: np.ndarray,
    references: np.ndarray,
) -> np.ndarray:
    centroid_scores = embeddings @ np.asarray(centroid, dtype=np.float32)
    reference_scores = embeddings @ references.T
    top_k = min(3, references.shape[0])
    top_reference_scores = np.partition(reference_scores, -top_k, axis=1)[
        :, -top_k:
    ].mean(axis=1)
    return 0.7 * centroid_scores + 0.3 * top_reference_scores


def _read_session_window(
    connection: sqlite3.Connection,
    window: CalibrationWindow,
) -> np.ndarray:
    normalized = connection.execute(
        """
        SELECT recording.normalized_path
        FROM recording_sessions session
        JOIN recordings recording ON recording.id = session.legacy_recording_id
        WHERE session.id = ?
        """,
        (window.session_id,),
    ).fetchone()
    if normalized is not None:
        path = Path(str(normalized["normalized_path"] or ""))
        if path.is_file():
            return _read_audio_part(path, window.start_ms, window.end_ms)
    sources = list(
        connection.execute(
            """
            SELECT source.session_start_ms, source.session_end_ms,
                   source.source_start_ms, instance.source_path
            FROM session_sources source
            JOIN source_instances instance ON instance.id = source.source_instance_id
            WHERE source.session_id = ?
              AND source.session_start_ms < ?
              AND source.session_end_ms > ?
            ORDER BY source.session_start_ms, source.chunk_index
            """,
            (window.session_id, window.end_ms, window.start_ms),
        )
    )
    if not sources:
        raise ValueError(f"找不到评估片段原音：{window.sample_id}")
    parts: list[np.ndarray] = []
    cursor = window.start_ms
    for source in sources:
        part_start = max(cursor, int(source["session_start_ms"]))
        part_end = min(window.end_ms, int(source["session_end_ms"]))
        if part_end <= part_start:
            continue
        if part_start > cursor:
            raise ValueError(f"评估片段跨越原音缺口：{window.sample_id}")
        source_start = int(source["source_start_ms"]) + (
            part_start - int(source["session_start_ms"])
        )
        source_end = source_start + (part_end - part_start)
        parts.append(
            _read_audio_part(
                Path(str(source["source_path"])).resolve(strict=True),
                source_start,
                source_end,
            )
        )
        cursor = part_end
    if cursor < window.end_ms:
        raise ValueError(f"评估片段原音不完整：{window.sample_id}")
    return np.concatenate(parts)


def _read_audio_part(path: Path, start_ms: int, end_ms: int) -> np.ndarray:
    with sf.SoundFile(path) as audio:
        if audio.samplerate != 16_000 or audio.channels != 1:
            raise ValueError(f"评估原音必须是 16 kHz 单声道：{path}")
        start_frame = round(start_ms * audio.samplerate / 1000)
        frame_count = round((end_ms - start_ms) * audio.samplerate / 1000)
        audio.seek(start_frame)
        samples = audio.read(frame_count, dtype="float32", always_2d=False)
    if len(samples) != frame_count:
        raise ValueError(f"评估原音长度不足：{path}")
    return np.asarray(samples, dtype=np.float32)


def _score_summary(scores: Sequence[float]) -> dict[str, float]:
    values = np.asarray(scores, dtype=np.float64)
    return {
        "min": float(np.min(values)),
        "p10": float(np.percentile(values, 10)),
        "median": float(np.median(values)),
        "p90": float(np.percentile(values, 90)),
        "max": float(np.max(values)),
    }


def _window_public(value: CalibrationWindow) -> dict[str, Any]:
    return {
        "sample_id": value.sample_id,
        "session_id": value.session_id,
        "session_key": value.session_key,
        "start_ms": value.start_ms,
        "end_ms": value.end_ms,
        "duration_ms": value.duration_ms,
        "identity": value.identity.value,
        "origin": value.origin,
        "provenance": list(value.provenance),
    }


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 根节点必须是对象：{path}")
    return value


def _open_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{path.resolve(strict=True).as_posix()}?mode=ro&immutable=1",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    return connection


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(4 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _write_json_atomically(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _write_voiceprint_atomically(
    path: Path,
    embeddings: np.ndarray,
    centroid: np.ndarray,
    metadata: Mapping[str, Any],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".pending.npz")
    np.savez_compressed(
        temporary,
        embeddings=np.asarray(embeddings, dtype=np.float32),
        centroid=np.asarray(centroid, dtype=np.float32),
        metadata_json=np.asarray(json.dumps(metadata, ensure_ascii=False)),
    )
    temporary.replace(path)


__all__ = [
    "CalibrationWindow",
    "IdentityCalibrationSummary",
    "ThresholdMetrics",
    "calculate_threshold_metrics",
    "combined_voiceprint_scores",
    "merge_calibration_windows",
    "run_identity_calibration",
    "select_not_self_threshold",
    "select_self_threshold",
    "split_adaptation_windows",
]
