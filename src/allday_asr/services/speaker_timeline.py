from __future__ import annotations

import json
from collections import defaultdict
from typing import Any, Iterable, Sequence

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
        },
        "method": {
            "conversation_window_ms": CONVERSATION_WINDOW_MS,
            "conversation_step_ms": CONVERSATION_STEP_MS,
            "original_audio_immutable": True,
            "listening_audio": "从永久原音按需生成响度归一化缓存，不修改原文件。",
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
    return {
        "recording_id": recording_id,
        "run_id": run_id,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "duration_ms": end_ms - start_ms,
        "turns": turns,
        "overlaps": overlaps,
        "tokens": tokens,
        "audio_url": (
            "/api/speaker-timeline/audio"
            f"?recording_id={recording_id}&start_ms={start_ms}&end_ms={end_ms}&v=1"
        ),
    }


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
