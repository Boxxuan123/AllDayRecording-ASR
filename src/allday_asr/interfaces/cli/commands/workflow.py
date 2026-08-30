from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from allday_asr.application.semantic.pipeline import (
    SemanticSettings as SemanticV2E02Settings,
)
from allday_asr.config import load_config
from allday_asr.interfaces.cli.runtime import (
    build_quality_asr_runtime,
    build_quality_diarization_runtime,
)
from allday_asr.interfaces.cli.targets import resolve_v2_target
from allday_asr.paths import DEFAULT_CONFIG_PATH, DEFAULT_DB_PATH
from allday_asr.services.quality_workflow import run_quality_workflow
from allday_asr.storage.database import Database

app = typer.Typer(
    help="按会话持久编排 V2-C、V2-D 和本地 V2-E.0.2。",
    no_args_is_help=True,
)
console = Console()

@app.command(name="run")
def quality_workflow_run(
    recording_id: Optional[int] = typer.Argument(
        None, min=1, help="旧单文件 recording_id；原生分片会话改用 --session。"
    ),
    session_id: Optional[int] = typer.Option(
        None, "--session", min=1, help="已经关闭并冻结的录音会话 ID。"
    ),
    shadow: bool = typer.Option(
        False,
        "--shadow",
        help="显式允许无独立备份或超出已验证时长的受监控实验运行。",
    ),
    profile: Optional[str] = typer.Option(
        None, help="auto、quality-16gb 或 compatible-8gb。"
    ),
    diarization_model_path: Optional[Path] = typer.Option(
        None,
        "--diarization-model-path",
        exists=True,
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="已经下载好的 Community-1 本地 snapshot。",
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
    """运行质量 ASR、说话人、自动增强审计和本地语义证据。"""
    resolved = load_config(config)
    database = Database.open(db)
    recording_id, session_id = resolve_v2_target(
        database, recording_id, session_id
    )
    try:
        asr_runtime = build_quality_asr_runtime(
            resolved,
            requested_profile=profile,
        )
        diarization_runtime = build_quality_diarization_runtime(
            resolved,
            model_path=diarization_model_path,
        )
        summary = run_quality_workflow(
            database,
            recording_id,
            session_id=session_id,
            asr_settings=asr_runtime.settings,
            diarization_settings=diarization_runtime.settings,
            semantic_settings=SemanticV2E02Settings(),
            primary_factory=asr_runtime.primary_factory,
            secondary_factory=asr_runtime.secondary_factory,
            diarization_factory=diarization_runtime.backend_factory,
            admission_mode="shadow" if shadow else "production",
            progress=lambda stage, detail: console.print(
                f"[cyan]{stage}[/cyan] {detail}"
            ),
        )
    except (RuntimeError, ValueError) as exc:
        console.print(f"[red]V2 工作流未完成[/red]：{exc}")
        raise typer.Exit(code=2) from exc
    console.print(
        f"[green]V2 工作流完成[/green] workflow_run={summary.workflow_run_id} | "
        f"state={summary.state} | ASR={summary.asr_run_id} | "
        f"D={summary.diarization_run_id} | D.1={summary.v2d1_run_id or 'failed'} | "
        f"D.2={summary.v2d2_run_id or 'skipped'} | "
        f"E={summary.semantic_run_id or 'skipped'}"
    )
    if summary.reused_stages:
        console.print(f"复用阶段：{', '.join(summary.reused_stages)}")
    if shadow:
        console.print(
            "[yellow]本次为显式 shadow 运行，不代表已满足独立备份和正式生产门槛。[/yellow]"
        )
    if summary.review_required:
        console.print("[yellow]增强证据需要人工检查：[/yellow]")
        for reason in summary.review_reasons:
            console.print(f"[yellow]- {reason['message']}[/yellow]")
    console.print(
        "[yellow]本地证据链已完成；没有调用云端 LLM，也没有修改或生成替代原音。[/yellow]"
    )


@app.command(name="status")
def quality_workflow_status(
    session_id: int = typer.Argument(..., min=1, help="录音会话 ID。"),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """显示 SQLite 中持久保存的 V2 工作流状态。"""
    database = Database.open(db)
    rows = [
        row
        for row in database.list_session_processing_runs(session_id)
        if str(row["run_kind"]) == "quality_workflow_v2"
    ]
    table = Table("Run", "Status", "Workflow state", "Started", "Completed")
    for row in rows:
        summary = json.loads(str(row["summary_json"] or "{}"))
        table.add_row(
            str(row["id"]),
            str(row["status"]),
            str(summary.get("workflow_state") or "unknown"),
            str(row["started_at"]),
            str(row["completed_at"] or "-"),
        )
    console.print(table)
