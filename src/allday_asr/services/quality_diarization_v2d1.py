from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from allday_asr.domain.hashing import canonical_json_sha256 as _sha256_mapping
from allday_asr.paths import EVALUATION_DIR, OUTPUT_DIR
from allday_asr.services.benchmark import (
    CONTINUOUS_TRUTH_FORMAT,
    NAME_PATTERN,
    SPEECH_SOURCE_VALUES,
    ContinuousTruthSummary,
    evaluate_benchmark,
    import_continuous_truth,
)
from allday_asr.storage.database import Database

V2D1_DETECTED_ADAPTER = "quality-diarization-v2d1-detected-v1"
V2D1_RESCUE_ADAPTER = "quality-diarization-v2d1-recall-rescue-v1"


@dataclass(frozen=True)
class V2D1Settings:
    bridge_gap_ms: int = 4_000
    require_rejected_asr_text: bool = True
    pipeline_revision: str = "v2d1-source-separated-recall-rescue-v1"

    def __post_init__(self) -> None:
        if self.bridge_gap_ms < 0 or self.bridge_gap_ms > 10_000:
            raise ValueError("bridge_gap_ms must be between 0 and 10000")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class V2D1Summary:
    run_id: int
    diarization_run_id: int
    asr_run_id: int
    detected_prediction_set_id: int
    rescue_prediction_set_id: int
    detected_regions: int
    possible_regions: int
    evidence_components: int
    detected_ms: int
    possible_ms: int
    expanded_ms: int
    evaluations: dict[str, Any]
    speaker_policy: str
    manifest_path: Path


@dataclass(frozen=True)
class EvidenceInterval:
    start_ms: int
    end_ms: int
    evidence_type: str
    metadata: dict[str, Any]


def run_quality_diarization_v2d1(
    database: Database,
    recording_id: int | None,
    *,
    session_id: int | None = None,
    diarization_run_id: int,
    settings: V2D1Settings,
    evaluation_truth_set_ids: Sequence[int] = (),
) -> V2D1Summary:
    """Create separate detected/possible speech layers without relabeling speakers."""
    diarization_run = database.get_processing_run(diarization_run_id)
    run_session_id = int(diarization_run["session_id"])
    if session_id is None:
        if recording_id is None:
            session_id = run_session_id
        else:
            session_id = int(database.get_session_for_recording(recording_id)["id"])
    if session_id != run_session_id:
        raise ValueError("V2-D run does not belong to the selected recording session")
    run_recording_id = (
        int(diarization_run["recording_id"])
        if diarization_run["recording_id"] is not None
        else None
    )
    if recording_id is not None and run_recording_id != recording_id:
        raise ValueError("V2-D run does not belong to the selected recording")
    if str(diarization_run["run_kind"]) != "quality_diarization_v2d":
        raise ValueError("V2-D.1 requires a V2-D diarization parent run")
    if str(diarization_run["status"]) != "completed":
        raise ValueError("V2-D.1 requires a completed V2-D parent run")
    diarization_summary = _json_object(diarization_run["summary_json"])
    asr_run_id = int(
        diarization_summary.get("asr_run_id")
        or diarization_run["parent_run_id"]
        or 0
    )
    if not asr_run_id:
        raise ValueError("V2-D parent run does not identify its V2-C ASR run")
    asr_run = database.get_processing_run(asr_run_id)
    if str(asr_run["run_kind"]) != "quality_asr_v2c" or str(
        asr_run["status"]
    ) != "completed":
        raise ValueError("V2-D.1 requires the completed parent V2-C ASR run")

    session = database.get_recording_session(session_id)
    duration_ms = int(session["duration_ms"])
    if int(diarization_run["session_id"]) != session_id or int(
        asr_run["session_id"]
    ) != session_id:
        raise ValueError("V2-D.1 parent runs belong to a different recording session")

    config = {
        **settings.to_dict(),
        "diarization_run_id": diarization_run_id,
        "asr_run_id": asr_run_id,
        "speaker_policy": "preserve anonymous speaker labels; never merge by media source",
        "source_policy": "speech source is an independent human-truth layer",
    }
    config_sha256 = _sha256_mapping(config)
    run_id = database.start_processing_run(
        recording_id,
        session_id=session_id,
        run_kind="quality_diarization_v2d1",
        config=config,
        config_sha256=config_sha256,
        model_manifest={
            "kind": "evidence-fusion-no-new-model",
            "parents": {
                "diarization_run_id": diarization_run_id,
                "asr_run_id": asr_run_id,
            },
            "evidence": [
                "pyannote-regular-turns",
                "v2c-accepted-speech-ranges",
                "v2c-rejected-candidates-with-asr-text",
            ],
        },
        pipeline_version="v2-d.1",
        parent_run_id=diarization_run_id,
    )
    try:
        detected, possible, components = build_v2d1_speech_layers(
            database,
            diarization_run_id=diarization_run_id,
            asr_run_id=asr_run_id,
            duration_ms=duration_ms,
            settings=settings,
        )
        detected_predictions = _prediction_rows(
            run_id, detected, components, include_possible=False
        )
        rescue_predictions = _prediction_rows(
            run_id, [*detected, *possible], components, include_possible=True
        )
        fingerprint = database.session_input_fingerprint(session_id)
        detected_set = database.create_benchmark_prediction_set(
            {
                "prediction_key": f"v2d1:{run_id}:detected",
                "name": f"v2-d1-run-{run_id}-detected",
                "session_id": session_id,
                "processing_run_id": run_id,
                "input_fingerprint": fingerprint,
                "adapter": V2D1_DETECTED_ADAPTER,
                "model_manifest": {
                    "settings": settings.to_dict(),
                    "tier": "detected",
                    "diarization_run_id": diarization_run_id,
                    "asr_run_id": asr_run_id,
                },
            },
            detected_predictions,
        )
        rescue_set = database.create_benchmark_prediction_set(
            {
                "prediction_key": f"v2d1:{run_id}:recall-rescue",
                "name": f"v2-d1-run-{run_id}-recall-rescue",
                "session_id": session_id,
                "processing_run_id": run_id,
                "input_fingerprint": fingerprint,
                "adapter": V2D1_RESCUE_ADAPTER,
                "model_manifest": {
                    "settings": settings.to_dict(),
                    "tier": "detected-plus-possible",
                    "diarization_run_id": diarization_run_id,
                    "asr_run_id": asr_run_id,
                },
            },
            rescue_predictions,
        )
        evaluations = _evaluate_prediction_sets(
            database,
            evaluation_truth_set_ids,
            detected_prediction_set_id=int(detected_set["id"]),
            rescue_prediction_set_id=int(rescue_set["id"]),
        )
        summary_payload = {
            "diarization_run_id": diarization_run_id,
            "asr_run_id": asr_run_id,
            "detected_prediction_set_id": int(detected_set["id"]),
            "rescue_prediction_set_id": int(rescue_set["id"]),
            "detected_regions": len(detected),
            "possible_regions": len(possible),
            "evidence_components": len(components),
            "detected_ms": _intervals_ms(detected),
            "possible_ms": _intervals_ms(possible),
            "expanded_ms": _intervals_ms([*detected, *possible]),
            "evaluations": evaluations,
            "speaker_policy": "anonymous speakers preserved independently of source",
        }
        manifest_path = _write_manifest(
            run_id,
            session_id,
            config,
            summary_payload,
            detected,
            possible,
            components,
        )
        database.finish_processing_run(
            run_id,
            status="completed",
            summary=summary_payload,
            artifacts={"manifest": str(manifest_path.resolve())},
        )
        return V2D1Summary(
            run_id=run_id,
            manifest_path=manifest_path,
            **summary_payload,
        )
    except Exception as exc:
        database.finish_processing_run(run_id, status="failed", error=repr(exc))
        raise


def build_v2d1_speech_layers(
    database: Database,
    *,
    diarization_run_id: int,
    asr_run_id: int,
    duration_ms: int,
    settings: V2D1Settings,
) -> tuple[
    list[EvidenceInterval],
    list[EvidenceInterval],
    list[tuple[int, int]],
]:
    detected_evidence: list[EvidenceInterval] = []
    possible_seeds: list[EvidenceInterval] = []
    for row in database.list_diarization_turns(
        diarization_run_id, turn_kind="regular"
    ):
        detected_evidence.append(
            EvidenceInterval(
                int(row["session_start_ms"]),
                int(row["session_end_ms"]),
                "pyannote_regular",
                {"speaker": str(row["speaker_label"])},
            )
        )

    for hypothesis in database.list_asr_hypotheses(asr_run_id, role="primary"):
        raw = _json_object(hypothesis["raw_response_json"])
        analysis_start = int(hypothesis["analysis_start_ms"])
        core_start = int(hypothesis["core_start_ms"])
        core_end = int(hypothesis["core_end_ms"])
        for start_ms, end_ms in raw.get("speech_ranges_ms", []):
            clipped = _clip_interval(
                analysis_start + int(start_ms),
                analysis_start + int(end_ms),
                core_start,
                core_end,
            )
            if clipped:
                detected_evidence.append(
                    EvidenceInterval(
                        *clipped,
                        "v2c_accepted_gate",
                        {"window_index": int(hypothesis["window_index"])},
                    )
                )
        for segment in raw.get("segments", []):
            if not isinstance(segment, dict) or bool(segment.get("accepted")):
                continue
            text = str(segment.get("text") or "").strip()
            aligned = int(segment.get("aligned_token_count") or 0)
            if settings.require_rejected_asr_text and not (text and aligned > 0):
                continue
            clipped = _clip_interval(
                analysis_start + int(segment["core_start_ms"]),
                analysis_start + int(segment["core_end_ms"]),
                core_start,
                core_end,
            )
            if clipped:
                possible_seeds.append(
                    EvidenceInterval(
                        *clipped,
                        "v2c_rejected_with_asr",
                        {
                            "window_index": int(hypothesis["window_index"]),
                            "candidate_index": int(segment["candidate_index"]),
                            "duration_ms": int(segment.get("duration_ms") or 0),
                            "snr_db": float(segment.get("snr_db") or 0.0),
                            "silero_overlap_ms": int(
                                segment.get("silero_overlap_ms") or 0
                            ),
                            "asr_text": text,
                            "acceptance_reasons": list(
                                segment.get("acceptance_reasons") or []
                            ),
                        },
                    )
                )

    detected_ranges = _merge_ranges(
        [(item.start_ms, item.end_ms) for item in detected_evidence]
    )
    seed_ranges = _merge_ranges(
        [
            *detected_ranges,
            *((item.start_ms, item.end_ms) for item in possible_seeds),
        ],
        gap_ms=0,
    )
    components = _merge_ranges(seed_ranges, gap_ms=settings.bridge_gap_ms)
    possible_ranges = _subtract_ranges(components, detected_ranges)

    detected = [
        EvidenceInterval(
            start,
            end,
            "detected",
            {
                "evidence_types": sorted(
                    {
                        item.evidence_type
                        for item in detected_evidence
                        if _intersection_ms(start, end, item.start_ms, item.end_ms) > 0
                    }
                )
            },
        )
        for start, end in detected_ranges
    ]
    possible = [
        EvidenceInterval(
            start,
            end,
            "possible",
            {
                "reason": "rejected-asr-evidence-or-context-bridge",
                "evidence_types": sorted(
                    {
                        item.evidence_type
                        for item in possible_seeds
                        if _distance_ms(start, end, item.start_ms, item.end_ms)
                        <= settings.bridge_gap_ms
                    }
                ),
                "rejected_candidates": [
                    item.metadata
                    for item in possible_seeds
                    if _distance_ms(start, end, item.start_ms, item.end_ms)
                    <= settings.bridge_gap_ms
                ],
            },
        )
        for start, end in possible_ranges
        if end > start
    ]
    return detected, possible, components


def create_source_micro_truth(
    database: Database,
    recording_id: int,
    *,
    name: str,
    start_ms: int,
    end_ms: int,
    speech_source: str,
    notes: Sequence[str] = (),
    output_path: Path | None = None,
) -> ContinuousTruthSummary:
    """Freeze one explicit source observation without assigning speaker identity."""
    if not NAME_PATTERN.fullmatch(name):
        raise ValueError(
            "name must contain only letters, digits, dots, underscores, or hyphens"
        )
    if speech_source not in SPEECH_SOURCE_VALUES:
        raise ValueError(f"unsupported speech source: {speech_source}")
    session = database.get_session_for_recording(recording_id)
    session_id = int(session["id"])
    if start_ms < 0 or end_ms <= start_ms or end_ms > int(session["duration_ms"]):
        raise ValueError("source observation time range is invalid")
    target = output_path or (
        EVALUATION_DIR
        / f"session-{session_id:06d}"
        / f"{name}.jsonl"
    )
    rows = [
        {
            "type": "metadata",
            "format": CONTINUOUS_TRUTH_FORMAT,
            "name": name,
            "session_id": session_id,
            "scope_start_ms": start_ms,
            "scope_end_ms": end_ms,
            "input_fingerprint": database.session_input_fingerprint(session_id),
            "completeness": {
                "vad": "exhaustive",
                "transcript": "none",
                "speaker": "none",
                "identity": "none",
                "overlap": "none",
                "alignment": "none",
                "entities": "none",
            },
            "provenance": {
                "kind": "human_source_micro_truth_v2d1",
                "speaker_policy": "source does not merge distinct anonymous speakers",
                "notes": list(notes),
            },
        },
        {
            "type": "annotation",
            "key": f"{name}:speech:0001",
            "kind": "speech",
            "session_start_ms": start_ms,
            "session_end_ms": end_ms,
            "label": "speech",
            "text": None,
            "metadata": {
                "reviewed": True,
                "speech_source": speech_source,
                "speaker_identity_labeled": False,
            },
        },
    ]
    _write_jsonl_atomically(target, rows)
    truth_sha256 = _sha256_file(target)
    truth_key = f"continuous:{session_id}:{truth_sha256}"
    for row in database.list_truth_sets(session_id):
        if str(row["truth_key"]) == truth_key:
            return ContinuousTruthSummary(
                truth_set_id=int(row["id"]),
                session_id=session_id,
                annotation_count=len(database.list_truth_annotations(int(row["id"]))),
                output_path=target.resolve(),
                truth_sha256=truth_sha256,
            )
    return import_continuous_truth(database, target)


def _prediction_rows(
    run_id: int,
    intervals: Sequence[EvidenceInterval],
    components: Sequence[tuple[int, int]],
    *,
    include_possible: bool,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, interval in enumerate(
        sorted(intervals, key=lambda item: (item.start_ms, item.end_ms))
    ):
        component_index, component = _component_for(
            interval.start_ms, interval.end_ms, components
        )
        tier = interval.evidence_type
        rows.append(
            {
                "prediction_key": (
                    f"v2d1:{run_id}:{'rescue' if include_possible else 'detected'}:"
                    f"{tier}:{index:06d}"
                ),
                "prediction_kind": "speech",
                "session_start_ms": interval.start_ms,
                "session_end_ms": interval.end_ms,
                "label": "speech",
                "text": None,
                "confidence": 0.35 if tier == "possible" else 0.90,
                "metadata": {
                    **interval.metadata,
                    "tier": tier,
                    "component_id": component_index,
                    "component_start_ms": component[0],
                    "component_end_ms": component[1],
                    "speech_source": "unknown",
                    "speaker_policy": "preserve",
                },
            }
        )
    return rows


def _evaluate_prediction_sets(
    database: Database,
    truth_set_ids: Sequence[int],
    *,
    detected_prediction_set_id: int,
    rescue_prediction_set_id: int,
) -> dict[str, Any]:
    evaluations: dict[str, Any] = {}
    for truth_set_id in dict.fromkeys(int(value) for value in truth_set_ids):
        entry: dict[str, Any] = {}
        for label, prediction_set_id in (
            ("detected", detected_prediction_set_id),
            ("recall_rescue", rescue_prediction_set_id),
        ):
            try:
                result = evaluate_benchmark(
                    database, truth_set_id, prediction_set_id
                )
                entry[label] = {
                    "benchmark_run_id": result.benchmark_run_id,
                    "prediction_set_id": prediction_set_id,
                    "vad": result.metrics["vad"],
                }
            except (KeyError, RuntimeError, ValueError) as exc:
                entry[label] = {
                    "prediction_set_id": prediction_set_id,
                    "error": repr(exc),
                }
        evaluations[str(truth_set_id)] = entry
    return evaluations


def _write_manifest(
    run_id: int,
    session_id: int,
    config: dict[str, Any],
    summary: dict[str, Any],
    detected: Sequence[EvidenceInterval],
    possible: Sequence[EvidenceInterval],
    components: Sequence[tuple[int, int]],
) -> Path:
    directory = OUTPUT_DIR / f"session-{session_id:06d}" / "diarization-v2d1"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"run-{run_id:06d}.json"
    payload = {
        "format": "AllDayRecording V2-D.1 evidence manifest v1",
        "run_id": run_id,
        "session_id": session_id,
        "config": config,
        "summary": summary,
        "detected": [asdict(item) for item in detected],
        "possible": [asdict(item) for item in possible],
        "components": [
            {"component_id": index, "start_ms": start, "end_ms": end}
            for index, (start, end) in enumerate(components)
        ],
    }
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return path


def _component_for(
    start_ms: int,
    end_ms: int,
    components: Sequence[tuple[int, int]],
) -> tuple[int, tuple[int, int]]:
    for index, component in enumerate(components):
        if component[0] <= start_ms and component[1] >= end_ms:
            return index, component
    raise RuntimeError("speech evidence interval is outside every component")


def _merge_ranges(
    ranges: Iterable[tuple[int, int]], *, gap_ms: int = 0
) -> list[tuple[int, int]]:
    ordered = sorted((start, end) for start, end in ranges if end > start)
    if not ordered:
        return []
    merged = [list(ordered[0])]
    for start, end in ordered[1:]:
        if start - merged[-1][1] <= gap_ms:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def _subtract_ranges(
    ranges: Sequence[tuple[int, int]],
    remove: Sequence[tuple[int, int]],
) -> list[tuple[int, int]]:
    output: list[tuple[int, int]] = []
    for start, end in ranges:
        cursor = start
        for remove_start, remove_end in remove:
            if remove_end <= cursor:
                continue
            if remove_start >= end:
                break
            if remove_start > cursor:
                output.append((cursor, min(end, remove_start)))
            cursor = max(cursor, remove_end)
            if cursor >= end:
                break
        if cursor < end:
            output.append((cursor, end))
    return output


def _clip_interval(
    start_ms: int, end_ms: int, minimum_ms: int, maximum_ms: int
) -> tuple[int, int] | None:
    start = max(start_ms, minimum_ms)
    end = min(end_ms, maximum_ms)
    return (start, end) if end > start else None


def _intersection_ms(start_a: int, end_a: int, start_b: int, end_b: int) -> int:
    return max(0, min(end_a, end_b) - max(start_a, start_b))


def _distance_ms(start_a: int, end_a: int, start_b: int, end_b: int) -> int:
    if _intersection_ms(start_a, end_a, start_b, end_b) > 0:
        return 0
    return min(abs(start_a - end_b), abs(start_b - end_a))


def _intervals_ms(intervals: Sequence[EvidenceInterval]) -> int:
    return sum(end - start for start, end in _merge_ranges(
        (item.start_ms, item.end_ms) for item in intervals
    ))


def _json_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    parsed = json.loads(value)
    return parsed if isinstance(parsed, dict) else {}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_jsonl_atomically(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            "\n".join(
                json.dumps(row, ensure_ascii=False, sort_keys=True)
                for row in rows
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
