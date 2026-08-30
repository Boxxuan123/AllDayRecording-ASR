from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from allday_asr.paths import DEFAULT_DB_PATH
from allday_asr.services.enrollment import enroll_known_person
from allday_asr.services.voice_library import (
    accumulate_reviewed_samples,
    get_library_status,
    sync_all_enrollments,
    sync_person_enrollment,
    write_library_manifests,
)
from allday_asr.storage.database import Database

app = typer.Typer(help="管理本地人物声纹样本库。", no_args_is_help=True)
console = Console()

@app.command(name="sync")
def voice_library_sync(
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """将已有独立声纹登记同步进人物样本库。"""
    summary = sync_all_enrollments(Database.open(db))
    console.print(
        f"[green]声纹库已同步[/green] people={summary.people}，"
        f"enrollment_sources={summary.source_rows}"
    )
    console.print(f"总览：{summary.manifest_path.resolve()}")


@app.command(name="status")
def voice_library_status(
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """查看人物声纹库的数据量、状态和留出集。"""
    database = Database.open(db)
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


@app.command(name="accumulate")
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
        Database.open(db),
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


@app.command(name="enroll-person")
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
    database = Database.open(db)
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

