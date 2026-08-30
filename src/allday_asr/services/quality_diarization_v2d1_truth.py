from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from allday_asr.paths import EVALUATION_DIR
from allday_asr.services.benchmark import (
    CONTINUOUS_TRUTH_FORMAT,
    evaluate_benchmark,
    import_continuous_truth,
)
from allday_asr.services.quality_diarization_v2d1_review import (
    v2d1_review_overview,
)
from allday_asr.storage.database import Database

TRUTH_PROVENANCE_KIND = "v2d1_candidate_review_truth_v1"


def create_v2d1_review_truth(
    database: Database,
    run_id: int,
    *,
    include_identities: bool = False,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    """Freeze D.1 human decisions as truth over exactly the reviewed intervals."""
    run = _require_v2d1_run(database, run_id)
    session_id = int(run["session_id"])
    review = v2d1_review_overview(database, run_id)
    if not review["completed"]:
        raise ValueError("请先完成全部 V2-D.1 可能语音审核")

    reviews = [
        row
        for row in database.list_v2d1_candidate_reviews(run_id)
        if str(row["status"]) in {"confirmed_speech", "rejected"}
    ]
    if not reviews:
        raise ValueError("没有可用于评测的明确人工判断；听不清区间不会进入真值")
    identity_rows = {
        str(row["candidate_id"]): row
        for row in database.list_v2d1_identity_labels(run_id)
        if str(row["review_status"]) == "confirmed_speech"
    }
    confirmed = [
        row for row in reviews if str(row["status"]) == "confirmed_speech"
    ]
    diarization_run_id = int(run["parent_run_id"] or 0)
    manual_identity_rows = (
        database.list_manual_identity_annotations(
            session_id, diarization_run_id=diarization_run_id
        )
        if include_identities and diarization_run_id
        else []
    )
    if include_identities:
        missing = [
            str(row["candidate_id"])
            for row in confirmed
            if str(row["candidate_id"]) not in identity_rows
        ]
        if missing:
            raise ValueError(f"还有 {len(missing)} 条确认语音没有人物标签")
        if not confirmed and not manual_identity_rows:
            raise ValueError("没有可用的人工人物区间，无法生成人工身份真值")

    review_ranges = _merge_ranges(
        (int(row["session_start_ms"]), int(row["session_end_ms"]))
        for row in reviews
    )
    speech_ranges = _merge_ranges(
        (int(row["session_start_ms"]), int(row["session_end_ms"]))
        for row in confirmed
    )
    annotations: list[dict[str, Any]] = []
    for index, (start_ms, end_ms) in enumerate(review_ranges):
        annotations.append(
            _annotation(
                f"review-region:{index:04d}",
                "uncertain",
                start_ms,
                end_ms,
                label="review_region_complete_scope",
                metadata={
                    "source": "V2-D.1 human review",
                    "exhaustive_vad_within_region": True,
                },
            )
        )
    for index, (start_ms, end_ms) in enumerate(speech_ranges):
        annotations.append(
            _annotation(
                f"speech:{index:04d}",
                "speech",
                start_ms,
                end_ms,
                metadata={"source": "confirmed_speech", "human_reviewed": True},
            )
        )
    if include_identities:
        for index, row in enumerate(confirmed):
            candidate_id = str(row["candidate_id"])
            annotations.append(
                _annotation(
                    f"speaker:{index:04d}:{candidate_id}",
                    "speaker",
                    int(row["session_start_ms"]),
                    int(row["session_end_ms"]),
                    label=str(identity_rows[candidate_id]["identity_label"]),
                    metadata={
                        "source": "V2-D.1 confirmed speech identity overlay",
                        "candidate_id": candidate_id,
                        "human_reviewed": True,
                        "audit_only": True,
                    },
                )
            )
        for row in manual_identity_rows:
            annotations.append(
                _annotation(
                    f"manual-speaker:{int(row['id']):06d}",
                    "speaker",
                    int(row["session_start_ms"]),
                    int(row["session_end_ms"]),
                    label=str(row["identity_label"]),
                    metadata={
                        "source": "manual identity sampling overlay",
                        "manual_identity_annotation_id": int(row["id"]),
                        "anonymous_speaker_label": str(
                            row["anonymous_speaker_label"]
                        ),
                        "human_reviewed": True,
                        "audit_only": True,
                    },
                )
            )

    combined_identity_counts = dict(review["identity_counts"])
    for row in manual_identity_rows:
        identity = str(row["identity_label"])
        combined_identity_counts[identity] = (
            combined_identity_counts.get(identity, 0) + 1
        )
    bounding_ranges = [
        *review_ranges,
        *[
            (int(row["session_start_ms"]), int(row["session_end_ms"]))
            for row in manual_identity_rows
        ],
    ]

    mode = "identity" if include_identities else "vad"
    name = f"v2d1-review-{run_id}-{mode}"
    metadata = {
        "type": "metadata",
        "format": CONTINUOUS_TRUTH_FORMAT,
        "name": name,
        "session_id": session_id,
        "scope_start_ms": min(start for start, _ in bounding_ranges),
        "scope_end_ms": max(end for _, end in bounding_ranges),
        "coverage_semantics": "exhaustive_within_review_regions",
        "input_fingerprint": database.session_input_fingerprint(session_id),
        "completeness": {
            "vad": "exhaustive",
            "transcript": "none",
            "speaker": "sparse" if include_identities else "none",
            "identity": "sparse" if include_identities else "none",
            "overlap": "none",
            "alignment": "none",
            "entities": "none",
        },
        "provenance": {
            "kind": TRUTH_PROVENANCE_KIND,
            "v2d1_run_id": run_id,
            "include_identities": include_identities,
            "review_completed_at": review["completed_at"],
            "review_status_counts": review["status_counts"],
            "identity_counts": combined_identity_counts if include_identities else {},
            "manual_identity_annotation_ids": (
                [int(row["id"]) for row in manual_identity_rows]
                if include_identities
                else []
            ),
            "model_outputs_used_for_selection": True,
            "claim_limit": (
                "Only D.1 model-selected intervals with a definite human decision; "
                "uncertain decisions are excluded."
            ),
            "original_model_output_mutated": False,
        },
    }
    rows = [metadata, *annotations]
    serialized = _serialize_jsonl(rows)
    truth_sha256 = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    target_root = (
        output_dir.resolve()
        if output_dir is not None
        else EVALUATION_DIR / f"session-{session_id:06d}" / "v2d1-human-review"
    )
    target = target_root / f"run-{run_id:06d}-{mode}-{truth_sha256[:12]}.jsonl"
    if not target.is_file():
        _write_text_atomically(target, serialized)

    existing = next(
        (
            row
            for row in database.list_truth_sets(session_id)
            if str(row["truth_sha256"]) == truth_sha256
        ),
        None,
    )
    if existing is None:
        imported = import_continuous_truth(database, target)
        truth_set_id = imported.truth_set_id
    else:
        truth_set_id = int(existing["id"])
    return {
        "truth_set_id": truth_set_id,
        "name": name,
        "path": str(target.resolve()),
        "truth_sha256": truth_sha256,
        "include_identities": include_identities,
        "evaluation_region_count": len(review_ranges),
        "evaluated_duration_ms": _ranges_ms(review_ranges),
        "speech_duration_ms": _ranges_ms(speech_ranges),
        "identity_region_count": (
            len(confirmed) + len(manual_identity_rows)
            if include_identities
            else 0
        ),
        "identity_counts": combined_identity_counts if include_identities else {},
    }


def evaluate_v2d1_review(
    database: Database,
    run_id: int,
    *,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    truth = create_v2d1_review_truth(
        database, run_id, include_identities=False, output_dir=output_dir
    )
    run = _require_v2d1_run(database, run_id)
    summary = _json_object(run["summary_json"])
    prediction_sets = {
        "detected": int(summary.get("detected_prediction_set_id") or 0),
        "recall_rescue": int(summary.get("rescue_prediction_set_id") or 0),
    }
    if not all(prediction_sets.values()):
        raise ValueError("V2-D.1 run 缺少可评测的预测快照")
    benchmarks: dict[str, Any] = {}
    existing = database.list_benchmark_runs(int(truth["truth_set_id"]))
    for label, prediction_set_id in prediction_sets.items():
        row = next(
            (
                value
                for value in reversed(existing)
                if int(value["prediction_set_id"]) == prediction_set_id
            ),
            None,
        )
        if row is None:
            result = evaluate_benchmark(
                database, int(truth["truth_set_id"]), prediction_set_id
            )
            benchmark_run_id = result.benchmark_run_id
            metrics = result.metrics
        else:
            benchmark_run_id = int(row["id"])
            metrics = _json_object(row["metrics_json"])
        prediction = database.get_benchmark_prediction_set(prediction_set_id)
        benchmarks[label] = {
            "benchmark_run_id": benchmark_run_id,
            "prediction_set_id": prediction_set_id,
            "prediction_name": str(prediction["name"]),
            "vad": metrics.get("vad") or {},
        }
    return {"available": True, "truth": truth, "benchmarks": benchmarks}


def v2d1_review_evaluation_overview(
    database: Database, session_id: int
) -> dict[str, Any]:
    run = _latest_v2d1_run(database, session_id)
    if run is None:
        return {
            "available": False,
            "can_create": False,
            "reason": "当前会话还没有已完成的 V2-D.1 结果。",
        }
    run_id = int(run["id"])
    review = v2d1_review_overview(database, run_id)
    truth_set = None
    for row in reversed(database.list_truth_sets(session_id)):
        provenance = _json_object(row["provenance_json"])
        if (
            provenance.get("kind") == TRUTH_PROVENANCE_KIND
            and int(provenance.get("v2d1_run_id") or 0) == run_id
            and provenance.get("include_identities") is False
        ):
            truth_set = row
            break
    can_create = bool(
        review["completed"]
        and review["status_counts"]["confirmed_speech"]
        + review["status_counts"]["rejected"]
        > 0
    )
    if truth_set is None:
        return {
            "available": False,
            "can_create": can_create,
            "reason": (
                "D.1 人工检查已完成，可以生成连续 VAD 真值。"
                if can_create
                else "请先完成 D.1 可能语音的人工检查。"
            ),
            "v2d1_run_id": run_id,
            "review": review,
        }
    truth = _truth_payload(database, truth_set)
    summary = _json_object(run["summary_json"])
    expected = {
        "detected": int(summary.get("detected_prediction_set_id") or 0),
        "recall_rescue": int(summary.get("rescue_prediction_set_id") or 0),
    }
    benchmarks: dict[str, Any] = {}
    benchmark_rows = database.list_benchmark_runs(int(truth_set["id"]))
    for label, prediction_set_id in expected.items():
        row = next(
            (
                value
                for value in reversed(benchmark_rows)
                if int(value["prediction_set_id"]) == prediction_set_id
            ),
            None,
        )
        if row is None:
            continue
        metrics = _json_object(row["metrics_json"])
        benchmarks[label] = {
            "benchmark_run_id": int(row["id"]),
            "prediction_set_id": prediction_set_id,
            "prediction_name": str(row["prediction_name"]),
            "vad": metrics.get("vad") or {},
        }
    return {
        "available": bool(benchmarks),
        "can_create": can_create,
        "v2d1_run_id": run_id,
        "review": review,
        "truth": truth,
        "benchmarks": benchmarks,
        "reason": None if benchmarks else "真值已冻结，尚未生成 VAD 对比报告。",
    }


def _truth_payload(database: Database, truth_set: Any) -> dict[str, Any]:
    annotations = database.list_truth_annotations(int(truth_set["id"]))
    review_ranges = [
        (int(row["session_start_ms"]), int(row["session_end_ms"]))
        for row in annotations
        if str(row["annotation_kind"]) == "uncertain"
        and str(row["label"] or "") == "review_region_complete_scope"
    ]
    speech_ranges = [
        (int(row["session_start_ms"]), int(row["session_end_ms"]))
        for row in annotations
        if str(row["annotation_kind"]) == "speech"
    ]
    return {
        "truth_set_id": int(truth_set["id"]),
        "name": str(truth_set["name"]),
        "path": str(truth_set["truth_path"]),
        "truth_sha256": str(truth_set["truth_sha256"]),
        "include_identities": False,
        "evaluation_region_count": len(review_ranges),
        "evaluated_duration_ms": _ranges_ms(review_ranges),
        "speech_duration_ms": _ranges_ms(speech_ranges),
        "identity_region_count": 0,
        "identity_counts": {},
    }


def _latest_v2d1_run(database: Database, session_id: int):
    matches = [
        row
        for row in database.list_session_processing_runs(session_id)
        if str(row["run_kind"]) == "quality_diarization_v2d1"
        and str(row["status"]) == "completed"
    ]
    return matches[-1] if matches else None


def _require_v2d1_run(database: Database, run_id: int):
    run = database.get_processing_run(run_id)
    if str(run["run_kind"]) != "quality_diarization_v2d1":
        raise ValueError("连续审核真值只适用于 V2-D.1 run")
    if str(run["status"]) != "completed" or run["session_id"] is None:
        raise ValueError("V2-D.1 run 尚未完成或没有录音会话")
    return run


def _annotation(
    key: str,
    kind: str,
    start_ms: int,
    end_ms: int,
    *,
    label: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "type": "annotation",
        "key": key,
        "kind": kind,
        "session_start_ms": start_ms,
        "session_end_ms": end_ms,
        "label": label,
        "text": None,
        "metadata": metadata or {},
    }


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


def _serialize_jsonl(rows: Sequence[dict[str, Any]]) -> str:
    return "\n".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows
    ) + "\n"


def _write_text_atomically(path: Path, value: str) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(value, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _json_object(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    parsed = json.loads(str(value))
    return parsed if isinstance(parsed, dict) else {}
