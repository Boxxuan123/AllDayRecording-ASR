from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import soundfile as sf

from allday_asr.asr.funasr_backend import ASR_MODEL_ID, VAD_MODEL_ID, FunASRBackend
from allday_asr.audio.tools import normalize_to_wav
from allday_asr.paths import recording_output_dir
from allday_asr.storage.database import Database


ProgressCallback = Callable[[int, int, str], None]


@dataclass(frozen=True)
class ProcessSummary:
    recording_id: int
    vad_segments: int
    processed_now: int
    status_counts: dict[str, int]
    elapsed_seconds: float
    speech_seconds_processed: float
    rtf: float | None
    peak_vram_mib: float | None


def process_recording(
    database: Database,
    recording_id: int,
    *,
    device: str = "auto",
    max_segments: int | None = None,
    force_vad: bool = False,
    retry_failed: bool = False,
    reprocess_asr: bool = False,
    language: str = "zh",
    progress: ProgressCallback | None = None,
) -> ProcessSummary:
    recording = database.get_recording(recording_id)
    source = Path(recording["source_path"])
    if not source.exists():
        raise FileNotFoundError(f"原始录音不存在：{source}")

    started = time.perf_counter()
    database.update_recording(recording_id, status="processing", error=None)
    output_dir = recording_output_dir(recording_id)
    normalized_path = output_dir / "normalized-16k-mono.wav"

    try:
        if not normalized_path.is_file():
            database.set_stage(recording_id, "normalize", "running")
            normalize_to_wav(source, normalized_path)
            database.update_recording(recording_id, normalized_path=str(normalized_path))
            database.set_stage(
                recording_id,
                "normalize",
                "completed",
                details={
                    "path": str(normalized_path),
                    "sample_rate": 16000,
                    "channels": 1,
                    "reused": False,
                },
            )
        elif recording["normalized_path"] != str(normalized_path):
            database.update_recording(recording_id, normalized_path=str(normalized_path))

        backend: FunASRBackend | None = None
        if force_vad or database.segment_count(recording_id) == 0:
            backend = FunASRBackend(device=device)
            database.set_stage(
                recording_id,
                "vad",
                "running",
                model_id=VAD_MODEL_ID,
                model_version=backend.package_version,
            )
            vad_segments = backend.detect_speech(normalized_path)
            database.replace_vad_segments(recording_id, vad_segments, str(source))
            database.set_stage(
                recording_id,
                "vad",
                "completed",
                model_id=VAD_MODEL_ID,
                model_version=backend.package_version,
                details={
                    "segment_count": len(vad_segments),
                    "speech_seconds": sum(end - start for start, end in vad_segments) / 1000,
                },
            )

        if retry_failed:
            database.reset_failed_segments(recording_id)
        if reprocess_asr:
            database.reset_all_asr_segments(recording_id)
        database.reset_interrupted_segments(recording_id)

        pending = database.pending_segments(recording_id, limit=max_segments)
        if not pending:
            counts = database.segment_status_counts(recording_id)
            unfinished = counts.get("pending", 0) + counts.get("running", 0)
            failed = counts.get("failed", 0)
            completed = unfinished == 0 and failed == 0
            database.update_recording(
                recording_id,
                status="completed" if completed else "partial",
                error=None,
            )
            return ProcessSummary(
                recording_id=recording_id,
                vad_segments=database.segment_count(recording_id),
                processed_now=0,
                status_counts=counts,
                elapsed_seconds=time.perf_counter() - started,
                speech_seconds_processed=0.0,
                rtf=None,
                peak_vram_mib=None,
            )

        backend = backend or FunASRBackend(device=device)
        database.set_stage(
            recording_id,
            "asr",
            "running",
            model_id=ASR_MODEL_ID,
            model_version=backend.package_version,
        )
        processed_now = 0
        speech_ms_processed = 0

        if pending:
            backend.ensure_asr_loaded()
        asr_started = time.perf_counter()
        peak_vram_mib: float | None = None
        if backend.device.startswith("cuda"):
            import torch

            torch.cuda.reset_peak_memory_stats()

        with sf.SoundFile(normalized_path) as audio:
            if audio.samplerate != 16000 or audio.channels != 1:
                raise RuntimeError(
                    f"标准化音频格式错误：{audio.samplerate} Hz / {audio.channels} channels"
                )
            for position, segment in enumerate(pending, start=1):
                segment_id = int(segment["id"])
                start_ms = int(segment["start_ms"])
                end_ms = int(segment["end_ms"])
                database.mark_segment_running(segment_id)
                if progress:
                    progress(position, len(pending), f"{start_ms / 1000:.1f}s–{end_ms / 1000:.1f}s")
                try:
                    start_frame = round(start_ms * audio.samplerate / 1000)
                    frame_count = max(1, round((end_ms - start_ms) * audio.samplerate / 1000))
                    audio.seek(start_frame)
                    samples = audio.read(frame_count, dtype="float32", always_2d=False)
                    transcript = backend.transcribe(samples, language=language)
                    database.mark_segment_completed(
                        segment_id,
                        language=transcript.language,
                        text_raw=transcript.raw_text,
                        text_display=transcript.display_text,
                        asr_model=ASR_MODEL_ID,
                    )
                    processed_now += 1
                    speech_ms_processed += end_ms - start_ms
                except Exception as exc:
                    database.mark_segment_failed(segment_id, repr(exc))

        counts = database.segment_status_counts(recording_id)
        unfinished = counts.get("pending", 0) + counts.get("running", 0)
        failed = counts.get("failed", 0)
        stage_status = "completed" if unfinished == 0 and failed == 0 else "partial"
        elapsed = time.perf_counter() - started
        asr_elapsed = time.perf_counter() - asr_started
        rtf = asr_elapsed / (speech_ms_processed / 1000) if speech_ms_processed else None
        if backend.device.startswith("cuda"):
            import torch

            peak_vram_mib = torch.cuda.max_memory_allocated() / 1024**2
        database.set_stage(
            recording_id,
            "asr",
            stage_status,
            model_id=ASR_MODEL_ID,
            model_version=backend.package_version,
            details={
                "processed_now": processed_now,
                "status_counts": counts,
                "elapsed_seconds": elapsed,
                "asr_elapsed_seconds": asr_elapsed,
                "speech_seconds_processed": speech_ms_processed / 1000,
                "rtf": rtf,
                "peak_vram_mib": peak_vram_mib,
                "language": language,
            },
        )
        database.update_recording(
            recording_id,
            status="completed" if stage_status == "completed" else "partial",
            error=None,
        )
        return ProcessSummary(
            recording_id=recording_id,
            vad_segments=database.segment_count(recording_id),
            processed_now=processed_now,
            status_counts=counts,
            elapsed_seconds=elapsed,
            speech_seconds_processed=speech_ms_processed / 1000,
            rtf=rtf,
            peak_vram_mib=peak_vram_mib,
        )
    except Exception as exc:
        database.update_recording(recording_id, status="failed", error=repr(exc))
        raise
