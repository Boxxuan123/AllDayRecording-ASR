from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from allday_asr.config import load_config
from allday_asr.interfaces.cli.runtime import build_quality_asr_runtime
from allday_asr.interfaces.cli.targets import resolve_v2_target
from allday_asr.paths import DEFAULT_CONFIG_PATH, DEFAULT_DB_PATH
from allday_asr.services.quality_asr import (
    run_quality_asr,
    snapshot_quality_asr,
)
from allday_asr.storage.database import Database

app = typer.Typer(
    help="V2-C 质量优先双模型转写、强制对齐与分歧队列。",
    no_args_is_help=True,
)
console = Console()

@app.command(name="run")
def quality_asr_run(
    recording_id: Optional[int] = typer.Argument(
        None, min=1, help="旧单文件 recording_id；原生分片会话改用 --session。"
    ),
    session_id: Optional[int] = typer.Option(
        None, "--session", min=1, help="原生 V2 录音会话 ID。"
    ),
    profile: Optional[str] = typer.Option(
        None, help="auto、quality-16gb 或 compatible-8gb。"
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
    database = Database.open(db)
    recording_id, session_id = resolve_v2_target(
        database, recording_id, session_id
    )
    try:
        runtime = build_quality_asr_runtime(
            resolved,
            requested_profile=profile,
            max_windows=max_windows,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    summary = run_quality_asr(
        database,
        recording_id,
        session_id=session_id,
        settings=runtime.settings,
        primary_factory=runtime.primary_factory,
        secondary_factory=runtime.secondary_factory,
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


@app.command(name="status")
def quality_asr_status(
    run_id: int = typer.Argument(..., min=1),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """查看 V2-C run 的逐窗口模型证据和分歧优先级。"""
    database = Database.open(db)
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


@app.command(name="snapshot")
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
        Database.open(db), run_id, name=name, truth_set_id=truth_set_id
    )
    console.print(
        f"[green]V2-C 预测已冻结[/green] prediction_set={summary.prediction_set_id} | "
        f"predictions={summary.prediction_count} | sha256={summary.content_sha256}"
    )
