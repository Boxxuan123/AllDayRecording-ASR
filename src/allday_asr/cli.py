from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from allday_asr.asr.oracle_backends import create_oracle_backend
from allday_asr.asr.quality_backends import (
    FunAsrNanoBackend,
    Qwen3AsrBackend,
    SpeechGateSettings,
)
from allday_asr.audio.tools import extract_clip
from allday_asr.blind_web import serve_blind_annotation
from allday_asr.config import load_config
from allday_asr.diarization.quality_backends import (
    PyannoteCommunityBackend,
    SpeakerTurn,
)
from allday_asr.doctor import run_checks
from allday_asr.exporters import export_jsonl, export_markdown
from allday_asr.paths import DEFAULT_CONFIG_PATH, DEFAULT_DB_PATH, recording_output_dir
from allday_asr.services.benchmark import (
    benchmark_comparison,
    create_acoustic_blind_truth_task,
    create_blind_truth_task,
    create_continuous_truth_template,
    evaluate_benchmark,
    freeze_completed_blind_subset,
    import_continuous_truth,
    migrate_legacy_truth,
    paired_oracle_bootstrap,
    snapshot_oracle_asr_predictions,
    snapshot_v1_predictions,
)
from allday_asr.services.daily import run_daily
from allday_asr.services.diarization import audit_speaker_assignments, diarize_recording
from allday_asr.services.enrollment import enroll_known_person, enroll_self
from allday_asr.services.evaluation import (
    create_evaluation_template,
    evaluate_truth,
    parse_offset,
)
from allday_asr.services.ingest import ingest_recording
from allday_asr.services.processing import process_recording
from allday_asr.services.quality_asr import (
    QualityAsrSettings,
    run_quality_asr,
    snapshot_quality_asr,
)
from allday_asr.services.quality_diarization import (
    QualityDiarizationSettings,
    compute_overlap_regions,
    run_quality_diarization,
    snapshot_quality_diarization,
)
from allday_asr.services.quality_diarization_v2d1 import (
    V2D1Settings,
    create_source_micro_truth,
    run_quality_diarization_v2d1,
)
from allday_asr.services.review import import_self_review
from allday_asr.services.sources import (
    audit_all_sources,
    audit_source_object,
    plan_logical_windows,
)
from allday_asr.services.speakers import (
    export_speaker_samples,
    mark_speaker_as_self,
    unmark_self,
)
from allday_asr.services.timeline import build_timeline
from allday_asr.services.verification import export_self_candidates
from allday_asr.services.voice_library import (
    accumulate_reviewed_samples,
    get_library_status,
    sync_all_enrollments,
    sync_person_enrollment,
    write_library_manifests,
)
from allday_asr.storage.database import Database
from allday_asr.web import serve_web

app = typer.Typer(
    name="allday-asr",
    help="本地全天录音转写与时间线工具。",
    no_args_is_help=True,
)
console = Console()
library_app = typer.Typer(help="管理本地人物声纹样本库。", no_args_is_help=True)
app.add_typer(library_app, name="voice-library")
evaluation_app = typer.Typer(help="建立人工真值并评测离线结果。", no_args_is_help=True)
app.add_typer(evaluation_app, name="evaluation")
benchmark_app = typer.Typer(
    help="管理连续时间真值、不可变预测快照和多 run 对比。",
    no_args_is_help=True,
)
app.add_typer(benchmark_app, name="benchmark")
quality_asr_app = typer.Typer(
    help="V2-C 质量优先双模型转写、强制对齐与分歧队列。",
    no_args_is_help=True,
)
app.add_typer(quality_asr_app, name="asr-v2")
quality_diarization_app = typer.Typer(
    help="V2-D 重叠感知说话人时间轴和 ASR token 归属。",
    no_args_is_help=True,
)
app.add_typer(quality_diarization_app, name="diarization-v2")


@quality_diarization_app.command(name="run")
def quality_diarization_run(
    recording_id: int = typer.Argument(..., min=1),
    asr_run_id: Optional[int] = typer.Option(
        None,
        "--asr-run",
        min=1,
        help="用于 token 归属的已完成 V2-C run；默认选择该录音最新一次。",
    ),
    model_path: Optional[Path] = typer.Option(
        None,
        exists=True,
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="已经下载好的 Community-1 本地 snapshot；默认使用项目缓存/HF_TOKEN。",
    ),
    num_speakers: Optional[int] = typer.Option(
        None, min=1, help="已知说话人数；会覆盖 min/max。"
    ),
    min_speakers: Optional[int] = typer.Option(None, min=1, help="最少说话人数。"),
    max_speakers: Optional[int] = typer.Option(None, min=1, help="最多说话人数。"),
    config: Path = typer.Option(
        DEFAULT_CONFIG_PATH,
        exists=True,
        file_okay=True,
        dir_okay=False,
        resolve_path=True,
        help="TOML 配置文件。",
    ),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """在整段原始会话派生的临时 PCM 上运行本地 Community-1。"""
    resolved = load_config(config)
    database = Database(db)
    if asr_run_id is None:
        candidates = [
            row
            for row in database.list_processing_runs(recording_id)
            if str(row["run_kind"]) == "quality_asr_v2c"
            and str(row["status"]) == "completed"
        ]
        if not candidates:
            raise typer.BadParameter("该录音没有已完成的 V2-C ASR run")
        asr_run_id = int(candidates[-1]["id"])
    quality = resolved.quality_diarization
    settings = QualityDiarizationSettings(
        num_speakers=num_speakers if num_speakers is not None else quality.num_speakers,
        min_speakers=min_speakers if min_speakers is not None else quality.min_speakers,
        max_speakers=max_speakers if max_speakers is not None else quality.max_speakers,
        min_primary_overlap_ratio=quality.min_primary_overlap_ratio,
        min_secondary_overlap_ratio=quality.min_secondary_overlap_ratio,
        min_primary_margin=quality.min_primary_margin,
    )
    selected_model_path = model_path or (
        Path(quality.model_path).resolve() if quality.model_path else None
    )
    try:
        summary = run_quality_diarization(
            database,
            recording_id,
            asr_run_id=asr_run_id,
            settings=settings,
            backend_factory=lambda: PyannoteCommunityBackend(
                model_id=quality.model_id,
                model_path=selected_model_path,
                device=resolved.runtime.device,
                token_env=quality.token_env,
            ),
        )
    except RuntimeError as exc:
        console.print(f"[red]V2-D 未完成[/red]：{exc}")
        raise typer.Exit(code=2) from exc
    console.print(
        f"[green]V2-D 完成[/green] run={summary.run_id} | "
        f"speakers={summary.speakers} | regular={summary.regular_turns} | "
        f"exclusive={summary.exclusive_turns} | overlap="
        f"{summary.overlap_regions}/{summary.overlap_ms / 1000:.1f}s | "
        f"tokens={summary.attributed_tokens} primary={summary.primary_tokens} "
        f"overlap={summary.overlap_tokens} uncertain={summary.uncertain_tokens} "
        f"none={summary.unassigned_tokens}"
    )
    console.print(f"运行清单：{summary.manifest_path.resolve()}")


@quality_diarization_app.command(name="status")
def quality_diarization_status(
    run_id: int = typer.Argument(..., min=1),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """查看说话人时长、重叠区间和 token 归属统计。"""
    database = Database(db)
    run = database.get_processing_run(run_id)
    if str(run["run_kind"]) != "quality_diarization_v2d":
        raise typer.BadParameter("run 不是 V2-D 说话人时间轴运行")
    regular = database.list_diarization_turns(run_id, turn_kind="regular")
    exclusive = database.list_diarization_turns(run_id, turn_kind="exclusive")
    attributions = database.list_token_speaker_attributions(run_id)
    durations: dict[str, int] = {}
    for row in regular:
        label = str(row["speaker_label"])
        durations[label] = durations.get(label, 0) + (
            int(row["session_end_ms"]) - int(row["session_start_ms"])
        )
    table = Table("说话人", "累计时长", "regular turns", "primary tokens", "overlap tokens")
    for label in sorted(durations):
        table.add_row(
            label,
            f"{durations[label] / 1000:.1f}s",
            str(sum(str(row["speaker_label"]) == label for row in regular)),
            str(
                sum(
                    str(row["speaker_label"] or "") == label
                    and str(row["attribution_kind"]) == "primary"
                    for row in attributions
                )
            ),
            str(
                sum(
                    str(row["speaker_label"] or "") == label
                    and str(row["attribution_kind"]) == "overlap"
                    for row in attributions
                )
            ),
        )
    turn_objects = [
        SpeakerTurn(
            start_ms=int(row["session_start_ms"]),
            end_ms=int(row["session_end_ms"]),
            speaker_label=str(row["speaker_label"]),
        )
        for row in regular
    ]
    overlap = compute_overlap_regions(turn_objects)
    console.print(
        f"run={run_id} status={run['status']} regular={len(regular)} "
        f"exclusive={len(exclusive)} overlap={len(overlap)}/"
        f"{sum(end - start for start, end, _ in overlap) / 1000:.1f}s "
        f"attributions={len(attributions)}"
    )
    console.print(table)


@quality_diarization_app.command(name="snapshot")
def quality_diarization_snapshot(
    run_id: int = typer.Argument(..., min=1),
    name: Optional[str] = typer.Option(None, help="不可变 benchmark prediction 名称。"),
    truth_set_id: Optional[int] = typer.Option(
        None,
        "--truth-set",
        min=1,
        help="只冻结该真值 review-region 内的说话人/重叠预测。",
    ),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """把 V2-D regular turns 和真实重叠区冻结为公平评测快照。"""
    summary = snapshot_quality_diarization(
        Database(db), run_id, name=name, truth_set_id=truth_set_id
    )
    console.print(
        f"[green]V2-D 预测已冻结[/green] prediction_set="
        f"{summary.prediction_set_id} | speakers={summary.speaker_predictions} | "
        f"overlap={summary.overlap_predictions} | sha256={summary.content_sha256}"
    )


@quality_diarization_app.command(name="refine")
def quality_diarization_refine(
    recording_id: int = typer.Argument(..., min=1),
    diarization_run_id: int | None = typer.Option(
        None,
        "--diarization-run",
        min=1,
        help="父 V2-D run；默认选择该录音最新完成的一次。",
    ),
    bridge_gap_seconds: float = typer.Option(
        4.0,
        "--bridge-gap",
        min=0.0,
        max=10.0,
        help="仅用于低置信轨的证据桥接间隔；不会改写正式说话人轨。",
    ),
    truth_set_ids: list[int] = typer.Option(
        [],
        "--truth-set",
        min=1,
        help="可重复提供，用同一真值比较 detected 与 recall-rescue。",
    ),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """生成 V2-D.1 检测/可能语音双层快照，保留原匿名 speaker。"""
    database = Database(db)
    if diarization_run_id is None:
        candidates = [
            row
            for row in database.list_processing_runs(recording_id)
            if str(row["run_kind"]) == "quality_diarization_v2d"
            and str(row["status"]) == "completed"
        ]
        if not candidates:
            raise typer.BadParameter("该录音没有已完成的 V2-D run")
        diarization_run_id = int(candidates[-1]["id"])
    summary = run_quality_diarization_v2d1(
        database,
        recording_id,
        diarization_run_id=diarization_run_id,
        settings=V2D1Settings(
            bridge_gap_ms=round(bridge_gap_seconds * 1000)
        ),
        evaluation_truth_set_ids=truth_set_ids,
    )
    console.print(
        f"[green]V2-D.1 完成[/green] run={summary.run_id} | "
        f"detected={summary.detected_regions}/{summary.detected_ms / 1000:.1f}s | "
        f"possible={summary.possible_regions}/{summary.possible_ms / 1000:.1f}s | "
        f"prediction_sets={summary.detected_prediction_set_id}/"
        f"{summary.rescue_prediction_set_id}"
    )
    for truth_set_id, values in summary.evaluations.items():
        detected = values.get("detected", {}).get("vad", {})
        rescue = values.get("recall_rescue", {}).get("vad", {})
        console.print(
            f"truth={truth_set_id} | detected recall={detected.get('recall')} "
            f"FA={detected.get('false_alarm_rate')} | rescue recall="
            f"{rescue.get('recall')} FA={rescue.get('false_alarm_rate')}"
        )
    console.print(f"运行清单：{summary.manifest_path.resolve()}")


@quality_diarization_app.command(name="source-truth")
def quality_diarization_source_truth(
    recording_id: int = typer.Argument(..., min=1),
    start: str = typer.Option(..., help="范围起点，例如 17:00。"),
    end: str = typer.Option(..., help="范围终点，例如 17:14。"),
    source: str = typer.Option(
        ...,
        help="live_person、media_playback、mixed_live_media 或 unknown。",
    ),
    name: str = typer.Option(..., help="稳定的真值名称，只使用字母、数字、点、横线。"),
    note: list[str] = typer.Option(
        [], "--note", help="审计备注，可重复提供；不会作为 speaker 身份真值。"
    ),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """冻结一段来源真值；来源标签不会合并或重命名匿名说话人。"""
    summary = create_source_micro_truth(
        Database(db),
        recording_id,
        name=name,
        start_ms=parse_offset(start),
        end_ms=parse_offset(end),
        speech_source=source,
        notes=note,
    )
    console.print(
        f"[green]来源微型真值已冻结[/green] truth_set={summary.truth_set_id} | "
        f"annotations={summary.annotation_count} | sha256={summary.truth_sha256}"
    )
    console.print(f"真值文件：{summary.output_path.resolve()}")


@quality_asr_app.command(name="run")
def quality_asr_run(
    recording_id: int = typer.Argument(..., min=1),
    profile: Optional[str] = typer.Option(
        None, help="quality-16gb（默认目标）或 compatible-8gb。"
    ),
    max_windows: Optional[int] = typer.Option(
        None, min=1, help="只处理前 N 个五分钟窗口；仅用于 smoke test。"
    ),
    resume_run_id: Optional[int] = typer.Option(
        None, min=1, help="续跑同配置的失败/中断 V2-C run。"
    ),
    config: Path = typer.Option(
        DEFAULT_CONFIG_PATH,
        exists=True,
        file_okay=True,
        dir_okay=False,
        resolve_path=True,
        help="TOML 配置文件。",
    ),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """顺序运行最大 Qwen ASR+Aligner 和 Fun-ASR-Nano，避免同时占用显存。"""
    resolved = load_config(config)
    selected_profile = profile or resolved.asr.vram_profile
    if selected_profile not in {"quality-16gb", "compatible-8gb"}:
        raise typer.BadParameter("profile 必须是 quality-16gb 或 compatible-8gb")
    batch_size = (
        resolved.asr.primary_batch_size_16gb
        if selected_profile == "quality-16gb"
        else resolved.asr.primary_batch_size_8gb
    )
    settings = QualityAsrSettings(
        language=resolved.asr.language,
        window_ms=round(resolved.asr.window_seconds * 1000),
        context_ms=round(resolved.asr.context_seconds * 1000),
        vram_profile=selected_profile,
        max_windows=max_windows,
        speech_gate_fsmn_merge_gap_ms=resolved.asr.speech_gate_fsmn_merge_gap_ms,
        speech_gate_max_utterance_ms=resolved.asr.speech_gate_max_utterance_ms,
        speech_gate_inference_padding_ms=(
            resolved.asr.speech_gate_inference_padding_ms
        ),
        speech_gate_output_padding_ms=resolved.asr.speech_gate_output_padding_ms,
        speech_gate_min_candidate_ms=resolved.asr.speech_gate_min_candidate_ms,
        speech_gate_min_snr_db=resolved.asr.speech_gate_min_snr_db,
        speech_gate_silero_threshold=resolved.asr.speech_gate_silero_threshold,
        speech_gate_silero_min_speech_ms=(
            resolved.asr.speech_gate_silero_min_speech_ms
        ),
        speech_gate_silero_min_silence_ms=(
            resolved.asr.speech_gate_silero_min_silence_ms
        ),
        speech_gate_min_silero_overlap_ms=(
            resolved.asr.speech_gate_min_silero_overlap_ms
        ),
    )
    speech_gate = SpeechGateSettings(
        fsmn_merge_gap_ms=settings.speech_gate_fsmn_merge_gap_ms,
        max_utterance_ms=settings.speech_gate_max_utterance_ms,
        inference_padding_ms=settings.speech_gate_inference_padding_ms,
        speech_output_padding_ms=settings.speech_gate_output_padding_ms,
        min_candidate_ms=settings.speech_gate_min_candidate_ms,
        min_snr_db=settings.speech_gate_min_snr_db,
        silero_threshold=settings.speech_gate_silero_threshold,
        silero_min_speech_ms=settings.speech_gate_silero_min_speech_ms,
        silero_min_silence_ms=settings.speech_gate_silero_min_silence_ms,
        min_silero_overlap_ms=settings.speech_gate_min_silero_overlap_ms,
    )
    summary = run_quality_asr(
        Database(db),
        recording_id,
        settings=settings,
        primary_factory=lambda: Qwen3AsrBackend(
            model_id=resolved.asr.primary_model,
            aligner_model_id=resolved.asr.forced_aligner_model,
            device=resolved.runtime.device,
            batch_size=batch_size,
            max_new_tokens=resolved.asr.max_new_tokens,
            speech_gate=speech_gate,
        ),
        secondary_factory=lambda: FunAsrNanoBackend(
            model_id=resolved.asr.secondary_model,
            device=resolved.runtime.device,
        ),
        resume_run_id=resume_run_id,
    )
    console.print(
        f"[green]V2-C 完成[/green] run={summary.run_id} | "
        f"windows={summary.window_count} | primary={summary.primary_hypotheses} | "
        f"secondary={summary.secondary_hypotheses} | tokens={summary.aligned_tokens} | "
        f"speech_gate={summary.accepted_speech_candidates}/"
        f"{summary.speech_candidates} | committed={summary.committed_primary_tokens} | "
        f"disagreements={summary.disagreements} | "
        f"low_alignment={summary.low_alignment_hypotheses}"
    )
    console.print(f"运行清单：{summary.manifest_path.resolve()}")


@quality_asr_app.command(name="status")
def quality_asr_status(
    run_id: int = typer.Argument(..., min=1),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """查看 V2-C run 的逐窗口模型证据和分歧优先级。"""
    database = Database(db)
    run = database.get_processing_run(run_id)
    hypotheses = database.list_asr_hypotheses(run_id)
    disagreements = database.list_asr_disagreements(run_id)
    table = Table("窗口", "角色", "模型", "Gate", "Tokens", "文本")
    for row in hypotheses:
        tokens = database.list_asr_tokens(int(row["id"]), core_only=True)
        text = str(row["text"]).replace("\n", " ")
        raw_response = json.loads(str(row["raw_response_json"] or "{}"))
        segments = raw_response.get("segments", [])
        gate = (
            f"{sum(bool(item.get('accepted', True)) for item in segments)}/"
            f"{len(segments)}"
            if segments
            else "-"
        )
        table.add_row(
            str(row["window_index"]),
            str(row["hypothesis_role"]),
            str(row["model_id"]),
            gate,
            str(len(tokens)),
            text[:80],
        )
    console.print(
        f"run={run_id} status={run['status']} hypotheses={len(hypotheses)} "
        f"disagreements={len(disagreements)}"
    )
    console.print(table)
    if disagreements:
        disagreement_table = Table("窗口", "优先级", "规范化字符距离")
        for row in disagreements:
            disagreement_table.add_row(
                str(row["window_index"]),
                str(row["priority"]),
                f"{float(row['normalized_distance']):.4f}",
            )
        console.print(disagreement_table)


@quality_asr_app.command(name="snapshot")
def quality_asr_snapshot(
    run_id: int = typer.Argument(..., min=1),
    name: Optional[str] = typer.Option(None, help="不可变 benchmark prediction 名称。"),
    truth_set_id: Optional[int] = typer.Option(
        None,
        "--truth-set",
        min=1,
        help="只保留并裁剪到该真值集的 review-region，用于非连续窗口公平评测。",
    ),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """将 Qwen 主假设的 core tokens 冻结为可与 V1 比较的预测集。"""
    summary = snapshot_quality_asr(
        Database(db), run_id, name=name, truth_set_id=truth_set_id
    )
    console.print(
        f"[green]V2-C 预测已冻结[/green] prediction_set={summary.prediction_set_id} | "
        f"predictions={summary.prediction_count} | sha256={summary.content_sha256}"
    )


@app.command(name="web")
def web_command(
    port: int = typer.Option(8765, min=1, max=65535, help="本地网页端口。"),
    open_browser: bool = typer.Option(
        True,
        "--open/--no-open",
        help="启动后是否自动用默认浏览器打开。",
    ),
    config: Path = typer.Option(
        DEFAULT_CONFIG_PATH,
        exists=True,
        file_okay=True,
        dir_okay=False,
        resolve_path=True,
        help="TOML 配置文件。",
    ),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """启动仅限本机访问的评测、日记和候选操作台。"""
    serve_web(
        database_path=db,
        config_path=config,
        host="127.0.0.1",
        port=port,
        open_browser=open_browser,
    )


@app.command(name="config-show")
def config_show(
    config: Path = typer.Option(
        DEFAULT_CONFIG_PATH,
        exists=True,
        file_okay=True,
        dir_okay=False,
        resolve_path=True,
        help="TOML 配置文件。",
    ),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """校验并显示一键流程的最终配置、哈希和数据库版本。"""
    resolved = load_config(config)
    database = Database(db)
    console.print_json(data=resolved.to_dict())
    console.print(f"config_sha256={resolved.sha256()}")
    console.print(f"database_schema_version={database.schema_version()}")


@app.command(name="sources")
def source_objects(
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """列出永久保存的不可变原始音频对象及其 V2 会话。"""
    database = Database(db)
    sessions_by_recording = {
        int(session["legacy_recording_id"]): session
        for session in database.list_recording_sessions()
        if session["legacy_recording_id"] is not None
    }
    recordings_by_hash = {
        str(recording["sha256"]): recording for recording in database.list_recordings()
    }
    table = Table("Source", "Session", "File", "Bytes", "Integrity", "SHA-256")
    for source in database.list_source_objects():
        recording = recordings_by_hash.get(str(source["sha256"]))
        session = (
            sessions_by_recording.get(int(recording["id"])) if recording is not None else None
        )
        table.add_row(
            str(source["id"]),
            str(session["id"]) if session is not None else "-",
            str(source["original_filename"]),
            str(source["byte_size"] or "unknown"),
            str(source["integrity_status"]),
            str(source["sha256"])[:16],
        )
    console.print(table)


@app.command(name="source-audit")
def source_audit(
    source_id: Optional[int] = typer.Argument(
        None, min=1, help="原始音频对象 ID；省略时审计全部对象。"
    ),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """只读校验原始音频的 SHA-256、大小和媒体元数据。"""
    database = Database(db)
    results = (
        [audit_source_object(database, source_id)]
        if source_id is not None
        else audit_all_sources(database)
    )
    table = Table("Source", "Status", "Bytes", "SHA-256", "Path")
    failed = False
    for result in results:
        failed = failed or result.status != "verified"
        table.add_row(
            str(result.source_object_id),
            result.status,
            str(result.actual_byte_size or "-"),
            (result.actual_sha256 or "-")[:16],
            str(result.path),
        )
        if result.mismatches:
            console.print_json(data=result.mismatches)
        if result.error:
            console.print(f"[red]{result.error}[/red]")
    console.print(table)
    if failed:
        raise typer.Exit(code=1)


@app.command(name="session-windows")
def session_windows(
    session_id: int = typer.Argument(..., min=1, help="V2 录音会话 ID。"),
    window_seconds: float = typer.Option(300.0, min=1.0, help="核心窗口秒数。"),
    context_seconds: float = typer.Option(5.0, min=0.0, help="两侧上下文秒数。"),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """规划不会修改原音、也不会生成整段 PCM 的逻辑处理窗口。"""
    database = Database(db)
    windows = plan_logical_windows(
        database,
        session_id,
        window_ms=round(window_seconds * 1000),
        context_ms=round(context_seconds * 1000),
    )
    table = Table("Index", "Core ms", "Analysis ms", "Sources", "Coverage")
    for window in windows:
        table.add_row(
            str(window.index),
            f"{window.core_start_ms}-{window.core_end_ms}",
            f"{window.analysis_start_ms}-{window.analysis_end_ms}",
            ",".join(str(item.source_object_id) for item in window.slices),
            "complete" if window.coverage_complete else f"gaps={window.uncovered_ranges}",
        )
    console.print(table)


@app.command(name="daily-run")
def daily_run_command(
    source: str = typer.Argument(
        ..., help="音频文件路径，或已经入库的 recording_id。"
    ),
    config: Path = typer.Option(
        DEFAULT_CONFIG_PATH,
        exists=True,
        file_okay=True,
        dir_okay=False,
        resolve_path=True,
        help="TOML 配置文件。",
    ),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """安全、幂等地生成一次离线日记，并列出所有待人工确认项。"""
    source_path = Path(source)
    if source.isdigit() and not source_path.exists():
        resolved_source: int | Path = int(source)
    else:
        if not source_path.is_file():
            raise typer.BadParameter(f"音频文件不存在：{source}")
        resolved_source = source_path.resolve()
    resolved_config = load_config(config)

    with Progress(
        SpinnerColumn(spinner_name="line"),
        TextColumn("{task.description}"),
        console=console,
    ) as progress_ui:
        task_id = progress_ui.add_task("准备一键离线日记……", total=None)

        def update(stage: str, detail: str) -> None:
            progress_ui.update(task_id, description=f"{stage}：{detail}")

        summary = run_daily(
            Database(db), resolved_source, resolved_config, progress=update
        )

    table = Table(title=f"一键离线日记 · recording {summary.recording_id}")
    for column in ("阶段", "状态", "说明"):
        table.add_column(column)
    for step in summary.steps:
        table.add_row(step.name, step.status, step.detail)
    console.print(table)
    console.print(
        f"status={summary.status} | run_id={summary.run_id} | "
        f"config={summary.config_sha256[:12]}"
    )
    if summary.review_actions:
        console.print("[yellow]待人工确认：[/yellow]")
        for action in summary.review_actions:
            console.print(f"- {action}")
    console.print(f"运行清单：{summary.manifest_markdown_path.resolve()}")


@app.command(name="actions")
def action_candidates(
    recording_id: int = typer.Argument(..., min=1),
    status: Optional[str] = typer.Option(
        None, help="只显示 pending、confirmed 或 dismissed。"
    ),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """查看已经提取的日程/待办候选；不会写入真实日历。"""
    if status is not None and status not in {"pending", "confirmed", "dismissed"}:
        raise typer.BadParameter("status 只能是 pending、confirmed 或 dismissed")
    rows = Database(db).list_action_candidates(recording_id, status=status)
    table = Table(title=f"行动候选 · recording {recording_id}")
    for column in ("ID", "类型", "状态", "时间", "地点", "置信度", "标题"):
        table.add_column(column)
    for row in rows:
        table.add_row(
            str(row["id"]),
            row["candidate_type"],
            row["status"],
            row["scheduled_at"] or row["time_text"] or "",
            row["location"] or "",
            f"{row['confidence']:.2f}",
            row["title"],
        )
    console.print(table)


@app.command(name="action-review")
def action_review(
    candidate_id: int = typer.Argument(..., min=1),
    status: str = typer.Option(..., help="pending、confirmed 或 dismissed。"),
    title: Optional[str] = typer.Option(None, help="人工修订标题。"),
    scheduled_at: Optional[str] = typer.Option(
        None, help="人工修订 ISO 8601 时间，例如 2026-08-29T10:00:00+08:00。"
    ),
    location: Optional[str] = typer.Option(None, help="人工修订地点。"),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """确认、忽略或重新打开候选；仍然不会写入真实日历。"""
    try:
        row = Database(db).review_action_candidate(
            candidate_id,
            status=status,
            title=title,
            scheduled_at=scheduled_at,
            location=location,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(
        f"[green]候选已更新[/green] id={row['id']}，status={row['status']}，"
        f"title={row['title']}"
    )


@evaluation_app.command(name="init")
def evaluation_init(
    recording_id: int = typer.Argument(..., min=1),
    name: str = typer.Option("baseline-v1", help="评测集名称。"),
    start: str = typer.Option("0", help="开始偏移：秒、MM:SS 或 HH:MM:SS。"),
    end: Optional[str] = typer.Option(
        None, help="结束偏移：秒、MM:SS 或 HH:MM:SS；默认到录音结束。"
    ),
    force: bool = typer.Option(False, help="覆盖同名真值模板。"),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """从现有片段创建不含原音的私有 JSONL 人工真值模板。"""
    try:
        start_ms = parse_offset(start)
        end_ms = parse_offset(end) if end is not None else None
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    summary = create_evaluation_template(
        Database(db),
        recording_id,
        name=name,
        start_ms=start_ms,
        end_ms=end_ms,
        force=force,
    )
    console.print(
        f"[green]评测模板已创建[/green] segments={summary.item_count}，"
        f"speech={summary.speech_seconds:.1f}s"
    )
    console.print(f"真值：{summary.truth_path.resolve()}")
    console.print(f"说明：{summary.instructions_path.resolve()}")


@evaluation_app.command(name="run")
def evaluation_run(
    truth: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=True,
        dir_okay=False,
        resolve_path=True,
        help="人工填写后的 JSONL 真值文件。",
    ),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """计算 CER、说话人成对指标、本人识别和关键事实召回率。"""
    summary = evaluate_truth(Database(db), truth)
    text_metrics = summary.metrics["text"]
    speaker_metrics = summary.metrics["speaker_pairwise"]
    identity_metrics = summary.metrics["self_identity"]
    fact_metrics = summary.metrics["key_facts"]
    console.print(
        f"[green]评测完成[/green] run_id={summary.evaluation_run_id}，"
        f"segments={summary.item_count}"
    )
    console.print(
        f"CER={_metric(text_metrics['cer'])} | "
        f"speaker_pair_f1={_metric(speaker_metrics['f1'])} | "
        f"self_recall={_metric(identity_metrics['recall'])} | "
        f"key_fact_recall={_metric(fact_metrics['recall'])}"
    )
    console.print(f"报告：{summary.report_markdown_path.resolve()}")


@benchmark_app.command(name="migrate-v1-truth")
def benchmark_migrate_v1_truth(
    truth: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=True,
        dir_okay=False,
        resolve_path=True,
        help="现有 AllDayRecording evaluation truth v1 JSONL。",
    ),
    name: Optional[str] = typer.Option(None, help="新的连续时间真值集名称。"),
    output: Optional[Path] = typer.Option(
        None, help="新 JSONL 路径；默认写在旧真值旁且不覆盖。"
    ),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """将 segment 绑定的旧标注无损映射到 source/session 连续时间。"""
    summary = migrate_legacy_truth(
        Database(db), truth, name=name, output_path=output
    )
    console.print(
        f"[green]连续时间真值已冻结[/green] truth_set={summary.truth_set_id}，"
        f"annotations={summary.annotation_count}"
    )
    console.print(f"真值：{summary.output_path.resolve()}")
    console.print(f"SHA-256：{summary.truth_sha256}")


@benchmark_app.command(name="init-truth")
def benchmark_init_truth(
    session_id: int = typer.Argument(..., min=1, help="录音会话 ID。"),
    name: str = typer.Option(..., help="连续时间真值名称。"),
    start: str = typer.Option("0", help="开始偏移：秒、MM:SS 或 HH:MM:SS。"),
    end: Optional[str] = typer.Option(None, help="结束偏移；默认到会话结束。"),
    output: Optional[Path] = typer.Option(None, help="自定义 JSONL 输出路径。"),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """创建不依赖 VAD segment、尚未冻结的连续时间真值模板。"""
    try:
        start_ms = parse_offset(start)
        end_ms = parse_offset(end) if end is not None else None
        path = create_continuous_truth_template(
            Database(db),
            session_id,
            name=name,
            start_ms=start_ms,
            end_ms=end_ms,
            output_path=output,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(f"[green]连续时间真值模板已创建[/green] {path.resolve()}")
    console.print("填写 annotation 后运行：allday-asr benchmark import-truth <path>")


@benchmark_app.command(name="init-blind")
def benchmark_init_blind(
    session_id: int = typer.Argument(..., min=1, help="录音会话 ID。"),
    name: str = typer.Option(..., help="盲标任务/真值名称。"),
    duration: str = typer.Option("30:00", help="连续盲标时长。"),
    chunk: str = typer.Option("5:00", help="仅用于人工试听的派生 WAV 分块时长。"),
    seed: str = typer.Option("20260828", help="可审计的盲选种子。"),
    output_dir: Optional[Path] = typer.Option(None, help="自定义任务目录。"),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """从未查看模型输出的连续范围建立 V2-C.1 盲标任务。"""
    try:
        summary = create_blind_truth_task(
            Database(db),
            session_id,
            name=name,
            duration_ms=parse_offset(duration),
            chunk_ms=parse_offset(chunk),
            seed=seed,
            output_dir=output_dir,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(
        f"[green]V2-C.1 盲标任务已创建[/green] "
        f"scope={summary.scope_start_ms}–{summary.scope_end_ms} ms | "
        f"windows={len(summary.audio_paths)}"
    )
    console.print(f"任务：{summary.task_path.resolve()}")
    console.print("标注前不要查看任何模型输出；具体格式见同目录 README.md。")


@benchmark_app.command(name="annotate-blind")
def benchmark_annotate_blind(
    task: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=True,
        dir_okay=False,
        resolve_path=True,
        help="V2-C.1 truth-draft.jsonl 路径。",
    ),
    port: int = typer.Option(8766, min=1, max=65535, help="本地盲标网页端口。"),
    open_browser: bool = typer.Option(
        True,
        "--open/--no-open",
        help="启动后是否自动用默认浏览器打开。",
    ),
) -> None:
    """启动隔离的盲标网页；不连接数据库，也不读取任何模型输出。"""
    serve_blind_annotation(
        task,
        host="127.0.0.1",
        port=port,
        open_browser=open_browser,
    )


@benchmark_app.command(name="init-blind-v2c2")
def benchmark_init_blind_v2c2(
    session_id: int = typer.Argument(..., min=1, help="录音会话 ID。"),
    name: str = typer.Option(..., help="V2-C.2 盲标任务/真值名称。"),
    review_duration: str = typer.Option("10:00", help="实际需要人工复核的总时长。"),
    chunk: str = typer.Option("1:00", help="分散声学候选块的长度。"),
    minimum_gap: str = typer.Option("1:00", help="候选块之间至少留出的间隔。"),
    seed: str = typer.Option(
        "v2c2-primary-20260828", help="声学排序同分时使用的固定种子。"
    ),
    output_dir: Optional[Path] = typer.Option(None, help="自定义任务目录。"),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """用原始波形声学活动度生成 V2-C.2 语音富集盲标任务。"""
    try:
        summary = create_acoustic_blind_truth_task(
            Database(db),
            session_id,
            name=name,
            review_duration_ms=parse_offset(review_duration),
            chunk_ms=parse_offset(chunk),
            minimum_gap_ms=parse_offset(minimum_gap),
            seed=seed,
            output_dir=output_dir,
        )
    except (RuntimeError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(
        f"[green]V2-C.2 声学富集盲标任务已创建[/green] "
        f"bounding-scope={summary.scope_start_ms}–{summary.scope_end_ms} ms | "
        f"windows={len(summary.audio_paths)}"
    )
    console.print(f"任务：{summary.task_path.resolve()}")
    console.print("选择过程只读取原始波形特征，不读取 VAD/ASR/旧转写。")


@benchmark_app.command(name="snapshot-v1")
def benchmark_snapshot_v1(
    truth_set_id: int = typer.Argument(..., min=1, help="连续时间真值集 ID。"),
    name: str = typer.Option(..., help="不可变预测快照名称。"),
    processing_run_id: Optional[int] = typer.Option(
        None, min=1, help="关联的 processing run；默认使用最近一次。"
    ),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """冻结当前 V1 segments、转写、人物标签，防止后续结果漂移。"""
    summary = snapshot_v1_predictions(
        Database(db),
        truth_set_id,
        name=name,
        processing_run_id=processing_run_id,
    )
    console.print(
        f"[green]预测快照已创建[/green] prediction_set={summary.prediction_set_id}，"
        f"predictions={summary.prediction_count}"
    )
    console.print(f"内容 SHA-256：{summary.content_sha256}")


@benchmark_app.command(name="freeze-completed")
def benchmark_freeze_completed(
    task: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=True,
        dir_okay=False,
        resolve_path=True,
        help="尚未全部完成的 V2-C.2 truth-draft.jsonl。",
    ),
    name: str = typer.Option(..., help="明确标记为 preliminary 的预备真值名称。"),
    output: Optional[Path] = typer.Option(None, help="派生 JSONL 输出路径。"),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """冻结已完整检查的窗口；未检查窗口保持排除，不伪装成完整 V2-C.2。"""
    summary = freeze_completed_blind_subset(
        Database(db),
        task,
        name=name,
        output_path=output,
    )
    console.print(
        f"[green]已完成窗口的预备真值已冻结[/green] "
        f"truth_set={summary.truth_set_id} | annotations={summary.annotation_count}"
    )
    console.print(f"真值：{summary.output_path.resolve()}")
    console.print(f"SHA-256：{summary.truth_sha256}")


@benchmark_app.command(name="oracle-asr")
def benchmark_oracle_asr(
    truth_set_id: int = typer.Argument(..., min=1, help="冻结真值集 ID。"),
    model: str = typer.Option(..., help="sensevoice、qwen 或 fun。"),
    name: str = typer.Option(..., help="不可变预测快照名称。"),
    language: str = typer.Option("zh", help="传给所有模型的统一语言提示。"),
    device: str = typer.Option("auto", help="auto、cpu 或 cuda:0。"),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """在完全相同的人工 transcript 边界上运行一个纯 ASR 模型。"""
    try:
        backend = create_oracle_backend(model, device=device)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--model") from exc
    with Progress(
        TextColumn("{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("同边界 ASR：准备模型", total=None)

        def update(completed: int, total: int) -> None:
            progress.update(
                task,
                description=f"同边界 ASR：{completed}/{total}",
            )

        summary = snapshot_oracle_asr_predictions(
            Database(db),
            truth_set_id,
            backend,
            name=name,
            language=language,
            on_progress=update,
        )
    console.print(
        f"[green]Oracle ASR 快照已冻结[/green] "
        f"prediction_set={summary.prediction_set_id} | "
        f"intervals={summary.prediction_count}"
    )
    console.print(f"内容 SHA-256：{summary.content_sha256}")


@benchmark_app.command(name="import-truth")
def benchmark_import_truth(
    truth: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=True,
        dir_okay=False,
        resolve_path=True,
        help="AllDayRecording continuous truth v2 JSONL。",
    ),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """验证源哈希和时间映射后，将连续真值冻结入库。"""
    summary = import_continuous_truth(Database(db), truth)
    console.print(
        f"[green]连续时间真值已冻结[/green] truth_set={summary.truth_set_id}，"
        f"annotations={summary.annotation_count}，SHA-256={summary.truth_sha256}"
    )


@benchmark_app.command(name="run")
def benchmark_run(
    truth_set_id: int = typer.Argument(..., min=1),
    prediction_set_id: int = typer.Argument(..., min=1),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """计算 segmentation-independent CER、VAD、DER/JER、对齐和实体指标。"""
    summary = evaluate_benchmark(Database(db), truth_set_id, prediction_set_id)
    metrics = summary.metrics
    console.print(
        f"[green]Benchmark 完成[/green] run={summary.benchmark_run_id} | "
        f"CER={_metric(metrics['asr'].get('cer'))} | "
        f"ITN-CER={_metric(metrics['asr_itn'].get('cer'))} | "
        f"VAD-F1={_metric(metrics['vad'].get('f1'))} | "
        f"DER={_metric(metrics['speaker'].get('der'))} | "
        f"JER={_metric(metrics['speaker'].get('jer'))} | "
        f"entity-F1={_metric(metrics['entities']['extraction'].get('f1'))}"
    )
    console.print(f"报告：{summary.report_markdown_path.resolve()}")


@benchmark_app.command(name="compare")
def benchmark_compare(
    truth_set_id: int = typer.Argument(..., min=1),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """比较同一冻结真值和原始输入上的全部 benchmark run。"""
    rows = benchmark_comparison(Database(db), truth_set_id)
    table = Table(
        "Run",
        "Prediction",
        "Name",
        "CER",
        "ITN CER",
        "VAD F1",
        "DER",
        "JER",
        "Align ms",
        "Entity F1",
    )
    for row in rows:
        table.add_row(
            str(row["benchmark_run_id"]),
            str(row["prediction_set_id"]),
            row["prediction_name"],
            _metric(row["asr_cer"]),
            _metric(row["asr_itn_cer"]),
            _metric(row["vad_f1"]),
            _metric(row["der"]),
            _metric(row["jer"]),
            _metric(row["alignment_mean_ms"]),
            _metric(row["entity_f1"]),
        )
    console.print(table)


@benchmark_app.command(name="compare-oracle-pair")
def benchmark_compare_oracle_pair(
    truth_set_id: int = typer.Argument(..., min=1),
    baseline_prediction_set_id: int = typer.Argument(..., min=1),
    candidate_prediction_set_id: int = typer.Argument(..., min=1),
    samples: int = typer.Option(20_000, min=1, help="paired bootstrap 重采样次数。"),
    seed: int = typer.Option(20_260_828, help="统计重采样种子。"),
    itn: bool = typer.Option(False, help="使用保守 ITN 等价 CER。"),
    speech_source: Optional[str] = typer.Option(
        None,
        help="只比较 live_person、media_playback、mixed_live_media 或 unknown。",
    ),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """对两个同边界 ASR 快照做 paired bootstrap，不只看点估计。"""
    result = paired_oracle_bootstrap(
        Database(db),
        truth_set_id,
        baseline_prediction_set_id,
        candidate_prediction_set_id,
        samples=samples,
        seed=seed,
        itn_equivalent=itn,
        speech_source=speech_source,
    )
    interval = result["paired_bootstrap"]["confidence_interval_95"]
    console.print(
        f"source={result['speech_source'] or 'all'} | "
        f"baseline CER={result['baseline']['cer']:.4f} | "
        f"candidate CER={result['candidate']['cer']:.4f} | "
        f"delta={result['candidate_minus_baseline_cer']:+.4f} | "
        f"95% CI=[{interval[0]:+.4f}, {interval[1]:+.4f}] | "
        f"P(candidate better)="
        f"{result['paired_bootstrap']['candidate_better_probability']:.4f}"
    )
    console.print(f"interval wins：{result['interval_wins']}")


def _metric(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.4f}"


@library_app.command(name="sync")
def voice_library_sync(
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """将已有独立声纹登记同步进人物样本库。"""
    summary = sync_all_enrollments(Database(db))
    console.print(
        f"[green]声纹库已同步[/green] people={summary.people}，"
        f"enrollment_sources={summary.source_rows}"
    )
    console.print(f"总览：{summary.manifest_path.resolve()}")


@library_app.command(name="status")
def voice_library_status(
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """查看人物声纹库的数据量、状态和留出集。"""
    database = Database(db)
    write_library_manifests(database)
    table = Table(title="人物声纹库")
    for column in ("ID", "人物", "状态", "有效语音", "会话", "Embedding", "留出", "阈值"):
        table.add_column(column)
    for item in get_library_status(database):
        table.add_row(
            str(item.person_id),
            item.display_name,
            item.status,
            f"{item.effective_speech_seconds:.1f}s",
            str(item.accepted_sessions),
            str(item.active_embeddings),
            str(item.holdout_samples),
            f"{item.threshold:.3f}" if item.threshold is not None else "",
        )
    console.print(table)


@library_app.command(name="accumulate")
def voice_library_accumulate(
    recording_id: int = typer.Argument(..., min=1),
    split: str = typer.Option(
        "auto", help="人物样本进入 auto、accepted 或 holdout；整次会话不拆分。"
    ),
    max_session_seconds: float = typer.Option(
        60.0, min=5.0, help="一次会话最多吸收多少秒 accepted 人物语音。"
    ),
    device: str = typer.Option("auto", help="auto、cuda:0 或 cpu。"),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """从已人工标注的日常录音积累人物、留出和负样本。"""
    summary = accumulate_reviewed_samples(
        Database(db),
        recording_id,
        split=split,
        device=device,
        max_session_seconds=max_session_seconds,
    )
    console.print(
        f"[green]日常样本已积累[/green] person={summary.person_samples}，"
        f"negative={summary.negative_samples}，mixed={summary.mixed_samples}，"
        f"uncertain={summary.uncertain_samples}，embedded={summary.embedded_samples}"
    )
    console.print(
        f"profile_embeddings_added={summary.profile_embeddings_added} | "
        f"总览：{summary.manifest_path.resolve()}"
    )


@library_app.command(name="enroll-person")
def voice_library_enroll_person(
    name: str = typer.Argument(..., help="人物显示名称。"),
    inputs: list[Path] = typer.Argument(
        ..., exists=True, resolve_path=True,
        help="经本人同意、只有该人物声音的音频文件或目录。",
    ),
    device: str = typer.Option("auto", help="auto、cuda:0 或 cpu。"),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """为经同意的固定人物建立独立声纹并加入样本库。"""
    database = Database(db)
    summary = enroll_known_person(
        database, inputs, display_name=name, device=device
    )
    sync_person_enrollment(database, summary.profile_id)
    manifest = write_library_manifests(database)
    console.print(
        f"[green]人物声纹登记完成[/green] person_id={summary.profile_id}，"
        f"name={name}，speech={summary.speech_seconds:.1f}s，"
        f"embeddings={summary.accepted_embeddings}"
    )
    console.print(f"声纹库：{manifest.resolve()}")


@app.command()
def doctor() -> None:
    """检查 Python、FFmpeg、模型包和真实 CUDA 运算。"""
    table = Table(title="AllDayRecording-ASR 环境检查")
    table.add_column("项目")
    table.add_column("状态")
    table.add_column("详情")
    results = run_checks()
    for result in results:
        table.add_row(result.name, "[green]OK[/green]" if result.ok else "[red]FAIL[/red]", result.detail)
    console.print(table)
    if not all(item.ok for item in results):
        raise typer.Exit(code=1)


@app.command()
def ingest(
    audio: Path = typer.Argument(..., exists=True, file_okay=True, dir_okay=False, resolve_path=True),
    device: str = typer.Option("Huawei Watch", help="录音设备名称。"),
    timezone_name: str = typer.Option("Asia/Singapore", "--timezone", help="录制时区。"),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """读取元数据和 SHA-256，将录音去重入库。"""
    database = Database(db)
    result = ingest_recording(database, audio, device=device, timezone_name=timezone_name)
    row = result.recording
    state = "已创建" if result.created else "已存在（未重复导入）"
    console.print(f"[green]{state}[/green] recording_id={row['id']}")
    console.print(
        f"时长 {row['duration_ms'] / 1000:.2f}s | {row['codec']} | "
        f"{row['sample_rate']} Hz | {row['channels']} ch | {row['recorded_at']}"
    )


@app.command()
def recordings(
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """列出已入库录音和处理状态。"""
    database = Database(db)
    rows = database.list_recordings()
    table = Table(title="录音")
    for column in ("ID", "状态", "时长", "录制时间", "设备", "源文件"):
        table.add_column(column)
    for row in rows:
        table.add_row(
            str(row["id"]),
            row["status"],
            f"{row['duration_ms'] / 1000:.1f}s",
            row["recorded_at"],
            row["device"] or "",
            row["source_path"],
        )
    console.print(table)


@app.command()
def process(
    recording_id: int = typer.Argument(..., min=1),
    device: str = typer.Option("auto", help="auto、cuda:0 或 cpu。"),
    max_segments: Optional[int] = typer.Option(
        None, min=1, help="本次最多转写多少个待处理片段；用于冒烟测试。"
    ),
    force_vad: bool = typer.Option(False, help="重新运行 VAD；会清除已有片段结果。"),
    retry_failed: bool = typer.Option(False, help="将失败片段重置后重试。"),
    reprocess_asr: bool = typer.Option(False, help="清除已有 ASR 派生文字并全部重跑。"),
    language: str = typer.Option("zh", help="SenseVoice 语言提示；中文默认 zh，多语可用 auto。"),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """标准化录音、运行 VAD，并逐片段转写；可重复执行以续跑。"""
    database = Database(db)
    with Progress(
        SpinnerColumn(spinner_name="line"),
        TextColumn("{task.description}"),
        console=console,
    ) as progress_ui:
        task_id = progress_ui.add_task("准备处理……", total=None)

        def update(current: int, total: int, detail: str) -> None:
            progress_ui.update(task_id, description=f"ASR {current}/{total}  {detail}")

        summary = process_recording(
            database,
            recording_id,
            device=device,
            max_segments=max_segments,
            force_vad=force_vad,
            retry_failed=retry_failed,
            reprocess_asr=reprocess_asr,
            language=language,
            progress=update,
        )
    console.print(
        f"[green]处理完成[/green] VAD={summary.vad_segments}，本次 ASR={summary.processed_now}，"
        f"状态={summary.status_counts}，耗时={summary.elapsed_seconds:.1f}s"
    )
    if summary.rtf is not None:
        console.print(f"本次纯 ASR RTF={summary.rtf:.3f}")
    if summary.peak_vram_mib is not None:
        console.print(f"GPU 峰值显存={summary.peak_vram_mib:.0f} MiB")


@app.command()
def diarize(
    recording_id: int = typer.Argument(..., min=1),
    device: str = typer.Option("auto", help="auto、cuda:0 或 cpu。"),
    preset_speakers: Optional[int] = typer.Option(
        None, min=1, help="已知说话人数；未知时不设置。"
    ),
    min_speaker_seconds: float = typer.Option(
        1.5, min=0.5, help="短于该时长的片段标记为 unknown。"
    ),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """使用 CAM++ 为现有片段分配匿名说话人标签。"""
    database = Database(db)
    summary = diarize_recording(
        database,
        recording_id,
        device=device,
        preset_speakers=preset_speakers,
        min_segment_ms=round(min_speaker_seconds * 1000),
    )
    console.print(
        f"[green]说话人分离完成[/green] assigned={summary.assigned_segments}，"
        f"unknown={summary.unassigned_segments}，speakers={summary.speakers}"
    )
    console.print(
        f"质量过滤：短片段={summary.rejected_short}，弱聚类={summary.rejected_weak_cluster}"
    )
    if summary.centers_path:
        console.print(f"说话人中心：{summary.centers_path.resolve()}")


@app.command(name="speaker-samples")
def speaker_samples(
    recording_id: int = typer.Argument(..., min=1),
    per_speaker: int = typer.Option(3, min=1, max=30, help="每位说话人导出几段最长样本。"),
    speaker: Optional[str] = typer.Option(None, help="只导出指定标签，例如 speaker_01。"),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """导出各匿名说话人的代表性音频，供人工确认身份。"""
    summary = export_speaker_samples(
        Database(db), recording_id, per_speaker=per_speaker, speaker=speaker
    )
    console.print(f"[green]试听样本已生成[/green] {summary.directory.resolve()}")
    console.print(f"清单：{summary.manifest.resolve()} | {summary.speakers}")
    for label, compilation in summary.compilations.items():
        console.print(f"{label} 连续试听：{compilation.resolve()}")


@app.command(name="mark-self")
def mark_self(
    recording_id: int = typer.Argument(..., min=1),
    speaker: str = typer.Argument(..., help="已试听确认的标签，例如 speaker_03。"),
    name: str = typer.Option("我", help="时间线显示名称。"),
    confirmed_pure: bool = typer.Option(
        False,
        "--confirmed-pure",
        help="确认已逐段试听，聚类中只有本人声音。",
    ),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """经用户确认后，将当前录音的一位说话人登记为本人。"""
    if not confirmed_pure:
        console.print(
            "[red]拒绝登记[/red]：请先逐段试听确认该聚类只有本人声音，"
            "再添加 --confirmed-pure。"
        )
        raise typer.Exit(code=2)
    summary = mark_speaker_as_self(
        Database(db),
        recording_id,
        speaker,
        display_name=name,
        confirmed_pure=confirmed_pure,
    )
    console.print(
        f"[green]已登记本人[/green] profile_id={summary.profile_id}，"
        f"speaker={summary.speaker}，segments={summary.assigned_segments}"
    )
    console.print(f"声纹档案：{summary.voiceprint_path.resolve()}")


@app.command(name="enroll-self")
def enroll_self_command(
    inputs: list[Path] = typer.Argument(
        ...,
        exists=True,
        resolve_path=True,
        help="本人独立录音文件或包含音频的目录，可提供多个。",
    ),
    name: str = typer.Option("我", help="本人显示名称。"),
    device: str = typer.Option("auto", help="auto、cuda:0 或 cpu。"),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """从独立、纯净的本人录音建立多 embedding 声纹档案。"""
    summary = enroll_self(
        Database(db), inputs, display_name=name, device=device
    )
    console.print(
        f"[green]本人声纹登记完成[/green] profile_id={summary.profile_id}，"
        f"unique_files={summary.source_files}，duplicates={summary.duplicate_files}，"
        f"speech={summary.speech_seconds:.1f}s"
    )
    console.print(
        f"embeddings={summary.accepted_embeddings}/{summary.candidate_embeddings}，"
        f"centroid_median={summary.median_centroid_similarity:.3f}，"
        f"pairwise_p10={summary.pairwise_p10_similarity:.3f}"
    )
    console.print(f"声纹档案：{summary.voiceprint_path.resolve()}")


@app.command(name="self-candidates")
def self_candidates(
    recording_id: int = typer.Argument(..., min=1),
    threshold: float = typer.Option(0.70, min=0.01, max=1.0, help="严格候选阈值。"),
    top: int = typer.Option(20, min=1, max=100, help="导出多少个最高分片段。"),
    min_segment_seconds: float = typer.Option(
        2.0, min=0.8, help="参与候选评分的最短片段时长。"
    ),
    device: str = typer.Option("auto", help="auto、cuda:0 或 cpu。"),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """用本人声纹生成试听候选，不自动写入身份。"""
    summary = export_self_candidates(
        Database(db),
        recording_id,
        device=device,
        threshold=threshold,
        top=top,
        min_segment_ms=round(min_segment_seconds * 1000),
    )
    console.print(
        f"[green]本人候选已生成[/green] scored={summary.scored_segments}，"
        f"strict={summary.strict_candidates}，threshold={summary.threshold:.3f}"
    )
    console.print(f"试听清单：{summary.manifest_path.resolve()}")
    console.print(f"完整分数：{summary.json_path.resolve()}")


@app.command(name="import-self-review")
def import_self_review_command(
    recording_id: int = typer.Argument(..., min=1),
    review: Optional[Path] = typer.Option(
        None, exists=True, file_okay=True, dir_okay=False, resolve_path=True,
        help="人工修改后的候选 README；默认使用当前录音输出。",
    ),
    threshold: float = typer.Option(
        0.36, min=0.01, max=1.0, help="经人工标注校准的自动识别阈值。"
    ),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """导入人工身份标注，保存校准并应用本人身份。"""
    summary = import_self_review(
        Database(db), recording_id, review_path=review, threshold=threshold
    )
    console.print(
        f"[green]人工标注已导入[/green] annotations={summary.annotations}，"
        f"self={summary.self_confirmed}，negative={summary.negative_confirmed}，"
        f"mixed={summary.mixed}，uncertain={summary.uncertain}"
    )
    console.print(
        f"identity manual={summary.manual_assigned}，auto={summary.automatic_assigned}，"
        f"threshold={summary.threshold:.3f}"
    )
    console.print(f"结构化标注：{summary.annotations_path.resolve()}")
    console.print(f"校准结果：{summary.calibration_path.resolve()}")


@app.command(name="unmark-self")
def unmark_self_command(
    recording_id: int = typer.Argument(..., min=1),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """撤销误登记的本人身份；保留转写、匿名说话人标签和原始录音。"""
    summary = unmark_self(Database(db), recording_id)
    console.print(
        f"[green]本人标记已撤销[/green] profile_id={summary.profile_id}，"
        f"segments={summary.cleared_segments}，profile_deleted={summary.profile_deleted}"
    )
    if summary.voiceprint_path:
        console.print(
            f"声纹文件：{summary.voiceprint_path.resolve()} | "
            f"deleted={summary.voiceprint_deleted}"
        )


@app.command(name="audit-speakers")
def audit_speakers(
    recording_id: int = typer.Argument(..., min=1),
    min_speaker_seconds: float = typer.Option(
        1.5, min=0.5, help="短于该时长的片段改为 unknown。"
    ),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """对已有标签执行保守质量过滤，不重新运行模型。"""
    summary = audit_speaker_assignments(
        Database(db),
        recording_id,
        min_segment_ms=round(min_speaker_seconds * 1000),
    )
    console.print(
        f"[green]说话人质量审计完成[/green] kept={summary.kept_segments}，"
        f"short_to_unknown={summary.rejected_short}，"
        f"weak_cluster_to_unknown={summary.rejected_weak_cluster}，"
        f"speakers={summary.speakers}"
    )


@app.command()
def timeline(
    recording_id: int = typer.Argument(..., min=1),
    max_gap_seconds: float = typer.Option(
        120.0, min=1.0, help="相邻片段超过此间隔时开始新事件。"
    ),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """聚合对话事件，并生成事件级 JSON 和 Markdown 时间线。"""
    summary = build_timeline(
        Database(db), recording_id, max_gap_seconds=max_gap_seconds
    )
    console.print(
        f"[green]时间线已生成[/green] events={summary.event_count}，"
        f"segments={summary.segment_count}"
    )
    console.print(f"Markdown：{summary.markdown_path.resolve()}")
    console.print(f"JSON：{summary.json_path.resolve()}")


@app.command(name="export")
def export_results(
    recording_id: int = typer.Argument(..., min=1),
    output_format: str = typer.Option("jsonl", "--format", help="jsonl 或 markdown。"),
    output: Optional[Path] = typer.Option(None, help="输出文件路径。"),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """导出已完成的转写结果。"""
    database = Database(db)
    normalized_format = output_format.lower()
    if normalized_format not in {"jsonl", "markdown", "md"}:
        raise typer.BadParameter("--format 只能是 jsonl 或 markdown")
    extension = "jsonl" if normalized_format == "jsonl" else "md"
    destination = output or recording_output_dir(recording_id) / f"transcript.{extension}"
    if normalized_format == "jsonl":
        export_jsonl(database, recording_id, destination)
    else:
        export_markdown(database, recording_id, destination)
    console.print(f"[green]已导出[/green] {destination.resolve()}")


@app.command()
def clip(
    segment_id: int = typer.Argument(..., min=1),
    output: Optional[Path] = typer.Option(None, help="输出 WAV 路径。"),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """按转写片段从原始录音截取一段可回听 WAV。"""
    database = Database(db)
    segment = database.get_segment(segment_id)
    recording = database.get_recording(int(segment["recording_id"]))
    destination = output or recording_output_dir(int(recording["id"])) / "clips" / f"segment-{segment_id}.wav"
    extract_clip(
        Path(recording["source_path"]),
        destination,
        int(segment["start_ms"]),
        int(segment["end_ms"]),
    )
    console.print(f"[green]已生成[/green] {destination.resolve()}")


if __name__ == "__main__":
    app()
