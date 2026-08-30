from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from allday_asr.audio.tools import extract_clip
from allday_asr.doctor import run_checks
from allday_asr.exporters import export_jsonl, export_markdown
from allday_asr.paths import DEFAULT_DB_PATH, recording_output_dir
from allday_asr.services.diarization import (
    audit_speaker_assignments,
    diarize_recording,
)
from allday_asr.services.enrollment import enroll_self
from allday_asr.services.ingest import ingest_recording
from allday_asr.services.processing import process_recording
from allday_asr.services.review import import_self_review
from allday_asr.services.speakers import (
    export_speaker_samples,
    mark_speaker_as_self,
    unmark_self,
)
from allday_asr.services.timeline import build_timeline
from allday_asr.services.verification import export_self_candidates
from allday_asr.storage.database import Database

app = typer.Typer()
console = Console()

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
    database = Database.open(db)
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
    database = Database.open(db)
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
    database = Database.open(db)
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
    database = Database.open(db)
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
        Database.open(db), recording_id, per_speaker=per_speaker, speaker=speaker
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
        Database.open(db),
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
        Database.open(db), inputs, display_name=name, device=device
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
        Database.open(db),
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
        Database.open(db), recording_id, review_path=review, threshold=threshold
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
    summary = unmark_self(Database.open(db), recording_id)
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
        Database.open(db),
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
        Database.open(db), recording_id, max_gap_seconds=max_gap_seconds
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
    database = Database.open(db)
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
    database = Database.open(db)
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

