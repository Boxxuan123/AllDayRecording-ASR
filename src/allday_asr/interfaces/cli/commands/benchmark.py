from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.progress import Progress, TextColumn
from rich.table import Table

from allday_asr.blind_web import serve_blind_annotation
from allday_asr.interfaces.cli.output import metric
from allday_asr.interfaces.cli.runtime import build_oracle_backend
from allday_asr.paths import DEFAULT_DB_PATH
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
from allday_asr.services.evaluation import parse_offset
from allday_asr.storage.database import Database

app = typer.Typer(
    help="管理连续时间真值、不可变预测快照和多 run 对比。",
    no_args_is_help=True,
)
console = Console()

@app.command(name="migrate-v1-truth")
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
        Database.open(db), truth, name=name, output_path=output
    )
    console.print(
        f"[green]连续时间真值已冻结[/green] truth_set={summary.truth_set_id}，"
        f"annotations={summary.annotation_count}"
    )
    console.print(f"真值：{summary.output_path.resolve()}")
    console.print(f"SHA-256：{summary.truth_sha256}")


@app.command(name="init-truth")
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
            Database.open(db),
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


@app.command(name="init-blind")
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
            Database.open(db),
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


@app.command(name="annotate-blind")
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


@app.command(name="init-blind-v2c2")
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
            Database.open(db),
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


@app.command(name="snapshot-v1")
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
        Database.open(db),
        truth_set_id,
        name=name,
        processing_run_id=processing_run_id,
    )
    console.print(
        f"[green]预测快照已创建[/green] prediction_set={summary.prediction_set_id}，"
        f"predictions={summary.prediction_count}"
    )
    console.print(f"内容 SHA-256：{summary.content_sha256}")


@app.command(name="freeze-completed")
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
        Database.open(db),
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


@app.command(name="oracle-asr")
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
        backend = build_oracle_backend(model, device=device)
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
            Database.open(db),
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


@app.command(name="import-truth")
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
    summary = import_continuous_truth(Database.open(db), truth)
    console.print(
        f"[green]连续时间真值已冻结[/green] truth_set={summary.truth_set_id}，"
        f"annotations={summary.annotation_count}，SHA-256={summary.truth_sha256}"
    )


@app.command(name="run")
def benchmark_run(
    truth_set_id: int = typer.Argument(..., min=1),
    prediction_set_id: int = typer.Argument(..., min=1),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """计算 segmentation-independent CER、VAD、DER/JER、对齐和实体指标。"""
    summary = evaluate_benchmark(Database.open(db), truth_set_id, prediction_set_id)
    metrics = summary.metrics
    console.print(
        f"[green]Benchmark 完成[/green] run={summary.benchmark_run_id} | "
        f"CER={metric(metrics['asr'].get('cer'))} | "
        f"ITN-CER={metric(metrics['asr_itn'].get('cer'))} | "
        f"VAD-F1={metric(metrics['vad'].get('f1'))} | "
        f"DER={metric(metrics['speaker'].get('der'))} | "
        f"JER={metric(metrics['speaker'].get('jer'))} | "
        f"entity-F1={metric(metrics['entities']['extraction'].get('f1'))}"
    )
    console.print(f"报告：{summary.report_markdown_path.resolve()}")


@app.command(name="compare")
def benchmark_compare(
    truth_set_id: int = typer.Argument(..., min=1),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """比较同一冻结真值和原始输入上的全部 benchmark run。"""
    rows = benchmark_comparison(Database.open(db), truth_set_id)
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
            metric(row["asr_cer"]),
            metric(row["asr_itn_cer"]),
            metric(row["vad_f1"]),
            metric(row["der"]),
            metric(row["jer"]),
            metric(row["alignment_mean_ms"]),
            metric(row["entity_f1"]),
        )
    console.print(table)


@app.command(name="compare-oracle-pair")
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
        Database.open(db),
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


