from __future__ import annotations

import json
import os
import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from allday_asr.domain.hashing import canonical_json_sha256 as _sha256_mapping
from allday_asr.paths import OUTPUT_DIR
from allday_asr.storage.database import Database

V2D2_CONTAMINATION_MIN_MS = 250
V2D2_CONTAMINATION_MIN_SHARE = 0.05


@dataclass(frozen=True)
class V2D2Summary:
    run_id: int
    diarization_run_id: int
    truth_set_id: int
    truth_name: str
    annotation_regions: int
    reviewed_truth_ms: int
    covered_truth_ms: int
    coverage: float
    human_speakers: list[dict[str, Any]]
    model_speakers: list[dict[str, Any]]
    contaminated_speakers: list[str]
    manifest_path: Path


def run_identity_contamination_audit(
    database: Database,
    recording_id: int | None,
    *,
    session_id: int | None = None,
    diarization_run_id: int,
    truth_set_id: int,
) -> V2D2Summary:
    """Audit sparse human identities without relabeling model speaker turns."""
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
        raise ValueError("V2-D.2 requires a V2-D diarization parent run")
    if str(diarization_run["status"]) != "completed":
        raise ValueError("V2-D.2 requires a completed V2-D parent run")

    truth_set = database.get_truth_set(truth_set_id)
    if str(truth_set["status"]) != "frozen":
        raise ValueError("V2-D.2 requires a frozen truth set")
    if int(truth_set["session_id"]) != session_id:
        raise ValueError("identity truth belongs to a different recording session")
    annotations = [
        row
        for row in database.list_truth_annotations(
            truth_set_id, annotation_kind="speaker"
        )
        if str(row["label"] or "").strip()
    ]
    if not annotations:
        raise ValueError("truth set has no human speaker annotations")

    config = {
        "diarization_run_id": diarization_run_id,
        "truth_set_id": truth_set_id,
        "truth_sha256": str(truth_set["truth_sha256"]),
        "turn_kind": "exclusive",
        "contamination_min_ms": V2D2_CONTAMINATION_MIN_MS,
        "contamination_min_share": V2D2_CONTAMINATION_MIN_SHARE,
        "identity_policy": (
            "human truth is an audit overlay; never globally rename or merge model speakers"
        ),
    }
    run_id = database.start_processing_run(
        recording_id,
        session_id=session_id,
        run_kind="quality_diarization_v2d2",
        config=config,
        config_sha256=_sha256_mapping(config),
        model_manifest={
            "kind": "human-truth-contamination-audit-no-identity-model",
            "diarization_run_id": diarization_run_id,
            "truth_set_id": truth_set_id,
            "leakage_policy": "truth is audit-only and is not a model prediction",
        },
        pipeline_version="v2-d.2",
        parent_run_id=diarization_run_id,
    )
    try:
        exclusive_turns = database.list_diarization_turns(
            diarization_run_id, turn_kind="exclusive"
        )
        human_speakers, model_speakers = build_identity_contamination_matrix(
            annotations,
            exclusive_turns,
        )
        reviewed_ranges = _merge_ranges(
            (int(row["session_start_ms"]), int(row["session_end_ms"]))
            for row in annotations
        )
        covered_ranges = _covered_truth_ranges(annotations, exclusive_turns)
        reviewed_truth_ms = _ranges_ms(reviewed_ranges)
        covered_truth_ms = _ranges_ms(covered_ranges)
        contaminated = [
            str(item["speaker"])
            for item in model_speakers
            if bool(item["contaminated"])
        ]
        summary_payload = {
            "diarization_run_id": diarization_run_id,
            "truth_set_id": truth_set_id,
            "truth_name": str(truth_set["name"]),
            "annotation_regions": len(annotations),
            "reviewed_truth_ms": reviewed_truth_ms,
            "covered_truth_ms": covered_truth_ms,
            "coverage": covered_truth_ms / reviewed_truth_ms,
            "human_speakers": human_speakers,
            "model_speakers": model_speakers,
            "contaminated_speakers": contaminated,
            "identity_policy": (
                "audit-only interval evidence; model speaker labels remain immutable"
            ),
        }
        manifest_path = _write_manifest(
            run_id,
            session_id,
            config,
            summary_payload,
            annotations,
        )
        database.finish_processing_run(
            run_id,
            status="completed",
            summary=summary_payload,
            artifacts={"manifest": str(manifest_path.resolve())},
        )
        return V2D2Summary(
            run_id=run_id,
            manifest_path=manifest_path,
            **{
                key: value
                for key, value in summary_payload.items()
                if key != "identity_policy"
            },
        )
    except Exception as exc:
        database.finish_processing_run(run_id, status="failed", error=repr(exc))
        raise


def build_identity_contamination_matrix(
    annotations: Sequence[Any],
    exclusive_turns: Sequence[Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    human_ranges: dict[str, list[tuple[int, int]]] = defaultdict(list)
    model_ranges: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for row in annotations:
        human_ranges[str(row["label"])].append(
            (int(row["session_start_ms"]), int(row["session_end_ms"]))
        )
    for row in exclusive_turns:
        model_ranges[str(row["speaker_label"])].append(
            (int(row["session_start_ms"]), int(row["session_end_ms"]))
        )
    human_ranges = {
        label: _merge_ranges(ranges) for label, ranges in human_ranges.items()
    }
    model_ranges = {
        label: _merge_ranges(ranges) for label, ranges in model_ranges.items()
    }

    matrix: dict[tuple[str, str], int] = {}
    for human_label, truth_ranges in human_ranges.items():
        for model_label, turn_ranges in model_ranges.items():
            overlap_ms = _ranges_intersection_ms(truth_ranges, turn_ranges)
            if overlap_ms:
                matrix[(human_label, model_label)] = overlap_ms

    human_payload: list[dict[str, Any]] = []
    for human_label, truth_ranges in human_ranges.items():
        truth_ms = _ranges_ms(truth_ranges)
        overlaps = {
            model_label: matrix.get((human_label, model_label), 0)
            for model_label in model_ranges
            if matrix.get((human_label, model_label), 0) > 0
        }
        covered_ms = _ranges_intersection_ms(
            truth_ranges,
            _merge_ranges(
                value for ranges in model_ranges.values() for value in ranges
            ),
        )
        ordered = sorted(overlaps.items(), key=lambda item: (-item[1], item[0]))
        human_payload.append(
            {
                "identity": human_label,
                "truth_ms": truth_ms,
                "covered_ms": covered_ms,
                "coverage": covered_ms / truth_ms,
                "model_speakers": [
                    {
                        "speaker": model_label,
                        "overlap_ms": overlap_ms,
                        "share_of_identity": overlap_ms / truth_ms,
                    }
                    for model_label, overlap_ms in ordered
                ],
                "fragmented": len(
                    [value for value in overlaps.values() if value >= 250]
                )
                > 1,
            }
        )

    model_payload: list[dict[str, Any]] = []
    for model_label in sorted(model_ranges):
        identities = {
            human_label: matrix.get((human_label, model_label), 0)
            for human_label in human_ranges
            if matrix.get((human_label, model_label), 0) > 0
        }
        reviewed_ms = sum(identities.values())
        if not reviewed_ms:
            continue
        ordered = sorted(identities.items(), key=lambda item: (-item[1], item[0]))
        material = [
            (label, overlap_ms)
            for label, overlap_ms in ordered
            if overlap_ms >= V2D2_CONTAMINATION_MIN_MS
            and overlap_ms / reviewed_ms >= V2D2_CONTAMINATION_MIN_SHARE
        ]
        model_payload.append(
            {
                "speaker": model_label,
                "reviewed_ms": reviewed_ms,
                "dominant_identity": ordered[0][0],
                "purity": ordered[0][1] / reviewed_ms,
                "identities": [
                    {
                        "identity": human_label,
                        "overlap_ms": overlap_ms,
                        "share_of_reviewed": overlap_ms / reviewed_ms,
                    }
                    for human_label, overlap_ms in ordered
                ],
                "contaminated": len(material) > 1,
            }
        )
    human_payload.sort(key=lambda item: (-item["truth_ms"], item["identity"]))
    model_payload.sort(key=lambda item: (-item["reviewed_ms"], item["speaker"]))
    return human_payload, model_payload


def _covered_truth_ranges(
    annotations: Sequence[Any], exclusive_turns: Sequence[Any]
) -> list[tuple[int, int]]:
    truth_ranges = _merge_ranges(
        (int(row["session_start_ms"]), int(row["session_end_ms"]))
        for row in annotations
    )
    turn_ranges = _merge_ranges(
        (int(row["session_start_ms"]), int(row["session_end_ms"]))
        for row in exclusive_turns
    )
    output: list[tuple[int, int]] = []
    for truth_start, truth_end in truth_ranges:
        for turn_start, turn_end in turn_ranges:
            start = max(truth_start, turn_start)
            end = min(truth_end, turn_end)
            if end > start:
                output.append((start, end))
    return _merge_ranges(output)


def _write_manifest(
    run_id: int,
    session_id: int,
    config: dict[str, Any],
    summary: dict[str, Any],
    annotations: Sequence[Any],
) -> Path:
    directory = OUTPUT_DIR / f"session-{session_id:06d}" / "diarization-v2d2"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"run-{run_id:06d}.json"
    payload = {
        "format": "AllDayRecording V2-D.2 identity contamination audit v1",
        "run_id": run_id,
        "session_id": session_id,
        "config": config,
        "summary": summary,
        "identity_truth_regions": [
            {
                "annotation_id": int(row["id"]),
                "start_ms": int(row["session_start_ms"]),
                "end_ms": int(row["session_end_ms"]),
                "identity": str(row["label"]),
                "metadata": _json_object(row["metadata_json"]),
            }
            for row in annotations
        ],
    }
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return path


def _ranges_intersection_ms(
    left: Sequence[tuple[int, int]], right: Sequence[tuple[int, int]]
) -> int:
    total = 0
    left_index = 0
    right_index = 0
    while left_index < len(left) and right_index < len(right):
        left_start, left_end = left[left_index]
        right_start, right_end = right[right_index]
        total += max(0, min(left_end, right_end) - max(left_start, right_start))
        if left_end <= right_end:
            left_index += 1
        else:
            right_index += 1
    return total


def _merge_ranges(ranges: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    ordered = sorted((start, end) for start, end in ranges if end > start)
    if not ordered:
        return []
    merged = [list(ordered[0])]
    for start, end in ordered[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def _ranges_ms(ranges: Sequence[tuple[int, int]]) -> int:
    return sum(end - start for start, end in ranges)


def _json_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    parsed = json.loads(value)
    return parsed if isinstance(parsed, dict) else {}

