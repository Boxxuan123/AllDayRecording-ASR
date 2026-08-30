from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from allday_asr.config import load_config
from allday_asr.application.diarization.identity_audit import (
    run_identity_contamination_audit,
)
from allday_asr.application.diarization.identity_candidates import (
    V2D3Settings,
    run_identity_candidate_mining,
    sync_identity_reference_set,
)
from allday_asr.application.diarization.pipeline import (
    compute_overlap_regions,
    run as run_quality_diarization,
    snapshot_quality_diarization,
)
from allday_asr.application.diarization.speech_recall import (
    V2D1Settings,
    create_source_micro_truth,
    run_quality_diarization_v2d1,
)
from allday_asr.diarization.quality_backends import SpeakerTurn
from allday_asr.interfaces.cli.runtime import build_quality_diarization_runtime
from allday_asr.interfaces.cli.targets import resolve_v2_target
from allday_asr.paths import DEFAULT_CONFIG_PATH, DEFAULT_DB_PATH
from allday_asr.services.evaluation import parse_offset
from allday_asr.storage.database import Database

app = typer.Typer(
    help="V2-D 重叠感知说话人时间轴和 ASR token 归属。",
    no_args_is_help=True,
)
console = Console()

@app.command(name="run")
def quality_diarization_run(
    recording_id: Optional[int] = typer.Argument(
        None, min=1, help="旧单文件 recording_id；原生分片会话改用 --session。"
    ),
    session_id: Optional[int] = typer.Option(
        None, "--session", min=1, help="原生 V2 录音会话 ID。"
    ),
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
    database = Database.open(db)
    recording_id, session_id = resolve_v2_target(
        database, recording_id, session_id
    )
    if asr_run_id is None:
        candidates = [
            row
            for row in database.list_session_processing_runs(session_id)
            if str(row["run_kind"]) == "quality_asr_v2c"
            and str(row["status"]) == "completed"
        ]
        if not candidates:
            raise typer.BadParameter("该录音没有已完成的 V2-C ASR run")
        asr_run_id = int(candidates[-1]["id"])
    runtime = build_quality_diarization_runtime(
        resolved,
        model_path=model_path,
        num_speakers=num_speakers,
        min_speakers=min_speakers,
        max_speakers=max_speakers,
    )
    try:
        summary = run_quality_diarization(
            database,
            recording_id,
            session_id=session_id,
            asr_run_id=asr_run_id,
            settings=runtime.settings,
            backend_factory=runtime.backend_factory,
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


@app.command(name="status")
def quality_diarization_status(
    run_id: int = typer.Argument(..., min=1),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """查看说话人时长、重叠区间和 token 归属统计。"""
    database = Database.open(db)
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


@app.command(name="snapshot")
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
        Database.open(db), run_id, name=name, truth_set_id=truth_set_id
    )
    console.print(
        f"[green]V2-D 预测已冻结[/green] prediction_set="
        f"{summary.prediction_set_id} | speakers={summary.speaker_predictions} | "
        f"overlap={summary.overlap_predictions} | sha256={summary.content_sha256}"
    )


@app.command(name="refine")
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
    database = Database.open(db)
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


@app.command(name="source-truth")
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
        Database.open(db),
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


@app.command(name="identity-audit")
def quality_diarization_identity_audit(
    recording_id: int = typer.Argument(..., min=1),
    truth_set_id: int = typer.Option(
        ...,
        "--truth-set",
        min=1,
        help="包含人工 speaker 标签的冻结真值集。",
    ),
    diarization_run_id: int | None = typer.Option(
        None,
        "--diarization-run",
        min=1,
        help="父 V2-D run；默认选择该录音最新完成的一次。",
    ),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """生成 V2-D.2 人工身份到匿名 speaker 的污染审计。"""
    database = Database.open(db)
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
    summary = run_identity_contamination_audit(
        database,
        recording_id,
        diarization_run_id=diarization_run_id,
        truth_set_id=truth_set_id,
    )
    console.print(
        f"[green]V2-D.2 身份审计完成[/green] run={summary.run_id} | "
        f"truth={summary.truth_set_id} | coverage={summary.coverage:.2%} | "
        f"contaminated={','.join(summary.contaminated_speakers) or 'none'}"
    )
    for item in summary.human_speakers:
        mapping = ", ".join(
            f"{value['speaker']}={value['overlap_ms'] / 1000:.3f}s"
            for value in item["model_speakers"]
        )
        console.print(
            f"human={item['identity']} | truth={item['truth_ms'] / 1000:.3f}s | "
            f"covered={item['coverage']:.2%} | {mapping or 'no model speaker'}"
        )
    console.print(f"运行清单：{summary.manifest_path.resolve()}")


@app.command(name="mine-identities")
def quality_diarization_mine_identities(
    recording_id: int = typer.Argument(..., min=1),
    truth_set_id: int = typer.Option(
        ...,
        "--truth-set",
        min=1,
        help="包含目标人物和电视/其他人物负对照的冻结真值集。",
    ),
    identity: str = typer.Option(
        ...,
        "--identity",
        help="要扩样的人工身份标签，例如 father 或 mother。",
    ),
    diarization_run_id: int | None = typer.Option(
        None,
        "--diarization-run",
        min=1,
        help="父 V2-D run；默认选择该录音最新完成的一次。",
    ),
    max_candidates: int = typer.Option(
        12,
        "--max-candidates",
        min=1,
        max=100,
        help="写入试听队列的候选上限。",
    ),
    device: str = typer.Option("auto", help="auto、cuda:0 或 cpu。"),
    model_path: Path | None = typer.Option(
        None,
        "--model-path",
        exists=True,
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="可选的本地 Community-1 snapshot 路径。",
    ),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """用 Community-1 声纹和人工负对照生成 V2-D.3 弱种子试听队列。"""
    database = Database.open(db)
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
    summary = run_identity_candidate_mining(
        database,
        recording_id,
        diarization_run_id=diarization_run_id,
        truth_set_id=truth_set_id,
        target_identity=identity,
        settings=V2D3Settings(max_candidates=max_candidates),
        device=device,
        model_path=model_path,
    )
    console.print(
        f"[green]V2-D.3 弱种子候选完成[/green] run={summary.run_id} | "
        f"identity={summary.target_identity} | seed={summary.target_truth_ms / 1000:.3f}s/"
        f"{summary.target_embedding_count} embeddings | quality={summary.seed_quality} | "
        f"selected={summary.selected_candidates}/{summary.scored_candidate_windows}"
    )
    console.print(
        "[yellow]这些分数未经身份阈值校准，只用于决定先听哪一段；"
        "没有写入任何人物身份或正式声纹。[/yellow]"
    )
    console.print(f"运行清单：{summary.manifest_path.resolve()}")


@app.command(name="sync-identity-references")
def quality_diarization_sync_identity_references(
    identity: str = typer.Option(
        ...,
        "--identity",
        help="要汇总的人工身份标签，例如 father 或 mother。",
    ),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """把跨 run 人工结论索引为可跨录音复用的原音区间。"""
    summary = sync_identity_reference_set(Database.open(db), identity)
    console.print(
        f"[green]人物参考集已同步[/green] identity={summary.identity_label} | "
        f"confirmed={summary.confirmed_intervals}/"
        f"{summary.confirmed_duration_ms / 1000:.3f}s | "
        f"rejected={summary.rejected_intervals} | "
        f"sessions={summary.sessions} | sources={summary.source_objects}"
    )
    console.print(
        "[yellow]这里只保存永久原音的 SHA-256 和时间坐标；"
        "没有复制音频、生成正式声纹或自动绑定人物。[/yellow]"
    )
