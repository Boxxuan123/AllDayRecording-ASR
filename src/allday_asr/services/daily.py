from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from allday_asr.config import AppConfig
from allday_asr.exporters import export_jsonl, export_markdown
from allday_asr.paths import recording_output_dir
from allday_asr.services.actions import extract_action_candidates
from allday_asr.services.diarization import diarize_recording
from allday_asr.services.ingest import ingest_recording
from allday_asr.services.processing import ProcessSummary, process_recording
from allday_asr.services.timeline import build_timeline
from allday_asr.services.verification import export_self_candidates
from allday_asr.storage.database import Database


ProgressCallback = Callable[[str, str], None]


@dataclass(frozen=True)
class DailyRunStep:
    name: str
    status: str
    detail: str


@dataclass(frozen=True)
class DailyRunSummary:
    run_id: int
    recording_id: int
    status: str
    config_sha256: str
    steps: list[DailyRunStep]
    review_actions: list[str]
    artifacts: dict[str, str]
    manifest_json_path: Path
    manifest_markdown_path: Path


def run_daily(
    database: Database,
    source: int | Path,
    config: AppConfig,
    *,
    progress: ProgressCallback | None = None,
) -> DailyRunSummary:
    """Run the safe, resumable offline pipeline without overwriting human review."""
    if isinstance(source, int):
        recording = database.get_recording(source)
        created = False
    else:
        ingest_result = ingest_recording(
            database,
            source,
            device=config.ingest.device,
            timezone_name=config.ingest.timezone,
        )
        recording = ingest_result.recording
        created = ingest_result.created
    recording_id = int(recording["id"])
    config_payload = config.to_dict()
    config_sha256 = config.sha256()
    run_id = database.start_processing_run(
        recording_id,
        run_kind="daily",
        config=config_payload,
        config_sha256=config_sha256,
    )
    database.set_stage(
        recording_id,
        "daily_run",
        "running",
        details={"run_id": run_id, "config_sha256": config_sha256},
    )

    steps: list[DailyRunStep] = [
        DailyRunStep(
            "ingest",
            "completed" if created else "reused",
            "新录音已按 SHA-256 入库" if created else "复用已入库录音",
        )
    ]
    artifacts: dict[str, str] = {}
    review_actions: list[str] = []

    def report(name: str, detail: str) -> None:
        if progress:
            progress(name, detail)

    try:
        report("process", "检查标准化、VAD 和 ASR 状态")
        if _asr_is_complete(database, recording_id):
            counts = database.segment_status_counts(recording_id)
            steps.append(DailyRunStep("process", "reused", f"复用已完成 ASR：{counts}"))
        else:
            process_summary = process_recording(
                database,
                recording_id,
                device=config.runtime.device,
                language=config.asr.language,
            )
            _require_complete_asr(process_summary)
            steps.append(
                DailyRunStep(
                    "process",
                    "completed",
                    f"VAD={process_summary.vad_segments}，本次 ASR={process_summary.processed_now}",
                )
            )

        report("diarization", "检查匿名说话人标签")
        diarization_stage = database.get_stage(recording_id, "diarization")
        if not config.diarization.enabled:
            steps.append(DailyRunStep("diarization", "disabled", "配置已关闭"))
        elif diarization_stage is not None and diarization_stage["status"] == "completed":
            steps.append(
                DailyRunStep(
                    "diarization",
                    "reused",
                    "复用已有标签，避免覆盖人工身份",
                )
            )
        else:
            diarization_summary = diarize_recording(
                database,
                recording_id,
                device=config.runtime.device,
                preset_speakers=config.diarization.preset_speakers,
                min_segment_ms=round(config.diarization.min_segment_seconds * 1000),
                min_cluster_segments=config.diarization.min_cluster_segments,
                min_cluster_speech_ms=round(
                    config.diarization.min_cluster_speech_seconds * 1000
                ),
            )
            steps.append(
                DailyRunStep(
                    "diarization",
                    "completed",
                    f"assigned={diarization_summary.assigned_segments}，"
                    f"unknown={diarization_summary.unassigned_segments}",
                )
            )

        report("identity", "检查本人档案和人工审核状态")
        identity_status = _handle_identity_candidates(
            database,
            recording_id,
            config,
            artifacts,
            review_actions,
        )
        steps.append(identity_status)

        report("timeline", "生成事件时间线与转写导出")
        timeline_summary = build_timeline(
            database,
            recording_id,
            max_gap_seconds=config.timeline.max_gap_seconds,
        )
        output_dir = recording_output_dir(recording_id)
        jsonl_path = export_jsonl(database, recording_id, output_dir / "transcript.jsonl")
        markdown_path = export_markdown(
            database,
            recording_id,
            output_dir / "transcript.md",
            max_gap_ms=round(config.timeline.max_gap_seconds * 1000),
        )
        artifacts.update(
            {
                "timeline_markdown": str(timeline_summary.markdown_path.resolve()),
                "timeline_json": str(timeline_summary.json_path.resolve()),
                "transcript_markdown": str(markdown_path.resolve()),
                "transcript_jsonl": str(jsonl_path.resolve()),
            }
        )
        steps.append(
            DailyRunStep(
                "timeline",
                "completed",
                f"events={timeline_summary.event_count}，segments={timeline_summary.segment_count}",
            )
        )

        report("actions", "提取带原音证据的日程/待办候选")
        if config.actions.enabled:
            action_summary = extract_action_candidates(
                database,
                recording_id,
                require_self_confirmation=config.actions.require_self_confirmation,
                confirmation_window_seconds=config.actions.confirmation_window_seconds,
                min_confidence=config.actions.min_confidence,
            )
            artifacts["action_candidates_markdown"] = str(
                action_summary.markdown_path.resolve()
            )
            artifacts["action_candidates_json"] = str(action_summary.json_path.resolve())
            if action_summary.pending_candidates:
                review_actions.append(
                    f"确认或忽略 {action_summary.pending_candidates} 个日程/待办候选："
                    f"{action_summary.markdown_path.resolve()}"
                )
            steps.append(
                DailyRunStep(
                    "actions",
                    "awaiting_review" if action_summary.pending_candidates else "completed",
                    f"total={action_summary.total_candidates}，"
                    f"pending={action_summary.pending_candidates}",
                )
            )
        else:
            steps.append(DailyRunStep("actions", "disabled", "配置已关闭行动候选"))

        status = "needs_attention" if review_actions else "completed"
        manifest_json_path = output_dir / "daily-run.json"
        manifest_markdown_path = output_dir / "daily-run.md"
        artifacts.update(
            {
                "daily_run_json": str(manifest_json_path.resolve()),
                "daily_run_markdown": str(manifest_markdown_path.resolve()),
            }
        )
        stage_snapshots = _stage_snapshots(database, recording_id)
        summary_payload = {
            "run_id": run_id,
            "recording_id": recording_id,
            "status": status,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "config_sha256": config_sha256,
            "config": config_payload,
            "processing_stages": stage_snapshots,
            "steps": [asdict(step) for step in steps],
            "review_actions": review_actions,
            "artifacts": artifacts,
        }
        manifest_json_path.write_text(
            json.dumps(summary_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        manifest_markdown_path.write_text(
            _render_manifest_markdown(summary_payload), encoding="utf-8"
        )
        database.finish_processing_run(
            run_id,
            status=status,
            summary={
                "steps": [asdict(step) for step in steps],
                "review_actions": review_actions,
                "processing_stages": stage_snapshots,
            },
            artifacts=artifacts,
        )
        database.set_stage(
            recording_id,
            "daily_run",
            "partial" if review_actions else "completed",
            details={
                "run_id": run_id,
                "status": status,
                "config_sha256": config_sha256,
                "review_actions": review_actions,
                "manifest_json_path": str(manifest_json_path),
                "manifest_markdown_path": str(manifest_markdown_path),
            },
        )
        return DailyRunSummary(
            run_id=run_id,
            recording_id=recording_id,
            status=status,
            config_sha256=config_sha256,
            steps=steps,
            review_actions=review_actions,
            artifacts=artifacts,
            manifest_json_path=manifest_json_path,
            manifest_markdown_path=manifest_markdown_path,
        )
    except Exception as exc:
        database.finish_processing_run(run_id, status="failed", error=repr(exc))
        database.set_stage(
            recording_id,
            "daily_run",
            "failed",
            error=repr(exc),
            details={"run_id": run_id, "config_sha256": config_sha256},
        )
        raise


def _asr_is_complete(database: Database, recording_id: int) -> bool:
    recording = database.get_recording(recording_id)
    normalized_path = Path(recording["normalized_path"] or "")
    stage = database.get_stage(recording_id, "asr")
    counts = database.segment_status_counts(recording_id)
    return bool(
        normalized_path.is_file()
        and stage is not None
        and stage["status"] == "completed"
        and counts.get("completed", 0) > 0
        and counts.get("pending", 0) == 0
        and counts.get("running", 0) == 0
        and counts.get("failed", 0) == 0
    )


def _require_complete_asr(summary: ProcessSummary) -> None:
    counts = summary.status_counts
    if (
        counts.get("completed", 0) == 0
        or counts.get("pending", 0)
        or counts.get("running", 0)
        or counts.get("failed", 0)
    ):
        raise RuntimeError(f"ASR 尚未完整完成：{counts}")


def _handle_identity_candidates(
    database: Database,
    recording_id: int,
    config: AppConfig,
    artifacts: dict[str, str],
    review_actions: list[str],
) -> DailyRunStep:
    if not config.identity.candidates_enabled:
        return DailyRunStep("identity", "disabled", "配置已关闭本人候选")
    annotations = database.list_segment_annotations(recording_id)
    if annotations:
        return DailyRunStep(
            "identity",
            "reviewed",
            f"复用 {len(annotations)} 条人工身份标注",
        )
    profile = database.get_self_profile()
    if profile is None or not profile["embedding_path"]:
        review_actions.append("先执行 enroll-self 建立本人独立声纹，再生成本人候选。")
        return DailyRunStep("identity", "needs_setup", "尚未登记本人声纹")

    root = recording_output_dir(recording_id) / "self-candidates"
    manifest_path = root / "README.md"
    scores_path = root / "scores.json"
    if manifest_path.is_file() and scores_path.is_file():
        artifacts["self_candidate_review"] = str(manifest_path.resolve())
        review_actions.append(f"试听并标注本人候选：{manifest_path.resolve()}")
        return DailyRunStep(
            "identity",
            "awaiting_review",
            "复用已有候选，未覆盖可能存在的人工编辑",
        )

    candidate_summary = export_self_candidates(
        database,
        recording_id,
        device=config.runtime.device,
        threshold=config.identity.threshold,
        min_segment_ms=round(config.identity.min_segment_seconds * 1000),
        top=config.identity.top,
    )
    artifacts["self_candidate_review"] = str(candidate_summary.manifest_path.resolve())
    artifacts["self_candidate_scores"] = str(candidate_summary.json_path.resolve())
    review_actions.append(f"试听并标注本人候选：{candidate_summary.manifest_path.resolve()}")
    return DailyRunStep(
        "identity",
        "awaiting_review",
        f"生成 {candidate_summary.strict_candidates} 个严格候选，等待人工确认",
    )


def _render_manifest_markdown(payload: dict) -> str:
    lines = [
        f"# Recording {payload['recording_id']} 一键离线日记",
        "",
        f"- run_id：`{payload['run_id']}`",
        f"- 状态：`{payload['status']}`",
        f"- 配置 SHA-256：`{payload['config_sha256']}`",
        "",
        "## 处理步骤",
        "",
    ]
    for step in payload["steps"]:
        lines.append(f"- `{step['status']}` **{step['name']}**：{step['detail']}")
    lines.extend(["", "## 待人工确认", ""])
    if payload["review_actions"]:
        lines.extend(f"- {item}" for item in payload["review_actions"])
    else:
        lines.append("- 无")
    lines.extend(["", "## 结果文件", ""])
    lines.extend(f"- `{name}`：`{path}`" for name, path in payload["artifacts"].items())
    lines.append("")
    return "\n".join(lines)


def _stage_snapshots(database: Database, recording_id: int) -> dict[str, dict]:
    snapshots: dict[str, dict] = {}
    for stage in database.list_stages(recording_id):
        if stage["stage"] == "daily_run":
            continue
        details = None
        if stage["details_json"]:
            try:
                details = json.loads(stage["details_json"])
            except json.JSONDecodeError:
                details = {"raw": stage["details_json"]}
        snapshots[stage["stage"]] = {
            "status": stage["status"],
            "model_id": stage["model_id"],
            "model_version": stage["model_version"],
            "details": details,
        }
    return snapshots
