from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from allday_asr.domain.intervals import group_segments
from allday_asr.domain.time import absolute_timestamp
from allday_asr.exporters import export_markdown
from allday_asr.paths import recording_output_dir
from allday_asr.storage.database import Database


@dataclass(frozen=True)
class TimelineSummary:
    recording_id: int
    event_count: int
    segment_count: int
    markdown_path: Path
    json_path: Path


def build_timeline(
    database: Database,
    recording_id: int,
    *,
    max_gap_seconds: float = 120.0,
) -> TimelineSummary:
    if max_gap_seconds <= 0:
        raise ValueError("max_gap_seconds 必须大于 0")
    recording = database.get_recording(recording_id)
    segments = database.all_segments(recording_id, completed_only=True)
    groups = group_segments(segments, max_gap_ms=round(max_gap_seconds * 1000))
    events = [
        {
            "start_ms": int(group[0]["start_ms"]),
            "end_ms": int(group[-1]["end_ms"]),
            "segment_ids": [int(segment["id"]) for segment in group],
        }
        for group in groups
    ]
    database.replace_conversation_events(recording_id, events)

    output_dir = recording_output_dir(recording_id)
    markdown_path = output_dir / "timeline.md"
    json_path = output_dir / "timeline.json"
    export_markdown(
        database,
        recording_id,
        markdown_path,
        max_gap_ms=round(max_gap_seconds * 1000),
    )

    segment_by_id = {int(segment["id"]): segment for segment in segments}
    event_payloads = []
    for index, event in enumerate(events, start=1):
        event_segments = [segment_by_id[segment_id] for segment_id in event["segment_ids"]]
        event_payloads.append(
            {
                "id": index,
                "start_ms": event["start_ms"],
                "end_ms": event["end_ms"],
                "start_at": absolute_timestamp(
                    recording["recorded_at"], event["start_ms"], recording["timezone"]
                ),
                "end_at": absolute_timestamp(
                    recording["recorded_at"], event["end_ms"], recording["timezone"]
                ),
                "segments": [
                    {
                        "id": int(segment["id"]),
                        "start_ms": int(segment["start_ms"]),
                        "end_ms": int(segment["end_ms"]),
                        "speaker": segment["person_name"]
                        or segment["speaker_session_id"]
                        or "unknown",
                        "text": segment["text_display"] or "",
                        "audio_ref": segment["audio_ref"],
                    }
                    for segment in event_segments
                ],
            }
        )
    payload = {
        "recording_id": recording_id,
        "recorded_at": recording["recorded_at"],
        "timezone": recording["timezone"],
        "max_gap_seconds": max_gap_seconds,
        "events": event_payloads,
    }
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    database.set_stage(
        recording_id,
        "timeline",
        "completed",
        details={
            "event_count": len(events),
            "segment_count": len(segments),
            "max_gap_seconds": max_gap_seconds,
            "markdown_path": str(markdown_path),
            "json_path": str(json_path),
        },
    )
    return TimelineSummary(
        recording_id=recording_id,
        event_count=len(events),
        segment_count=len(segments),
        markdown_path=markdown_path,
        json_path=json_path,
    )
