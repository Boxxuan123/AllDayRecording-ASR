from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from allday_asr.asr.quality_backends import QualityAsrBackend
from allday_asr.domain.hashing import canonical_json_sha256
from allday_asr.domain.text import levenshtein_operations, normalize_text
from allday_asr.paths import OUTPUT_DIR
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
    model_signature: str = ""
    pipeline_revision: str = "v2c3-dual-vad-evidence-gate-v2"
    max_windows: int | None = None
    speech_gate_fsmn_merge_gap_ms: int = 600
    speech_gate_max_utterance_ms: int = 30_000
    speech_gate_inference_padding_ms: int = 750
    speech_gate_output_padding_ms: int = 500
    speech_gate_min_candidate_ms: int = 800
    speech_gate_min_snr_db: float = 9.0
    speech_gate_silero_threshold: float = 0.15
    speech_gate_silero_min_speech_ms: int = 100
    speech_gate_silero_min_silence_ms: int = 250
    speech_gate_min_silero_overlap_ms: int = 500

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def sha256(self) -> str:
        return canonical_json_sha256(self.to_dict())


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
    speech_candidates: int
    accepted_speech_candidates: int
    rejected_speech_candidates: int
    committed_primary_tokens: int
    manifest_path: Path


@dataclass(frozen=True)
class QualitySnapshotSummary:
    prediction_set_id: int
    prediction_count: int
    content_sha256: str


BackendFactory = Callable[[], QualityAsrBackend]


def run_quality_asr(
    database: Database,
    recording_id: int | None,
    *,
    session_id: int | None = None,
    settings: QualityAsrSettings,
    primary_factory: BackendFactory,
    secondary_factory: BackendFactory,
    resume_run_id: int | None = None,
) -> QualityAsrSummary:
    if session_id is None:
        if recording_id is None:
            raise ValueError("V2-C 必须指定 recording_id 或 session_id")
        session = database.get_session_for_recording(recording_id)
        session_id = int(session["id"])
    else:
        session = database.get_recording_session(session_id)
        if (
            recording_id is not None
            and session["legacy_recording_id"] is not None
            and int(session["legacy_recording_id"]) != recording_id
        ):
            raise ValueError("recording_id 与 session_id 不属于同一会话")
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
            session_id=session_id,
            run_kind="quality_asr_v2c",
            config=settings.to_dict(),
            config_sha256=settings.sha256(),
            model_manifest=model_manifest,
            pipeline_version="v2-c",
        )
    else:
        run = database.get_processing_run(resume_run_id)
        if int(run["session_id"]) != session_id:
            raise ValueError("resume run belongs to a different recording session")
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
        primary_hypotheses = [
            row for row in hypotheses if row["hypothesis_role"] == "primary"
        ]
        speech_segments = [
            segment
            for row in primary_hypotheses
            for segment in json.loads(str(row["raw_response_json"] or "{}")).get(
                "segments", []
            )
        ]
        accepted_speech_candidates = sum(
            bool(segment.get("accepted", True)) for segment in speech_segments
        )
        committed_primary_tokens = sum(
            len(database.list_asr_tokens(int(row["id"]), core_only=True))
            for row in primary_hypotheses
        )
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
            "speech_candidates": len(speech_segments),
            "accepted_speech_candidates": accepted_speech_candidates,
            "rejected_speech_candidates": (
                len(speech_segments) - accepted_speech_candidates
            ),
            "committed_primary_tokens": committed_primary_tokens,
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
    database: Database,
    run_id: int,
    *,
    name: str | None = None,
    truth_set_id: int | None = None,
) -> QualitySnapshotSummary:
    run = database.get_processing_run(run_id)
    if str(run["run_kind"]) != "quality_asr_v2c":
        raise ValueError("processing run is not a V2-C ASR run")
    if str(run["status"]) != "completed":
        raise ValueError("only a completed V2-C run can be frozen as a benchmark snapshot")
    session_id = int(run["session_id"])
    input_fingerprint = str(run["input_fingerprint"])
    adapter = "quality-asr-v2c-primary-speech-aligned-token-transcripts-v4"
    evaluation_scopes: list[tuple[int, int]] | None = None
    if truth_set_id is not None:
        truth_set = database.get_truth_set(truth_set_id)
        if int(truth_set["session_id"]) != session_id:
            raise ValueError("V2-C run 和真值集不属于同一录音会话")
        if str(truth_set["input_fingerprint"]) != input_fingerprint:
            raise ValueError("V2-C run 和真值集的原始输入指纹不一致")
        review_regions = [
            row
            for row in database.list_truth_annotations(truth_set_id)
            if str(row["label"] or "") == "review_region_complete_scope"
        ]
        if review_regions:
            evaluation_scopes = [
                (int(row["session_start_ms"]), int(row["session_end_ms"]))
                for row in review_regions
            ]
        else:
            evaluation_scopes = [
                (int(truth_set["scope_start_ms"]), int(truth_set["scope_end_ms"]))
            ]
        evaluation_scopes.sort()
        for previous, current in zip(evaluation_scopes, evaluation_scopes[1:]):
            if current[0] < previous[1]:
                raise ValueError("真值集的 review-region 相互重叠")
        adapter += "-review-scoped"

    predictions: list[dict[str, Any]] = []
    for hypothesis in database.list_asr_hypotheses(run_id, role="primary"):
        hypothesis_id = int(hypothesis["id"])
        hypothesis_scopes = evaluation_scopes or [
            (int(hypothesis["core_start_ms"]), int(hypothesis["core_end_ms"]))
        ]
        raw_response = json.loads(str(hypothesis["raw_response_json"] or "{}"))
        for region_index, value in enumerate(raw_response.get("speech_ranges_ms", [])):
            if not isinstance(value, (list, tuple)) or len(value) < 2:
                continue
            original_region_start = int(hypothesis["analysis_start_ms"]) + int(
                value[0]
            )
            original_region_end = int(hypothesis["analysis_start_ms"]) + int(value[1])
            region_start = original_region_start
            region_end = original_region_end
            region_start = max(region_start, int(hypothesis["core_start_ms"]))
            region_end = min(region_end, int(hypothesis["core_end_ms"]))
            for scope_index, (scope_start, scope_end) in enumerate(hypothesis_scopes):
                start_ms = max(region_start, scope_start)
                end_ms = min(region_end, scope_end)
                if end_ms <= start_ms:
                    continue
                scope_suffix = (
                    ""
                    if evaluation_scopes is None
                    else f":truth:{truth_set_id}:scope:{scope_index}"
                )
                predictions.append(
                    {
                        "prediction_key": (
                            f"v2c:{run_id}:hypothesis:{hypothesis_id}:speech:"
                            f"{region_index}{scope_suffix}"
                        ),
                        "prediction_kind": "speech",
                        "session_start_ms": start_ms,
                        "session_end_ms": end_ms,
                        "label": "speech",
                        "metadata": {
                            "hypothesis_id": hypothesis_id,
                            "model_id": hypothesis["model_id"],
                            "speech_region_index": region_index,
                            "original_session_start_ms": original_region_start,
                            "original_session_end_ms": original_region_end,
                            "core_clipped": (
                                region_start != original_region_start
                                or region_end != original_region_end
                            ),
                            "scope_clipped": (
                                start_ms != region_start or end_ms != region_end
                            ),
                            "truth_set_id": truth_set_id,
                            "review_region_index": (
                                scope_index if evaluation_scopes is not None else None
                            ),
                            "source": "immutable hypothesis raw_response.speech_ranges_ms",
                        },
                    }
                )
        tokens = database.list_asr_tokens(hypothesis_id, core_only=True)
        for token in tokens:
            token_id = int(token["id"])
            source_trace_complete = bool(database.list_asr_token_sources(token_id))
            token_metadata = json.loads(str(token["metadata_json"] or "{}"))
            token_start = int(token["session_start_ms"])
            token_end = int(token["session_end_ms"])
            spans = (
                [(None, token_start, token_end)]
                if evaluation_scopes is None
                else [
                    (scope_index, max(token_start, start), min(token_end, end))
                    for scope_index, (start, end) in enumerate(evaluation_scopes)
                    if token_start < end and token_end > start
                ]
            )
            # Transcript predictions must use the aligned token interval.  A whole
            # five-minute window would connect every sparse truth interval it
            # touches and incorrectly charge unrelated speech as ASR insertions.
            for scope_index, start_ms, end_ms in spans:
                if end_ms <= start_ms:
                    continue
                scope_suffix = (
                    ""
                    if scope_index is None
                    else f":truth:{truth_set_id}:scope:{scope_index}"
                )
                common_metadata = {
                    "hypothesis_id": hypothesis_id,
                    "token_id": token_id,
                    "token_index": int(token["token_index"]),
                    "source_trace_complete": source_trace_complete,
                    "original_session_start_ms": token_start,
                    "original_session_end_ms": token_end,
                    "scope_clipped": start_ms != token_start or end_ms != token_end,
                    "alignment_metadata": token_metadata,
                }
                if truth_set_id is not None:
                    common_metadata.update(
                        {
                            "truth_set_id": truth_set_id,
                            "review_region_index": scope_index,
                        }
                    )
                predictions.append(
                    {
                        "prediction_key": (
                            f"v2c:{run_id}:token:{token_id}{scope_suffix}:transcript"
                        ),
                        "prediction_kind": "transcript",
                        "session_start_ms": start_ms,
                        "session_end_ms": end_ms,
                        "text": str(token["text"]),
                        "metadata": {
                            **common_metadata,
                            "model_id": hypothesis["model_id"],
                        },
                    }
                )
                predictions.append(
                    {
                        "prediction_key": (
                            f"v2c:{run_id}:token:{token_id}{scope_suffix}:alignment"
                        ),
                        "prediction_kind": "alignment_token",
                        "session_start_ms": start_ms,
                        "session_end_ms": end_ms,
                        "text": str(token["text"]),
                        "metadata": common_metadata,
                    }
                )
    if not predictions:
        raise ValueError("V2-C run has no primary predictions to snapshot")
    manifest = json.loads(str(run["model_manifest_json"] or "{}"))
    if truth_set_id is not None:
        manifest["benchmark_scope"] = {
            "truth_set_id": truth_set_id,
            "review_regions": [
                {"start_ms": start, "end_ms": end}
                for start, end in evaluation_scopes or []
            ],
            "boundary_policy": "intersect aligned token interval with review region",
        }
    prediction_key = f"v2c:{run_id}:primary:{adapter}"
    if truth_set_id is not None:
        prediction_key += f":truth:{truth_set_id}"
    row = database.create_benchmark_prediction_set(
        {
            "prediction_key": prediction_key,
            "name": name
            or (
                f"v2-c-run-{run_id}-primary-truth-{truth_set_id}"
                if truth_set_id is not None
                else f"v2-c-run-{run_id}-primary"
            ),
            "session_id": session_id,
            "processing_run_id": run_id,
            "input_fingerprint": input_fingerprint,
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
        token_metadata = token.metadata or {}
        speech_gate_committed = bool(
            token_metadata.get("speech_gate_committed", True)
        )
        kept = (
            window.core_start_ms <= midpoint < window.core_end_ms
            and speech_gate_committed
        )
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
                    **token_metadata,
                    "raw_token_index": raw_index,
                    "boundary_policy": (
                        "token-midpoint-in-window-core-and-accepted-speech-core-v2c3"
                        if "speech_gate_committed" in token_metadata
                        else "token-midpoint-in-core-v1"
                    ),
                },
                "source_refs": [
                        {
                            "source_object_id": item.source_object_id,
                            "source_instance_id": item.source_instance_id,
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
    raw_response = json.loads(str(hypothesis["raw_response_json"] or "{}"))
    expected_committed = raw_response.get("committed_token_text")
    tokens = database.list_asr_tokens(int(hypothesis["id"]))
    if expected_committed is not None:
        text = normalize_text(str(expected_committed))
        tokens = [
            token
            for token in tokens
            if json.loads(str(token["metadata_json"] or "{}")).get(
                "speech_gate_committed", False
            )
        ]
    token_text = normalize_text(
        "".join(str(token["text"]) for token in tokens)
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
