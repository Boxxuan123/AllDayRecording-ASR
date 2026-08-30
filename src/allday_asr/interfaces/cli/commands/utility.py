from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from allday_asr.config import load_config
from allday_asr.paths import DEFAULT_CONFIG_PATH, DEFAULT_DB_PATH
from allday_asr.services.daily import run_daily
from allday_asr.services.sources import (
    audit_all_sources,
    audit_source_object,
    plan_logical_windows,
)
from allday_asr.storage.database import Database
from allday_asr.web import serve_web

app = typer.Typer()
console = Console()

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
    database = Database.open(db)
    console.print_json(data=resolved.to_dict())
    console.print(f"config_sha256={resolved.sha256()}")
    console.print(f"database_schema_version={database.schema_version()}")


@app.command(name="sources")
def source_objects(
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """列出永久保存的不可变原始音频对象及其 V2 会话。"""
    database = Database.open(db)
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
    database = Database.open(db)
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
    database = Database.open(db)
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
            Database.open(db), resolved_source, resolved_config, progress=update
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
    rows = Database.open(db).list_action_candidates(recording_id, status=status)
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
        row = Database.open(db).review_action_candidate(
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

