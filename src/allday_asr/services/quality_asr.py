from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from allday_asr.asr.quality_backends import QualityAsrBackend
from allday_asr.paths import OUTPUT_DIR
from allday_asr.services.benchmark import normalize_text
from allday_asr.services.evaluation import levenshtein_operations
from allday_asr.services.sources import (
    LogicalWindow,
    plan_logical_windows,
    resolve_session_slices,
    temporary_logical_window,
)
from allday_asr.storage.database import Database


@dataclass(frozen=True)
class QualityAsrSettings:
    language: str = "zh"
    window_ms: int = 300_000
    context_ms: int = 5_000
    vram_profile: str = "quality-16gb"
    pipeline_revision: str = "v2c-vad-utterance-v2"
    max_windows: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def sha256(self) -> str:
        canonical = json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class QualityAsrSummary:
    run_id: int
    session_id: int
    window_count: int
    primary_hypotheses: int
    secondary_hypotheses: int
    aligned_tokens: int
    disagreements: int
    low_alignment_hypotheses: int
    manifest_path: Path


@dataclass(frozen=True)
class QualitySnapshotSummary:
    prediction_set_id: int
    prediction_count: int
    content_sha256: str


BackendFactory = Callable[[], QualityAsrBackend]


def run_quality_asr(
    database: Database,
    recording_id: int,
    *,
    settings: QualityAsrSettings,
    primary_factory: BackendFactory,
    secondary_factory: BackendFactory,
    resume_run_id: int | None = None,
) -> QualityAsrSummary:
    session = database.get_session_for_recording(recording_id)
    session_id = int(session["id"])
    windows = plan_logical_windows(
        database,
        session_id,
        window_ms=settings.window_ms,
        context_ms=settings.context_ms,
    )
    if settings.max_windows is not None:
        if settings.max_windows < 1:
            raise ValueError("max_windows must be at least 1")
        windows = windows[: settings.max_windows]

    if resume_run_id is None:
        primary_probe = primary_factory()
        secondary_probe = secondary_factory()
        try:
            model_manifest = {
                "primary": _backend_manifest(primary_probe),
                "secondary": _backend_manifest(secondary_probe),
                "quality_policy": "preserve-both-hypotheses-primary-qwen-aligned",
            }
        finally:
            _close_backend(primary_probe)
            _close_backend(secondary_probe)
        run_id = database.start_processing_run(
            recording_id,
            run_kind="quality_asr_v2c",
            config=settings.to_dict(),
            config_sha256=settings.sha256(),
            model_manifest=model_manifest,
            pipeline_version="v2-c",
        )
    else:
        run = database.get_processing_run(resume_run_id)
        if int(run["recording_id"]) != recording_id:
            raise ValueError("resume run belongs to a different recording")
        if str(run["config_sha256"]) != settings.sha256():
            raise ValueError("resume settings do not match the original run")
        database.resume_processing_run(resume_run_id)
        run_id = resume_run_id

    try:
        for factory, role in (
            (primary_factory, "primary"),
            (secondary_factory, "secondary"),
        ):
            backend = factory()
            if backend.role != role:
                raise ValueError(f"backend role mismatch: expected {role}, got {backend.role}")
            try:
                existing = {
                    int(row["window_index"])
                    for row in database.list_asr_hypotheses(run_id, role=role)
                }
                for window in windows:
                    if window.index in existing:
                        continue
                    _process_window(
                        database,
                        run_id,
                        window,
                        backend,
                        language=settings.language,
                    )
            finally:
                _close_backend(backend)

        _populate_disagreements(database, run_id, windows)
        hypotheses = database.list_asr_hypotheses(run_id)
        aligned_tokens = sum(
            len(database.list_asr_tokens(int(row["id"]))) for row in hypotheses
        )
        disagreements = database.list_asr_disagreements(run_id)
        low_alignment_hypotheses = sum(
            _hypothesis_alignment_coverage(database, row) < 0.85
            for row in hypotheses
            if str(row["text"]).strip()
        )
        summary_payload = {
            "session_id": session_id,
            "window_count": len(windows),
            "primary_hypotheses": sum(
                row["hypothesis_role"] == "primary" for row in hypotheses
            ),
            "secondary_hypotheses": sum(
                row["hypothesis_role"] == "secondary" for row in hypotheses
            ),
            "aligned_tokens": aligned_tokens,
            "disagreements": len(disagreements),
            "low_alignment_hypotheses": low_alignment_hypotheses,
        }
        manifest_path = _write_manifest(database, run_id, settings, summary_payload)
        database.finish_processing_run(
            run_id,
            status="completed",
            summary=summary_payload,
            artifacts={"manifest": str(manifest_path)},
        )
        return QualityAsrSummary(
            run_id=run_id,
            manifest_path=manifest_path,
            **summary_payload,
        )
    except Exception as exc:
        database.finish_processing_run(run_id, status="failed", error=repr(exc))
        raise


def snapshot_quality_asr(
    database: Database, run_id: int, *, name: str | None = None
) -> QualitySnapshotSummary:
    adapter = "quality-asr-v2c-primary-aligned-token-transcripts-v2"
    run = database.get_processing_run(run_id)
    if str(run["run_kind"]) != "quality_asr_v2c":
        raise ValueError("processing run is not a V2-C ASR run")
    if str(run["status"]) != "completed":
        raise ValueError("only a completed V2-C run can be frozen as a benchmark snapshot")
    session_id = int(run["session_id"])
    predictions: list[dict[str, Any]] = []
    for hypothesis in database.list_asr_hypotheses(run_id, role="primary"):
        hypothesis_id = int(hypothesis["id"])
        tokens = database.list_asr_tokens(hypothesis_id, core_only=True)
        for token in tokens:
            token_id = int(token["id"])
            source_trace_complete = bool(database.list_asr_token_sources(token_id))
            # Transcript predictions must use the aligned token interval.  A whole
            # five-minute window would connect every sparse truth interval it
            # touches and incorrectly charge unrelated speech as ASR insertions.
            predictions.append(
                {
                    "prediction_key": f"v2c:{run_id}:token:{token_id}:transcript",
                    "prediction_kind": "transcript",
                    "session_start_ms": int(token["session_start_ms"]),
                    "session_end_ms": int(token["session_end_ms"]),
                    "text": str(token["text"]),
                    "metadata": {
                        "hypothesis_id": hypothesis_id,
                        "model_id": hypothesis["model_id"],
                        "token_id": token_id,
                        "token_index": int(token["token_index"]),
                        "source_trace_complete": source_trace_complete,
                    },
                }
            )
            predictions.append(
                {
                    "prediction_key": f"v2c:{run_id}:token:{token_id}:alignment",
                    "prediction_kind": "alignment_token",
                    "session_start_ms": int(token["session_start_ms"]),
                    "session_end_ms": int(token["session_end_ms"]),
                    "text": str(token["text"]),
                    "metadata": {
                        "hypothesis_id": hypothesis_id,
                        "token_id": token_id,
                        "token_index": int(token["token_index"]),
                        "source_trace_complete": source_trace_complete,
                    },
                }
            )
    if not predictions:
        raise ValueError("V2-C run has no primary predictions to snapshot")
    manifest = json.loads(str(run["model_manifest_json"] or "{}"))
    row = database.create_benchmark_prediction_set(
        {
            "prediction_key": f"v2c:{run_id}:primary:{adapter}",
            "name": name or f"v2-c-run-{run_id}-primary",
            "session_id": session_id,
            "processing_run_id": run_id,
            "input_fingerprint": str(run["input_fingerprint"]),
            "adapter": adapter,
            "model_manifest": manifest,
        },
        predictions,
    )
    return QualitySnapshotSummary(
        prediction_set_id=int(row["id"]),
        prediction_count=len(predictions),
        content_sha256=str(row["content_sha256"]),
    )


def _process_window(
    database: Database,
    run_id: int,
    window: LogicalWindow,
    backend: QualityAsrBackend,
    *,
    language: str | None,
) -> None:
    if not window.coverage_complete:
        raise RuntimeError(f"window {window.index} has source gaps: {window.uncovered_ranges}")
    with temporary_logical_window(window) as audio_path:
        result = backend.transcribe(audio_path, language=language)
    tokens = _trace_tokens(database, window, backend, result.tokens)
    if backend.role == "primary" and result.text and not tokens:
        raise RuntimeError(
            f"primary window {window.index} returned text without forced-alignment tokens"
        )
    database.create_asr_hypothesis(
        {
            "hypothesis_key": f"v2c:{run_id}:{window.index}:{backend.role}",
            "run_id": run_id,
            "session_id": window.session_id,
            "window_index": window.index,
            "hypothesis_role": backend.role,
            "core_start_ms": window.core_start_ms,
            "core_end_ms": window.core_end_ms,
            "analysis_start_ms": window.analysis_start_ms,
            "analysis_end_ms": window.analysis_end_ms,
            "model_id": backend.model_id,
            "model_revision": backend.model_revision,
            "backend": backend.backend_name,
            "language": result.language,
            "text": result.text,
            "parameters": backend.parameters(),
            "raw_response": result.raw_response,
        },
        tokens,
    )


def _trace_tokens(database, window, backend, aligned_tokens) -> list[dict[str, Any]]:
    traced: list[dict[str, Any]] = []
    for raw_index, token in enumerate(aligned_tokens):
        relative_start_ms = max(0, round(token.start_seconds * 1000))
        relative_end_ms = min(window.duration_ms, round(token.end_seconds * 1000))
        if relative_end_ms <= relative_start_ms:
            continue
        session_start = window.analysis_start_ms + relative_start_ms
        session_end = window.analysis_start_ms + relative_end_ms
        slices, gaps = resolve_session_slices(
            database, window.session_id, session_start, session_end
        )
        if gaps or not slices:
            raise RuntimeError(
                f"token {raw_index} in window {window.index} cannot be traced to source"
            )
        midpoint = (session_start + session_end) / 2
        kept = window.core_start_ms <= midpoint < window.core_end_ms
        traced.append(
            {
                "token_index": len(traced),
                "text": token.text,
                "session_start_ms": session_start,
                "session_end_ms": session_end,
                "analysis_start_ms": relative_start_ms,
                "analysis_end_ms": relative_end_ms,
                "kept_in_core": kept,
                "confidence": token.confidence,
                "alignment_model_id": backend.alignment_model_id,
                "metadata": {
                    **(token.metadata or {}),
                    "raw_token_index": raw_index,
                    "boundary_policy": "token-midpoint-in-core-v1",
                },
                "source_refs": [
                    {
                        "source_object_id": item.source_object_id,
                        "source_sha256": item.source_sha256,
                        "source_start_ms": item.source_start_ms,
                        "source_end_ms": item.source_end_ms,
                    }
                    for item in slices
                ],
            }
        )
    return traced


def _populate_disagreements(
    database: Database, run_id: int, windows: list[LogicalWindow]
) -> None:
    existing = {int(row["window_index"]) for row in database.list_asr_disagreements(run_id)}
    rows = database.list_asr_hypotheses(run_id)
    by_window = {
        (int(row["window_index"]), str(row["hypothesis_role"])): row for row in rows
    }
    for window in windows:
        if window.index in existing:
            continue
        primary = by_window.get((window.index, "primary"))
        secondary = by_window.get((window.index, "secondary"))
        if primary is None or secondary is None:
            continue
        primary_text = normalize_text(str(primary["text"]))
        secondary_text = normalize_text(str(secondary["text"]))
        distance = _normalized_distance(primary_text, secondary_text)
        primary_coverage = _hypothesis_alignment_coverage(database, primary)
        secondary_coverage = _hypothesis_alignment_coverage(database, secondary)
        if distance <= 0 and min(primary_coverage, secondary_coverage) >= 0.85:
            continue
        priority = (
            "high"
            if distance >= 0.35 or min(primary_coverage, secondary_coverage) < 0.85
            else "medium"
            if distance >= 0.15
            else "low"
        )
        database.create_asr_disagreement(
            {
                "run_id": run_id,
                "window_index": window.index,
                "primary_hypothesis_id": int(primary["id"]),
                "secondary_hypothesis_id": int(secondary["id"]),
                "session_start_ms": window.core_start_ms,
                "session_end_ms": window.core_end_ms,
                "normalized_distance": distance,
                "priority": priority,
                "details": {
                    "primary_text": str(primary["text"]),
                    "secondary_text": str(secondary["text"]),
                    "metric": "normalized-character-levenshtein-v1",
                    "primary_alignment_coverage": primary_coverage,
                    "secondary_alignment_coverage": secondary_coverage,
                },
            }
        )


def _normalized_distance(left: str, right: str) -> float:
    if not left and not right:
        return 0.0
    operations = levenshtein_operations(left, right)
    edits = operations["substitutions"] + operations["deletions"] + operations["insertions"]
    return min(1.0, edits / max(len(left), len(right), 1))


def _hypothesis_alignment_coverage(database: Database, hypothesis) -> float:
    text = normalize_text(str(hypothesis["text"]))
    if not text:
        return 1.0
    token_text = normalize_text(
        "".join(
            str(token["text"])
            for token in database.list_asr_tokens(int(hypothesis["id"]))
        )
    )
    return max(0.0, 1.0 - _normalized_distance(text, token_text))


def _backend_manifest(backend: QualityAsrBackend) -> dict[str, Any]:
    return {
        "role": backend.role,
        "model_id": backend.model_id,
        "alignment_model_id": backend.alignment_model_id,
        "backend": backend.backend_name,
        "model_revision": backend.model_revision,
        "parameters": backend.parameters(),
    }


def _close_backend(backend: QualityAsrBackend) -> None:
    close = getattr(backend, "close", None)
    if callable(close):
        close()


def _write_manifest(
    database: Database,
    run_id: int,
    settings: QualityAsrSettings,
    summary: dict[str, Any],
) -> Path:
    run = database.get_processing_run(run_id)
    output_dir = OUTPUT_DIR / f"session-{int(run['session_id']):06d}" / "asr-v2c"
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"run-{run_id:06d}.json"
    payload = {
        "format": "allday-recording-asr-v2c-run-v1",
        "run_id": run_id,
        "session_id": int(run["session_id"]),
        "input_fingerprint": str(run["input_fingerprint"]),
        "settings": settings.to_dict(),
        "model_manifest": json.loads(str(run["model_manifest_json"] or "{}")),
        "summary": summary,
        "hypotheses": [
            {
                "id": int(row["id"]),
                "window_index": int(row["window_index"]),
                "role": str(row["hypothesis_role"]),
                "model_id": str(row["model_id"]),
                "content_sha256": str(row["content_sha256"]),
            }
            for row in database.list_asr_hypotheses(run_id)
        ],
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path
