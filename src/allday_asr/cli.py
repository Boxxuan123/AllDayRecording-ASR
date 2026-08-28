from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from allday_asr.audio.tools import extract_clip
from allday_asr.config import load_config
from allday_asr.doctor import run_checks
from allday_asr.exporters import export_jsonl, export_markdown
from allday_asr.paths import DEFAULT_CONFIG_PATH, DEFAULT_DB_PATH, recording_output_dir
from allday_asr.services.daily import run_daily
from allday_asr.services.evaluation import (
    create_evaluation_template,
    evaluate_truth,
    parse_offset,
)
from allday_asr.services.ingest import ingest_recording
from allday_asr.services.enrollment import enroll_known_person, enroll_self
from allday_asr.services.diarization import audit_speaker_assignments, diarize_recording
from allday_asr.services.processing import process_recording
from allday_asr.services.speakers import (
    export_speaker_samples,
    mark_speaker_as_self,
    unmark_self,
)
from allday_asr.services.timeline import build_timeline
from allday_asr.services.verification import export_self_candidates
from allday_asr.services.review import import_self_review
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
