from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Sequence
from typing import Any

from allday_asr.diarization.quality_backends import SpeakerTurn
from allday_asr.services.quality_diarization import compute_overlap_regions
from allday_asr.storage.database import Database

SPEAKER_COLORS = (
    "#3f8f91",
    "#8172c6",
    "#d58b32",
    "#c95f56",
    "#3c946d",
    "#5f7fa9",
    "#a56c91",
    "#7b8760",
)
CONVERSATION_WINDOW_MS = 90_000
CONVERSATION_STEP_MS = 30_000
MAX_AUDIO_WINDOW_MS = 120_000


def speaker_timeline_overview(
    database: Database, recording_id: int, *, run_id: int | None = None
) -> dict[str, Any]:
    """Build ranked, read-only listening queues from one completed V2-D run."""
    recording = database.get_recording(recording_id)
    run = _resolve_run(database, recording_id, run_id)
    if run is None:
        return {
            "available": False,
            "recording_id": recording_id,
            "duration_ms": int(recording["duration_ms"]),
            "reason": "这条录音还没有已完成的 V2-D 说话人结果。",
        }

    regular_rows = database.list_diarization_turns(int(run["id"]), turn_kind="regular")
    exclusive_rows = database.list_diarization_turns(
        int(run["id"]), turn_kind="exclusive"
    )
    tokens = _attributed_tokens(database, run)
    regular = [_speaker_turn(row) for row in regular_rows]
    overlaps = compute_overlap_regions(regular)
    duration_ms = int(recording["duration_ms"])
    speakers = _speaker_summaries(regular_rows)

    conversation = _conversation_candidates(
        exclusive_rows, tokens, overlaps, duration_ms
    )
    overlap_candidates = _overlap_candidates(overlaps, tokens, duration_ms)
    unassigned = _unassigned_candidates(tokens, duration_ms)
    v2d1_run = _latest_v2d1_run(database, recording_id, int(run["id"]))
    v2d1_summary = _json_object(v2d1_run["summary_json"]) if v2d1_run else {}
    rescue_rows = _v2d1_prediction_rows(database, v2d1_summary)
    possible = _possible_candidates(rescue_rows, tokens, duration_ms)
    v2d2_run = _latest_v2d2_run(database, recording_id, int(run["id"]))
    v2d2_summary = _json_object(v2d2_run["summary_json"]) if v2d2_run else {}
    identity_regions = _identity_truth_regions(
        database,
        int(v2d2_summary.get("truth_set_id") or 0),
        start_ms=0,
        end_ms=duration_ms,
    )
    for queue in (conversation, overlap_candidates, unassigned, possible):
        _enrich_candidates_with_identity(queue, identity_regions)
    audits_by_speaker = {
        str(item["speaker"]): item
        for item in v2d2_summary.get("model_speakers") or []
    }
    for speaker in speakers:
        speaker["identity_audit"] = audits_by_speaker.get(speaker["label"])
    summary = _json_object(run["summary_json"])
    model = _json_object(run["model_manifest_json"])
    return {
        "available": True,
        "recording_id": recording_id,
        "duration_ms": duration_ms,
        "run": {
            "id": int(run["id"]),
            "status": str(run["status"]),
            "completed_at": run["completed_at"],
            "model_id": model.get("model_id"),
            "model_revision": model.get("model_revision"),
            "backend": model.get("backend"),
            "privacy": model.get("privacy"),
        },
        "summary": summary,
        "speech_ms": _union_ms(
            (
                int(row["session_start_ms"]),
                int(row["session_end_ms"]),
            )
            for row in exclusive_rows
        ),
        "speakers": speakers,
        "queues": {
            "conversation": {
                "label": "多人对话",
                "description": "按说话人切换、语音密度、文字和重叠自动排序；优先找吃饭时的长对话。",
                "count": len(conversation),
                "items": conversation,
            },
            "overlap": {
                "label": "重叠说话",
                "description": "模型检测到至少两个人同时发声的区域。",
                "count": len(overlap_candidates),
                "total_ms": sum(end - start for start, end, _ in overlaps),
                "items": overlap_candidates,
            },
            "unassigned": {
                "label": "无归属文字",
                "description": "ASR 有文字，但说话人模型没有可靠归属；相邻词已合并，避免逐词试听。",
                "count": len(unassigned),
                "token_count": sum(
                    1 for token in tokens if token["primary_kind"] == "none"
                ),
                "items": unassigned,
            },
            "possible": {
                "label": "可能漏检",
                "description": (
                    "琥珀色是低置信召回补救：有被门控拒绝的 ASR 证据，或位于密集语音的短缺口。"
                    "它不是正式说话人结果，需要试听确认。"
                ),
                "count": len(possible),
                "total_ms": sum(item["possible_ms"] for item in possible),
                "items": possible,
            },
        },
        "v2d1": _v2d1_overview(v2d1_run, v2d1_summary),
        "v2d2": _v2d2_overview(v2d2_run, v2d2_summary),
        "method": {
            "conversation_window_ms": CONVERSATION_WINDOW_MS,
            "conversation_step_ms": CONVERSATION_STEP_MS,
            "original_audio_immutable": True,
            "listening_audio": "从永久原音按需生成响度归一化缓存，不修改原文件。",
            "speaker_policy": "来源层与匿名 speaker 独立；不会因同属电视而合并不同节目人物。",
            "identity_policy": "人工身份只做短区间审计，不把整个匿名簇重命名为人物。",
        },
    }


def speaker_timeline_window(
    database: Database,
    recording_id: int,
    *,
    run_id: int,
    start_ms: int,
    end_ms: int,
) -> dict[str, Any]:
    run = _require_run(database, recording_id, run_id)
    recording = database.get_recording(recording_id)
    duration_ms = int(recording["duration_ms"])
    _validate_window(start_ms, end_ms, duration_ms)

    regular_rows = database.list_diarization_turns(run_id, turn_kind="regular")
    turns = [
        _turn_payload(row, start_ms, end_ms)
        for row in regular_rows
        if int(row["session_end_ms"]) > start_ms
        and int(row["session_start_ms"]) < end_ms
    ]
    overlaps = [
        {
            "start_ms": max(start_ms, overlap_start),
            "end_ms": min(end_ms, overlap_end),
            "speakers": list(labels),
        }
        for overlap_start, overlap_end, labels in compute_overlap_regions(
            [_speaker_turn(row) for row in regular_rows]
        )
        if overlap_end > start_ms and overlap_start < end_ms
    ]
    tokens = [
        token
        for token in _attributed_tokens(database, run)
        if token["end_ms"] > start_ms and token["start_ms"] < end_ms
    ]
    v2d1_run = _latest_v2d1_run(database, recording_id, run_id)
    v2d1_summary = _json_object(v2d1_run["summary_json"]) if v2d1_run else {}
    speech_evidence = [
        _evidence_payload(row, start_ms, end_ms)
        for row in _v2d1_prediction_rows(database, v2d1_summary)
        if int(row["session_end_ms"]) > start_ms
        and int(row["session_start_ms"]) < end_ms
    ]
    source_regions = _source_truth_regions(
        database,
        int(run["session_id"]),
        start_ms=start_ms,
        end_ms=end_ms,
    )
    v2d2_run = _latest_v2d2_run(database, recording_id, run_id)
    v2d2_summary = _json_object(v2d2_run["summary_json"]) if v2d2_run else {}
    identity_regions = _identity_truth_regions(
        database,
        int(v2d2_summary.get("truth_set_id") or 0),
        start_ms=start_ms,
        end_ms=end_ms,
    )
    for turn in turns:
        turn["identity_evidence"] = _identity_evidence(
            int(turn["start_ms"]), int(turn["end_ms"]), identity_regions
        )
    for token in tokens:
        token["identity_evidence"] = _identity_evidence(
            int(token["start_ms"]), int(token["end_ms"]), identity_regions
        )
    return {
        "recording_id": recording_id,
        "run_id": run_id,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "duration_ms": end_ms - start_ms,
        "turns": turns,
        "overlaps": overlaps,
        "tokens": tokens,
        "speech_evidence": speech_evidence,
        "source_regions": source_regions,
        "identity_regions": identity_regions,
        "v2d2": _v2d2_overview(v2d2_run, v2d2_summary),
        "audio_url": (
            "/api/speaker-timeline/audio"
            f"?recording_id={recording_id}&start_ms={start_ms}&end_ms={end_ms}&v=1"
        ),
    }


def _latest_v2d1_run(
    database: Database, recording_id: int, diarization_run_id: int
):
    matches = [
        row
        for row in database.list_processing_runs(recording_id)
        if str(row["run_kind"]) == "quality_diarization_v2d1"
        and str(row["status"]) == "completed"
        and int(row["parent_run_id"] or 0) == diarization_run_id
    ]
    return matches[-1] if matches else None


def _latest_v2d2_run(
    database: Database, recording_id: int, diarization_run_id: int
):
    matches = [
        row
        for row in database.list_processing_runs(recording_id)
        if str(row["run_kind"]) == "quality_diarization_v2d2"
        and str(row["status"]) == "completed"
        and int(row["parent_run_id"] or 0) == diarization_run_id
    ]
    return matches[-1] if matches else None


def _v2d1_prediction_rows(
    database: Database, summary: dict[str, Any]
) -> list[Any]:
    prediction_set_id = int(summary.get("rescue_prediction_set_id") or 0)
    if not prediction_set_id:
        return []
    return database.list_benchmark_predictions(
        prediction_set_id, prediction_kind="speech"
    )


def _v2d1_overview(run, summary: dict[str, Any]) -> dict[str, Any]:
    if run is None:
        return {
            "available": False,
            "reason": "尚未生成 V2-D.1 双层语音证据。",
            "speaker_policy": "来源层不会合并匿名说话人。",
        }
    config = _json_object(run["config_json"])
    return {
        "available": True,
        "run_id": int(run["id"]),
        "detected_regions": int(summary.get("detected_regions") or 0),
        "possible_regions": int(summary.get("possible_regions") or 0),
        "detected_ms": int(summary.get("detected_ms") or 0),
        "possible_ms": int(summary.get("possible_ms") or 0),
        "bridge_gap_ms": int(config.get("bridge_gap_ms") or 0),
        "evaluations": summary.get("evaluations") or {},
        "speaker_policy": str(
            summary.get("speaker_policy")
            or "anonymous speakers preserved independently of source"
        ),
    }


def _v2d2_overview(run, summary: dict[str, Any]) -> dict[str, Any]:
    if run is None:
        return {
            "available": False,
            "reason": "尚未生成 V2-D.2 人工身份污染审计。",
            "identity_policy": "人工身份不会全局覆盖匿名 speaker。",
        }
    return {
        "available": True,
        "run_id": int(run["id"]),
        "truth_set_id": int(summary.get("truth_set_id") or 0),
        "truth_name": str(summary.get("truth_name") or ""),
        "annotation_regions": int(summary.get("annotation_regions") or 0),
        "reviewed_truth_ms": int(summary.get("reviewed_truth_ms") or 0),
        "covered_truth_ms": int(summary.get("covered_truth_ms") or 0),
        "coverage": float(summary.get("coverage") or 0.0),
        "human_speakers": list(summary.get("human_speakers") or []),
        "model_speakers": list(summary.get("model_speakers") or []),
        "contaminated_speakers": list(
            summary.get("contaminated_speakers") or []
        ),
        "identity_policy": str(
            summary.get("identity_policy")
            or "audit-only interval evidence; model speaker labels remain immutable"
        ),
    }


def _identity_truth_regions(
    database: Database,
    truth_set_id: int,
    *,
    start_ms: int,
    end_ms: int,
) -> list[dict[str, Any]]:
    if not truth_set_id:
        return []
    truth_set = database.get_truth_set(truth_set_id)
    if str(truth_set["status"]) != "frozen":
        return []
    regions = []
    for row in database.list_truth_annotations(
        truth_set_id, annotation_kind="speaker"
    ):
        row_start = int(row["session_start_ms"])
        row_end = int(row["session_end_ms"])
        identity = str(row["label"] or "").strip()
        if not identity or row_end <= start_ms or row_start >= end_ms:
            continue
        regions.append(
            {
                "annotation_id": int(row["id"]),
                "truth_set_id": truth_set_id,
                "start_ms": max(start_ms, row_start),
                "end_ms": min(end_ms, row_end),
                "source_start_ms": row_start,
                "source_end_ms": row_end,
                "identity": identity,
                "metadata": _json_object(row["metadata_json"]),
                "audit_only": True,
            }
        )
    return sorted(
        regions,
        key=lambda item: (item["start_ms"], item["end_ms"], item["identity"]),
    )


def _identity_evidence(
    start_ms: int,
    end_ms: int,
    regions: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    duration_ms = max(1, end_ms - start_ms)
    grouped: dict[str, int] = defaultdict(int)
    for region in regions:
        overlap_ms = _intersection_ms(
            start_ms,
            end_ms,
            int(region["start_ms"]),
            int(region["end_ms"]),
        )
        if overlap_ms:
            grouped[str(region["identity"])] += overlap_ms
    ordered = sorted(grouped.items(), key=lambda item: (-item[1], item[0]))
    evidence = [
        {
            "identity": identity,
            "overlap_ms": overlap_ms,
            "overlap_ratio": overlap_ms / duration_ms,
        }
        for identity, overlap_ms in ordered
    ]
    if not evidence:
        return {
            "kind": "none",
            "primary_identity": None,
            "evidence": [],
            "audit_only": True,
        }
    first = evidence[0]
    second_ratio = evidence[1]["overlap_ratio"] if len(evidence) > 1 else 0.0
    certain = (
        first["overlap_ratio"] >= 0.5
        and first["overlap_ratio"] - second_ratio >= 0.15
    )
    return {
        "kind": "human_truth_overlap" if certain else "ambiguous_human_truth",
        "primary_identity": first["identity"] if certain else None,
        "evidence": evidence,
        "audit_only": True,
    }


def _enrich_candidates_with_identity(
    candidates: Sequence[dict[str, Any]],
    regions: Sequence[dict[str, Any]],
) -> None:
    for candidate in candidates:
        grouped: dict[str, int] = defaultdict(int)
        for region in regions:
            overlap_ms = _intersection_ms(
                int(candidate["start_ms"]),
                int(candidate["end_ms"]),
                int(region["start_ms"]),
                int(region["end_ms"]),
            )
            if overlap_ms:
                grouped[str(region["identity"])] += overlap_ms
        identity_priority = {
            "mother": 0,
            "father": 1,
            "self": 2,
            "me": 2,
            "tv": 3,
            "unknown": 4,
        }
        ordered = sorted(
            grouped.items(),
            key=lambda item: (
                identity_priority.get(item[0], 5),
                -item[1],
                item[0],
            ),
        )
        candidate["identity_labels"] = [identity for identity, _ in ordered]
        candidate["identity_truth_ms"] = sum(grouped.values())


def _possible_candidates(
    rows: Sequence[Any],
    tokens: Sequence[dict[str, Any]],
    duration_ms: int,
) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for row in rows:
        metadata = _json_object(row["metadata_json"])
        if metadata.get("tier") != "possible":
            continue
        focus_start = int(row["session_start_ms"])
        focus_end = int(row["session_end_ms"])
        start = max(0, focus_start - 4_000)
        end = min(duration_ms, focus_end + 4_000)
        if end - start > MAX_AUDIO_WINDOW_MS:
            midpoint = (focus_start + focus_end) // 2
            start = max(0, midpoint - MAX_AUDIO_WINDOW_MS // 2)
            end = min(duration_ms, start + MAX_AUDIO_WINDOW_MS)
            start = max(0, end - MAX_AUDIO_WINDOW_MS)
        window_tokens = _tokens_in_range(tokens, start, end)
        evidence_types = [str(value) for value in metadata.get("evidence_types", [])]
        rejected = metadata.get("rejected_candidates") or []
        score = round(
            (focus_end - focus_start) / 1000
            + (4.0 if rejected else 0.0)
            + min(3.0, len(window_tokens) / 5),
            2,
        )
        values.append(
            {
                **_candidate(
                    candidate_id=f"possible-{int(row['id'])}",
                    kind="possible",
                    start_ms=start,
                    end_ms=end,
                    title="可能漏检语音",
                    score=score,
                    speakers=[],
                    speech_ms=None,
                    speaker_switches=None,
                    overlap_ms=0,
                    tokens=window_tokens,
                    unassigned_tokens=sum(
                        token["primary_kind"] == "none" for token in window_tokens
                    ),
                ),
                "focus_start_ms": focus_start,
                "focus_end_ms": focus_end,
                "possible_ms": focus_end - focus_start,
                "evidence_types": evidence_types,
                "has_rejected_asr": bool(rejected),
            }
        )
    values.sort(
        key=lambda item: (
            -int(item["has_rejected_asr"]),
            -item["score"],
            item["focus_start_ms"],
        )
    )
    for index, item in enumerate(values, start=1):
        item["rank"] = index
        item["title"] = f"可能漏检语音 {index:03d}"
    return values


def _evidence_payload(
    row, window_start_ms: int, window_end_ms: int
) -> dict[str, Any]:
    metadata = _json_object(row["metadata_json"])
    return {
        "id": int(row["id"]),
        "start_ms": max(window_start_ms, int(row["session_start_ms"])),
        "end_ms": min(window_end_ms, int(row["session_end_ms"])),
        "source_start_ms": int(row["session_start_ms"]),
        "source_end_ms": int(row["session_end_ms"]),
        "tier": str(metadata.get("tier") or "detected"),
        "confidence": float(row["confidence"] or 0.0),
        "evidence_types": list(metadata.get("evidence_types") or []),
        "reason": metadata.get("reason"),
    }


def _source_truth_regions(
    database: Database,
    session_id: int,
    *,
    start_ms: int,
    end_ms: int,
) -> list[dict[str, Any]]:
    regions: list[dict[str, Any]] = []
    seen: set[tuple[int, int, str]] = set()
    for truth_set in database.list_truth_sets(session_id):
        if str(truth_set["status"]) != "frozen":
            continue
        for row in database.list_truth_annotations(
            int(truth_set["id"]), annotation_kind="speech"
        ):
            row_start = int(row["session_start_ms"])
            row_end = int(row["session_end_ms"])
            if row_end <= start_ms or row_start >= end_ms:
                continue
            metadata = _json_object(row["metadata_json"])
            source = str(metadata.get("speech_source") or "")
            if source not in {
                "media_playback",
                "live_person",
                "mixed_live_media",
                "unknown",
            }:
                continue
            key = (max(start_ms, row_start), min(end_ms, row_end), source)
            if key in seen:
                continue
            seen.add(key)
            regions.append(
                {
                    "start_ms": key[0],
                    "end_ms": key[1],
                    "source_start_ms": row_start,
                    "source_end_ms": row_end,
                    "source": source,
                    "truth_set_id": int(truth_set["id"]),
                    "speaker_identity_labeled": bool(
                        metadata.get("speaker_identity_labeled", False)
                    ),
                }
            )
    return sorted(
        regions,
        key=lambda item: (item["start_ms"], item["end_ms"], item["source"]),
    )


def _resolve_run(database: Database, recording_id: int, run_id: int | None):
    if run_id is not None:
        return _require_run(database, recording_id, run_id)
    matches = [
        row
        for row in database.list_processing_runs(recording_id)
        if str(row["run_kind"]) == "quality_diarization_v2d"
        and str(row["status"]) == "completed"
    ]
    return matches[-1] if matches else None


def _require_run(database: Database, recording_id: int, run_id: int):
    run = database.get_processing_run(run_id)
    if int(run["recording_id"]) != recording_id:
        raise ValueError("V2-D 运行不属于所选录音")
    if str(run["run_kind"]) != "quality_diarization_v2d":
        raise ValueError("所选运行不是 V2-D 说话人运行")
    if str(run["status"]) != "completed":
        raise ValueError("V2-D 运行尚未完成")
    return run


def _validate_window(start_ms: int, end_ms: int, duration_ms: int) -> None:
    if start_ms < 0 or end_ms <= start_ms or end_ms > duration_ms:
        raise ValueError("试听时间范围无效")
    if end_ms - start_ms > MAX_AUDIO_WINDOW_MS:
        raise ValueError("单次试听不能超过 120 秒")


def _speaker_turn(row) -> SpeakerTurn:
    return SpeakerTurn(
        start_ms=int(row["session_start_ms"]),
        end_ms=int(row["session_end_ms"]),
        speaker_label=str(row["speaker_label"]),
        confidence=float(row["confidence"]) if row["confidence"] is not None else None,
    )


def _turn_payload(row, window_start_ms: int, window_end_ms: int) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "speaker": str(row["speaker_label"]),
        "start_ms": max(window_start_ms, int(row["session_start_ms"])),
        "end_ms": min(window_end_ms, int(row["session_end_ms"])),
        "source_start_ms": int(row["session_start_ms"]),
        "source_end_ms": int(row["session_end_ms"]),
        "confidence": (
            float(row["confidence"]) if row["confidence"] is not None else None
        ),
    }


def _attributed_tokens(database: Database, run) -> list[dict[str, Any]]:
    run_id = int(run["id"])
    summary = _json_object(run["summary_json"])
    asr_run_id = int(summary.get("asr_run_id") or run["parent_run_id"] or 0)
    if not asr_run_id:
        return []
    token_rows = database.list_committed_asr_tokens(asr_run_id)
    attribution_rows = database.list_token_speaker_attributions(run_id)
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in attribution_rows:
        grouped[int(row["token_id"])].append(
            {
                "speaker": row["speaker_label"],
                "kind": str(row["attribution_kind"]),
                "rank": int(row["rank"]),
                "overlap_ratio": float(row["overlap_ratio"]),
                "confidence": (
                    float(row["confidence"])
                    if row["confidence"] is not None
                    else None
                ),
            }
        )
    payload: list[dict[str, Any]] = []
    for row in token_rows:
        decisions = sorted(grouped.get(int(row["id"]), []), key=lambda item: item["rank"])
        primary = decisions[0] if decisions else {
            "speaker": None,
            "kind": "none",
            "rank": 0,
            "overlap_ratio": 0.0,
            "confidence": None,
        }
        payload.append(
            {
                "id": int(row["id"]),
                "text": str(row["text"]),
                "start_ms": int(row["session_start_ms"]),
                "end_ms": int(row["session_end_ms"]),
                "primary_speaker": primary["speaker"],
                "primary_kind": primary["kind"],
                "attributions": decisions,
            }
        )
    return payload


def _speaker_summaries(rows: Sequence[Any]) -> list[dict[str, Any]]:
    grouped: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["speaker_label"])].append(
            (int(row["session_start_ms"]), int(row["session_end_ms"]))
        )
    ordered = sorted(
        grouped.items(),
        key=lambda item: (-_union_ms(item[1]), item[0]),
    )
    return [
        {
            "label": label,
            "color": SPEAKER_COLORS[index % len(SPEAKER_COLORS)],
            "speech_ms": _union_ms(intervals),
            "turn_count": len(intervals),
        }
        for index, (label, intervals) in enumerate(ordered)
    ]


def _conversation_candidates(
    exclusive_rows: Sequence[Any],
    tokens: Sequence[dict[str, Any]],
    overlaps: Sequence[tuple[int, int, tuple[str, ...]]],
    duration_ms: int,
) -> list[dict[str, Any]]:
    raw: list[dict[str, Any]] = []
    start = 0
    while start < duration_ms:
        end = min(duration_ms, start + CONVERSATION_WINDOW_MS)
        if end - start < 15_000:
            break
        turns = [
            row
            for row in exclusive_rows
            if int(row["session_end_ms"]) > start
            and int(row["session_start_ms"]) < end
        ]
        intervals = [
            (max(start, int(row["session_start_ms"])), min(end, int(row["session_end_ms"])))
            for row in turns
        ]
        speech_ms = _union_ms(intervals)
        window_tokens = _tokens_in_range(tokens, start, end)
        if speech_ms < 4_000 and len(window_tokens) < 3:
            start += CONVERSATION_STEP_MS
            continue
        labels = sorted({str(row["speaker_label"]) for row in turns})
        switches = _speaker_switches(turns, start, end)
        overlap_ms = _overlap_ms(overlaps, start, end)
        assigned_tokens = sum(
            token["primary_kind"] != "none" for token in window_tokens
        )
        unassigned_tokens = len(window_tokens) - assigned_tokens
        coverage = speech_ms / max(1, end - start)
        score = round(
            35 * min(1.0, coverage / 0.60)
            + 20 * min(1.0, len(window_tokens) / 45)
            + 25 * min(1.0, switches / 12)
            + 15 * min(1.0, max(0, len(labels) - 1) / 2)
            + 5 * min(1.0, overlap_ms / 5_000),
            1,
        )
        raw.append(
            _candidate(
                candidate_id=f"conversation-{start}-{end}",
                kind="conversation",
                start_ms=start,
                end_ms=end,
                title="多人对话候选" if len(labels) >= 2 else "语音密集候选",
                score=score,
                speakers=labels,
                speech_ms=speech_ms,
                speaker_switches=switches,
                overlap_ms=overlap_ms,
                tokens=window_tokens,
                unassigned_tokens=unassigned_tokens,
            )
        )
        start += CONVERSATION_STEP_MS

    selected: list[dict[str, Any]] = []
    for item in sorted(raw, key=lambda row: (-row["score"], row["start_ms"])):
        if any(
            _intersection_ms(
                item["start_ms"], item["end_ms"], chosen["start_ms"], chosen["end_ms"]
            )
            > 30_000
            for chosen in selected
        ):
            continue
        selected.append(item)
        if len(selected) == 12:
            break
    for index, item in enumerate(selected, start=1):
        item["rank"] = index
        item["title"] = f"{item['title']} {index:02d}"
    return selected


def _overlap_candidates(
    overlaps: Sequence[tuple[int, int, tuple[str, ...]]],
    tokens: Sequence[dict[str, Any]],
    duration_ms: int,
) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for focus_start, focus_end, labels in overlaps:
        start = max(0, focus_start - 4_000)
        end = min(duration_ms, focus_end + 4_000)
        window_tokens = _tokens_in_range(tokens, start, end)
        values.append(
            {
                **_candidate(
                    candidate_id=f"overlap-{focus_start}-{focus_end}",
                    kind="overlap",
                    start_ms=start,
                    end_ms=end,
                    title=f"重叠 · {' + '.join(labels)}",
                    score=round((focus_end - focus_start) / 1000, 3),
                    speakers=list(labels),
                    speech_ms=None,
                    speaker_switches=None,
                    overlap_ms=focus_end - focus_start,
                    tokens=window_tokens,
                    unassigned_tokens=sum(
                        token["primary_kind"] == "none" for token in window_tokens
                    ),
                ),
                "focus_start_ms": focus_start,
                "focus_end_ms": focus_end,
            }
        )
    values.sort(key=lambda row: (-row["overlap_ms"], row["focus_start_ms"]))
    return values


def _unassigned_candidates(
    tokens: Sequence[dict[str, Any]], duration_ms: int
) -> list[dict[str, Any]]:
    missing = [token for token in tokens if token["primary_kind"] == "none"]
    groups: list[list[dict[str, Any]]] = []
    for token in missing:
        if (
            not groups
            or token["start_ms"] - groups[-1][-1]["end_ms"] > 2_000
            or token["end_ms"] - groups[-1][0]["start_ms"] > 45_000
        ):
            groups.append([token])
        else:
            groups[-1].append(token)
    values: list[dict[str, Any]] = []
    for group in groups:
        focus_start = group[0]["start_ms"]
        focus_end = group[-1]["end_ms"]
        start = max(0, focus_start - 4_000)
        end = min(duration_ms, focus_end + 4_000)
        window_tokens = _tokens_in_range(tokens, start, end)
        values.append(
            {
                **_candidate(
                    candidate_id=f"unassigned-{focus_start}-{focus_end}",
                    kind="unassigned",
                    start_ms=start,
                    end_ms=end,
                    title=f"无归属文字 · {len(group)} 词",
                    score=float(len(group)),
                    speakers=[],
                    speech_ms=None,
                    speaker_switches=None,
                    overlap_ms=0,
                    tokens=window_tokens,
                    unassigned_tokens=len(group),
                ),
                "focus_start_ms": focus_start,
                "focus_end_ms": focus_end,
            }
        )
    values.sort(
        key=lambda row: (-row["unassigned_tokens"], row["focus_start_ms"])
    )
    return values


def _candidate(
    *,
    candidate_id: str,
    kind: str,
    start_ms: int,
    end_ms: int,
    title: str,
    score: float,
    speakers: Sequence[str],
    speech_ms: int | None,
    speaker_switches: int | None,
    overlap_ms: int,
    tokens: Sequence[dict[str, Any]],
    unassigned_tokens: int,
) -> dict[str, Any]:
    text = "".join(token["text"] for token in tokens).strip()
    return {
        "id": candidate_id,
        "kind": kind,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "title": title,
        "score": score,
        "speakers": list(speakers),
        "speaker_count": len(speakers),
        "speech_ms": speech_ms,
        "speaker_switches": speaker_switches,
        "overlap_ms": overlap_ms,
        "token_count": len(tokens),
        "unassigned_tokens": unassigned_tokens,
        "preview": text[:110],
    }


def _tokens_in_range(
    tokens: Sequence[dict[str, Any]], start_ms: int, end_ms: int
) -> list[dict[str, Any]]:
    return [
        token
        for token in tokens
        if token["end_ms"] > start_ms and token["start_ms"] < end_ms
    ]


def _speaker_switches(rows: Sequence[Any], start_ms: int, end_ms: int) -> int:
    ordered = sorted(
        (
            row
            for row in rows
            if int(row["session_end_ms"]) > start_ms
            and int(row["session_start_ms"]) < end_ms
        ),
        key=lambda row: (int(row["session_start_ms"]), int(row["session_end_ms"])),
    )
    labels: list[str] = []
    for row in ordered:
        label = str(row["speaker_label"])
        if not labels or labels[-1] != label:
            labels.append(label)
    return max(0, len(labels) - 1)


def _overlap_ms(
    overlaps: Sequence[tuple[int, int, tuple[str, ...]]],
    start_ms: int,
    end_ms: int,
) -> int:
    return sum(
        _intersection_ms(start_ms, end_ms, overlap_start, overlap_end)
        for overlap_start, overlap_end, _ in overlaps
    )


def _union_ms(intervals: Iterable[tuple[int, int]]) -> int:
    ordered = sorted((start, end) for start, end in intervals if end > start)
    if not ordered:
        return 0
    total = 0
    current_start, current_end = ordered[0]
    for start, end in ordered[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
        else:
            total += current_end - current_start
            current_start, current_end = start, end
    return total + current_end - current_start


def _intersection_ms(start_a: int, end_a: int, start_b: int, end_b: int) -> int:
    return max(0, min(end_a, end_b) - max(start_a, start_b))


def _json_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    parsed = json.loads(value)
    return parsed if isinstance(parsed, dict) else {}
