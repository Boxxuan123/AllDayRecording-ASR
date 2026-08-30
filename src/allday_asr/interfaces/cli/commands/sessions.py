from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from allday_asr.config import load_config
from allday_asr.paths import DEFAULT_CONFIG_PATH, DEFAULT_DB_PATH
from allday_asr.services.session_backup import (
    create_session_backup,
    verify_session_backup,
)
from allday_asr.services.session_ingest import ingest_session_manifest
from allday_asr.services.session_readiness import evaluate_session_readiness
from allday_asr.storage.database import Database

app = typer.Typer(
    help="导入和检查由多个不可变原始分片组成的录音会话。",
    no_args_is_help=True,
)
console = Console()

@app.command(name="import-manifest")
def session_import_manifest(
    manifest: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=True,
        dir_okay=False,
        resolve_path=True,
        help="采集端生成的会话 JSON 清单。",
    ),
    method: str = typer.Option(
        "watch_manual_sync",
        "--method",
        help="watch_manual_sync 或 watch_auto。",
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
    """完整预检所有分片后，在一个事务中创建并关闭不可变会话。"""
    resolved = load_config(config)
    summary = ingest_session_manifest(
        Database.open(db),
        manifest,
        device=resolved.ingest.device,
        timezone_name=resolved.ingest.timezone,
        ingest_method=method,
    )
    state = "已创建" if summary.created else "已存在（清单哈希一致）"
    console.print(
        f"[green]{state}[/green] session_id={summary.session_id} | "
        f"chunks={summary.chunk_count} | duration={summary.duration_ms / 1000:.3f}s | "
        f"gaps={summary.gap_count} | overlaps={summary.overlap_count}"
    )
    console.print(f"input_fingerprint={summary.input_fingerprint}")
    console.print(f"manifest_sha256={summary.manifest_sha256}")


@app.command(name="list")
def session_list(
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """列出单文件兼容会话和原生多分片会话。"""
    database = Database.open(db)
    table = Table("Session", "Kind", "Status", "Duration", "Chunks", "Key")
    for session in database.list_recording_sessions():
        session_id = int(session["id"])
        manifest = database.get_session_manifest(session_id)
        table.add_row(
            str(session_id),
            "manifest" if manifest is not None else "legacy-file",
            str(session["status"]),
            f"{int(session['duration_ms']) / 1000:.3f}s",
            str(len(database.list_session_sources(session_id))),
            str(session["session_key"]),
        )
    console.print(table)


@app.command(name="backup")
def session_backup(
    session_id: int = typer.Argument(..., min=1, help="已经关闭的录音会话 ID。"),
    destination: Path = typer.Argument(
        ...,
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="独立磁盘、移动设备或网络存储的目标根目录。",
    ),
    storage_kind: str = typer.Option(
        ...,
        "--storage-kind",
        help="independent_device、network 或 same_device_test。",
    ),
    restore_drill: bool = typer.Option(
        True,
        "--restore-drill/--no-restore-drill",
        help="复制后从备份临时恢复并逐文件复算 SHA-256。",
    ),
    restore_probe_root: Optional[Path] = typer.Option(
        None,
        "--restore-probe-root",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="恢复演练临时目录；默认使用系统临时目录。",
    ),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """创建不覆盖已有文件的逐实例原音备份，并保存验证证据。"""
    try:
        summary = create_session_backup(
            Database.open(db),
            session_id,
            destination,
            storage_kind=storage_kind,
            restore_drill=restore_drill,
            restore_probe_root=restore_probe_root,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        console.print(f"[red]会话备份未完成[/red]：{exc}")
        raise typer.Exit(code=2) from exc
    action = "已创建" if summary.created else "已复核既有"
    console.print(
        f"[green]{action}会话备份[/green] backup={summary.backup_id} | "
        f"files={summary.file_count} | bytes={summary.total_bytes} | "
        f"storage={summary.storage_kind} | restore={summary.restore_verified}"
    )
    console.print(f"备份目录：{summary.backup_path}")
    if summary.storage_kind == "same_device_test":
        console.print(
            "[yellow]same_device_test 只验证流程，不满足正式生产的独立副本要求。[/yellow]"
        )


@app.command(name="backup-verify")
def session_backup_verify(
    backup_id: int = typer.Argument(..., min=1, help="会话备份 ID。"),
    restore_drill: bool = typer.Option(
        True,
        "--restore-drill/--no-restore-drill",
        help="同时执行一次临时恢复演练。",
    ),
    restore_probe_root: Optional[Path] = typer.Option(
        None,
        "--restore-probe-root",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="恢复演练临时目录；默认使用系统临时目录。",
    ),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """重新读取备份并验证清单、每个文件和可恢复性。"""
    try:
        summary = verify_session_backup(
            Database.open(db),
            backup_id,
            restore_drill=restore_drill,
            restore_probe_root=restore_probe_root,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        console.print(f"[red]备份复核失败[/red]：{exc}")
        raise typer.Exit(code=2) from exc
    console.print(
        f"[green]备份复核通过[/green] backup={summary.backup_id} | "
        f"files={summary.file_count} | bytes={summary.total_bytes} | "
        f"restore={summary.restore_drill} | production={summary.production_grade}"
    )


@app.command(name="readiness")
def session_readiness(
    session_id: int = typer.Argument(..., min=1, help="录音会话 ID。"),
    verify_backups: bool = typer.Option(
        True,
        "--verify-backups/--no-verify-backups",
        help="重新读取生产备份并复算 SHA-256。",
    ),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """只做完整性和工程能力检查，不运行 VAD、ASR 或说话人模型。"""
    try:
        result = evaluate_session_readiness(
            Database.open(db), session_id, verify_backups=verify_backups
        )
    except (OSError, RuntimeError, ValueError) as exc:
        console.print(f"[red]会话准入检查失败[/red]：{exc}")
        raise typer.Exit(code=2) from exc
    color = {
        "production_ready": "green",
        "shadow_ready": "yellow",
        "blocked": "red",
    }[str(result["state"])]
    console.print(
        f"[{color}]state={result['state']}[/{color}] | "
        f"shadow={result['shadow_ready']} | production={result['production_ready']} | "
        f"duration={result['duration_ms'] / 1000:.3f}s"
    )
    for reason in result["blocking_reasons"]:
        console.print(f"[red]阻断[/red]：{reason}")
    for reason in result["production_blockers"]:
        console.print(f"[yellow]生产门槛[/yellow]：{reason}")
    for warning in result["warnings"]:
        console.print(f"[yellow]提示[/yellow]：{warning}")
    console.print(
        "[dim]本命令只读取文件字节进行哈希校验，不解码或分析音频内容。[/dim]"
    )

