from __future__ import annotations

from pathlib import Path

from allday_asr.application.diarization.timeline import MAX_AUDIO_WINDOW_MS
from allday_asr.audio.tools import extract_clip
from allday_asr.paths import OUTPUT_DIR, recording_output_dir
from allday_asr.services.sources import (
    LogicalWindow,
    logical_window_cache_key,
    materialize_logical_window,
    resolve_session_slices,
)


class MediaUseCases:
    def audio_clip(self, segment_id: int, *, mode: str = "segment") -> Path:
        if mode not in {"segment", "context"}:
            raise ValueError("音频试听模式无效")
        database = self.database()
        segment = database.get_segment(segment_id)
        recording = database.get_recording(int(segment["recording_id"]))
        if mode == "context":
            context_ms = 3_000
            start_ms = max(0, int(segment["start_ms"]) - context_ms)
            end_ms = min(
                int(recording["duration_ms"]),
                int(segment["end_ms"]) + context_ms,
            )
            directory = "web-audio-context-v1"
            filename = f"segment-{segment_id}-context.wav"
        else:
            start_ms = int(segment["start_ms"])
            end_ms = int(segment["end_ms"])
            directory = "web-audio-v3"
            filename = f"segment-{segment_id}-listening.wav"
        destination = (
            recording_output_dir(int(recording["id"]))
            / directory
            / filename
        )
        with self.audio_lock:
            if not destination.is_file():
                extract_clip(
                    Path(recording["source_path"]),
                    destination,
                    start_ms,
                    end_ms,
                    audio_filter="loudnorm=I=-18:LRA=7:TP=-2",
                )
        return destination

    def speaker_timeline_audio_clip(
        self,
        recording_id: int | None = None,
        *,
        session_id: int | None = None,
        start_ms: int,
        end_ms: int,
    ) -> Path:
        database = self.database()
        if session_id is None:
            if recording_id is None:
                raise ValueError("必须指定 recording_id 或 session_id")
            database.get_recording(recording_id)
            session = database.get_session_for_recording(recording_id)
            session_id = int(session["id"])
        else:
            session = database.get_recording_session(session_id)
            legacy_recording_id = session["legacy_recording_id"]
            if (
                recording_id is not None
                and (
                    legacy_recording_id is None
                    or int(legacy_recording_id) != recording_id
                )
            ):
                raise ValueError("recording_id 与 session_id 不属于同一录音会话")
        duration_ms = int(session["duration_ms"])
        if start_ms < 0 or end_ms <= start_ms or end_ms > duration_ms:
            raise ValueError("试听时间范围无效")
        if end_ms - start_ms > MAX_AUDIO_WINDOW_MS:
            raise ValueError("单次试听不能超过 120 秒")
        slices, gaps = resolve_session_slices(
            database, session_id, start_ms, end_ms
        )
        window = LogicalWindow(
            session_id=session_id,
            index=0,
            core_start_ms=start_ms,
            core_end_ms=end_ms,
            analysis_start_ms=start_ms,
            analysis_end_ms=end_ms,
            slices=slices,
            uncovered_ranges=gaps,
        )
        transform = "pcm16-16khz-mono-loudnorm-i18-v1"
        fingerprint = logical_window_cache_key(window, transform=transform)[:16]
        output_root = (
            recording_output_dir(recording_id)
            if recording_id is not None
            else OUTPUT_DIR / f"session-{session_id:06d}"
        )
        destination = (
            output_root
            / "web-speaker-timeline-audio-v1"
            / f"range-{start_ms}-{end_ms}-{fingerprint}.wav"
        )
        with self.audio_lock:
            if not destination.is_file():
                materialize_logical_window(
                    window,
                    destination,
                    audio_filter="loudnorm=I=-18:LRA=7:TP=-2",
                )
        return destination
