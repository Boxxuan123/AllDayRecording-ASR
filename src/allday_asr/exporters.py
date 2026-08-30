from __future__ import annotations

import json
from pathlib import Path

from allday_asr.domain.intervals import group_segments as _group_segments
from allday_asr.domain.time import absolute_datetime as _absolute_datetime
from allday_asr.domain.time import absolute_timestamp as _absolute_timestamp
from allday_asr.domain.time import format_clock as _format_clock
from allday_asr.domain.time import format_offset
from allday_asr.storage.database import Database

_format_offset = format_offset


def export_jsonl(database: Database, recording_id: int, destination: Path) -> Path:
    recording = database.get_recording(recording_id)
    segments = database.all_segments(recording_id, completed_only=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8", newline="\n") as handle:
        for segment in segments:
            payload = {
                "id": segment["id"],
                "recording_id": recording_id,
                "start_ms": segment["start_ms"],
                "end_ms": segment["end_ms"],
                "start_at": _absolute_timestamp(
                    recording["recorded_at"], segment["start_ms"], recording["timezone"]
                ),
                "speaker": segment["person_name"] or segment["speaker_session_id"] or "unknown",
                "person_id": segment["person_id"],
                "language": segment["language"],
                "text_raw": segment["text_raw"] or "",
                "text": segment["text_display"] or "",
                "asr_model": segment["asr_model"],
                "audio_ref": segment["audio_ref"],
            }
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return destination


def export_markdown(
    database: Database,
    recording_id: int,
    destination: Path,
    *,
    max_gap_ms: int = 120_000,
) -> Path:
    recording = database.get_recording(recording_id)
    segments = database.all_segments(recording_id, completed_only=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    local_start = _absolute_datetime(recording["recorded_at"], 0, recording["timezone"])
    title_date = local_start.date().isoformat() if local_start else str(recording["recorded_at"])[:10]
    lines = [f"# {title_date} 录音时间线", ""]
    for group in _group_segments(segments, max_gap_ms=max_gap_ms):
        start_ms = int(group[0]["start_ms"])
        end_ms = int(group[-1]["end_ms"])
        lines.extend(
            [
                f"## {_format_clock(recording, start_ms)}–{_format_clock(recording, end_ms)} 对话",
                "",
            ]
        )
        for segment in group:
            speaker = segment["person_name"] or segment["speaker_session_id"] or "未知说话人"
            text = (segment["text_display"] or "").strip() or "（未识别出文字）"
            lines.append(f"- **{speaker}**：{text}")
            lines.append(
                f"  - 原音：`{segment['audio_ref']}`；片段 ID：`{segment['id']}`"
            )
        lines.append("")
    destination.write_text("\n".join(lines), encoding="utf-8")
    return destination
