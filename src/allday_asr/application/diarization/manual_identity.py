from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Any

from allday_asr.application.diarization.recall_review import IDENTITY_LABELS
from allday_asr.services.sources import resolve_session_slices
from allday_asr.storage.database import Database

MIN_IDENTITY_INTERVAL_MS = 1_000
MAX_IDENTITY_INTERVAL_MS = 10_000
MIN_SPEECH_COVERAGE = 0.5
SECONDARY_SPEAKER_LIMIT_MS = 250


def save_manual_identity_annotation(
    database: Database,
    *,
    session_id: int,
    diarization_run_id: int,
    start_ms: int,
    end_ms: int,
    identity_label: str,
    note: str | None = None,
) -> dict[str, Any]:
    identity_label = identity_label.strip()
    if identity_label not in IDENTITY_LABELS:
        raise ValueError("人物标签无效")
    duration_ms = end_ms - start_ms
    if duration_ms < MIN_IDENTITY_INTERVAL_MS:
        raise ValueError("人物真值区间至少需要 1 秒")
    if duration_ms > MAX_IDENTITY_INTERVAL_MS:
        raise ValueError("人物真值区间不能超过 10 秒")
    slices, gaps = resolve_session_slices(
        database, session_id, start_ms, end_ms
    )
    if gaps or not slices:
        raise ValueError("人物真值区间没有连续的不可变原音来源")

    turns = database.list_diarization_turns(
        diarization_run_id, turn_kind="regular"
    )
    overlaps: dict[str, int] = defaultdict(int)
    for row in turns:
        overlap_ms = _overlap_ms(
            start_ms,
            end_ms,
            int(row["session_start_ms"]),
            int(row["session_end_ms"]),
        )
        if overlap_ms:
            overlaps[str(row["speaker_label"])] += overlap_ms
    if not overlaps:
        raise ValueError("所选区间没有 V2-D 语音，请换到有匿名 speaker 的位置")
    ordered = sorted(overlaps.items(), key=lambda item: (-item[1], item[0]))
    primary_label, primary_ms = ordered[0]
    if primary_ms / duration_ms < MIN_SPEECH_COVERAGE:
        raise ValueError("所选区间的有效说话时间不足一半，请缩短到清晰语句")
    material_secondary = [
        (label, overlap_ms)
        for label, overlap_ms in ordered[1:]
        if overlap_ms >= SECONDARY_SPEAKER_LIMIT_MS
    ]
    if material_secondary:
        raise ValueError("所选区间包含多位匿名 speaker，请截取单人说话的短句")

    canonical = {
        "session_id": session_id,
        "diarization_run_id": diarization_run_id,
        "session_start_ms": start_ms,
        "session_end_ms": end_ms,
    }
    annotation_key = "manual-identity:" + hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    row = database.upsert_manual_identity_annotation(
        {
            "annotation_key": annotation_key,
            "session_id": session_id,
            "diarization_run_id": diarization_run_id,
            "session_start_ms": start_ms,
            "session_end_ms": end_ms,
            "identity_label": identity_label,
            "anonymous_speaker_label": primary_label,
            "note": note,
        }
    )
    return {
        "annotation": _row_payload(row),
        "overview": manual_identity_overview(
            database, session_id, diarization_run_id=diarization_run_id
        ),
    }


def retract_manual_identity_annotation(
    database: Database,
    annotation_id: int,
    *,
    session_id: int,
    diarization_run_id: int,
) -> dict[str, Any]:
    matches = [
        row
        for row in database.list_manual_identity_annotations(
            session_id, diarization_run_id=diarization_run_id, status=None
        )
        if int(row["id"]) == annotation_id
    ]
    if not matches:
        raise KeyError("人工人物区间不属于当前录音会话")
    row = database.retract_manual_identity_annotation(annotation_id)
    return {
        "annotation": _row_payload(row),
        "overview": manual_identity_overview(
            database, session_id, diarization_run_id=diarization_run_id
        ),
    }


def manual_identity_overview(
    database: Database,
    session_id: int,
    *,
    diarization_run_id: int,
) -> dict[str, Any]:
    all_rows = database.list_manual_identity_annotations(
        session_id,
        diarization_run_id=diarization_run_id,
        status=None,
    )
    rows = [row for row in all_rows if str(row["status"]) == "active"]
    counts: dict[str, int] = {}
    durations: dict[str, int] = {}
    for row in rows:
        identity = str(row["identity_label"])
        counts[identity] = counts.get(identity, 0) + 1
        durations[identity] = durations.get(identity, 0) + (
            int(row["session_end_ms"]) - int(row["session_start_ms"])
        )
    return {
        "count": len(rows),
        "duration_ms": sum(durations.values()),
        "identity_counts": counts,
        "identity_durations_ms": durations,
        "identities": sorted(counts),
        "latest_updated_at": max(
            (str(row["updated_at"]) for row in all_rows), default=None
        ),
        "items": [_row_payload(row) for row in rows],
    }


def _row_payload(row: Any) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "session_id": int(row["session_id"]),
        "diarization_run_id": int(row["diarization_run_id"]),
        "start_ms": int(row["session_start_ms"]),
        "end_ms": int(row["session_end_ms"]),
        "identity_label": str(row["identity_label"]),
        "anonymous_speaker_label": str(row["anonymous_speaker_label"]),
        "status": str(row["status"]),
        "note": row["note"],
        "updated_at": str(row["updated_at"]),
    }


def _overlap_ms(
    start_a: int, end_a: int, start_b: int, end_b: int
) -> int:
    return max(0, min(end_a, end_b) - max(start_a, start_b))
