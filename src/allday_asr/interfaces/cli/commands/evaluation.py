from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from allday_asr.interfaces.cli.output import metric
from allday_asr.paths import DEFAULT_DB_PATH
from allday_asr.services.evaluation import (
    create_evaluation_template,
    evaluate_truth,
    parse_offset,
)
from allday_asr.storage.database import Database

app = typer.Typer(help="建立人工真值并评测离线结果。", no_args_is_help=True)
console = Console()

@app.command(name="init")
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
        Database.open(db),
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


@app.command(name="run")
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
    summary = evaluate_truth(Database.open(db), truth)
    text_metrics = summary.metrics["text"]
    speaker_metrics = summary.metrics["speaker_pairwise"]
    identity_metrics = summary.metrics["self_identity"]
    fact_metrics = summary.metrics["key_facts"]
    console.print(
        f"[green]评测完成[/green] run_id={summary.evaluation_run_id}，"
        f"segments={summary.item_count}"
    )
    console.print(
        f"CER={metric(text_metrics['cer'])} | "
        f"speaker_pair_f1={metric(speaker_metrics['f1'])} | "
        f"self_recall={metric(identity_metrics['recall'])} | "
        f"key_fact_recall={metric(fact_metrics['recall'])}"
    )
    console.print(f"报告：{summary.report_markdown_path.resolve()}")

