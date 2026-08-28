from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from allday_asr.storage.database import Database


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


def _group_segments(segments: Iterable, max_gap_ms: int) -> list[list]:
    groups: list[list] = []
    for segment in segments:
        if not groups or int(segment["start_ms"]) - int(groups[-1][-1]["end_ms"]) > max_gap_ms:
            groups.append([segment])
        else:
            groups[-1].append(segment)
    return groups


def _absolute_timestamp(
    recorded_at: str, offset_ms: int, timezone_name: str | None = None
) -> str | None:
    value = _absolute_datetime(recorded_at, offset_ms, timezone_name)
    return value.isoformat() if value else None


def _absolute_datetime(
    recorded_at: str, offset_ms: int, timezone_name: str | None = None
) -> datetime | None:
    try:
        base = datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    value = base + timedelta(milliseconds=offset_ms)
    if timezone_name:
        try:
            value = value.astimezone(ZoneInfo(timezone_name))
        except ZoneInfoNotFoundError:
            pass
    return value


def _format_offset(milliseconds: int) -> str:
    seconds = milliseconds // 1000
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _format_clock(recording, milliseconds: int) -> str:
    value = _absolute_datetime(recording["recorded_at"], milliseconds, recording["timezone"])
    return value.strftime("%H:%M:%S") if value else _format_offset(milliseconds)
