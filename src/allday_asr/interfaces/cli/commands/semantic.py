from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from allday_asr.application.semantic.overview import overview as semantic_overview
from allday_asr.application.semantic.pipeline import (
    ReplaySemanticProvider,
    SemanticSettings as SemanticV2E02Settings,
    export_manual_bundle as export_manual_semantic_bundle,
    run as run_semantic_v2e02,
)
from allday_asr.interfaces.cli.targets import resolve_v2_target
from allday_asr.paths import DEFAULT_DB_PATH
from allday_asr.storage.database import Database

app = typer.Typer(
    help="V2-E 本地语义证据包与云端接口边界。",
    no_args_is_help=True,
)
console = Console()

@app.command(name="build")
def semantic_v2_build(
    recording_id: Optional[int] = typer.Argument(
        None, min=1, help="旧单文件 recording_id；原生分片会话改用 --session。"
    ),
    session_id: Optional[int] = typer.Option(
        None, "--session", min=1, help="原生 V2 录音会话 ID。"
    ),
    asr_run_id: int | None = typer.Option(
        None,
        "--asr-run",
        min=1,
        help="V2-C run；默认使用最新 V2-D 对应的 V2-C run。",
    ),
    diarization_run_id: int | None = typer.Option(
        None,
        "--diarization-run",
        min=1,
        help="V2-D run；默认使用最新完成的一次。",
    ),
    episode_gap_seconds: float = typer.Option(
        180.0,
        "--episode-gap-seconds",
        min=30.0,
        max=900.0,
        help="超过该无文字间隔时开始新 episode；不限制 episode 总时长。",
    ),
    min_informative_chars: int = typer.Option(
        4,
        "--min-informative-chars",
        min=1,
        max=100,
        help="进入 LLM 请求所需的最少信息字符；纯语气词仍留在本地底账。",
    ),
    max_llm_request_chars: int = typer.Option(
        200_000,
        "--max-llm-request-chars",
        min=4_000,
        help="仅用于传输规划；超限才按说话轮次重叠分块，不切断 episode。",
    ),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """生成四轨证据分离、不联网的 V2-E.0.2 episode 传输计划。"""
    database = Database.open(db)
    recording_id, session_id = resolve_v2_target(
        database, recording_id, session_id
    )
    summary = run_semantic_v2e02(
        database,
        recording_id,
        session_id=session_id,
        asr_run_id=asr_run_id,
        diarization_run_id=diarization_run_id,
        settings=SemanticV2E02Settings(
            episode_gap_ms=round(episode_gap_seconds * 1_000),
            min_informative_chars=min_informative_chars,
            max_llm_request_chars=max_llm_request_chars,
        ),
    )
    console.print(
        f"[green]V2-E.0.2 Episode 证据计划完成[/green] run={summary.run_id} | "
        f"ASR={summary.asr_run_id} | D={summary.diarization_run_id or 'none'} | "
        f"episodes={summary.episode_count} | "
        f"excluded={summary.excluded_block_count} | jobs={summary.llm_job_count} | "
        f"tokens={summary.token_count}"
    )
    console.print(
        "[yellow]本轮没有网络请求，没有上传文字或音频；"
        "episode 不是场景，匿名声纹也不是身份，mock 没有做语义推理。[/yellow]"
    )
    console.print(f"运行清单：{summary.manifest_path.resolve()}")


@app.command(name="export")
def semantic_v2_export(
    recording_id: int = typer.Argument(..., min=1),
    output: Path = typer.Argument(..., help="写入脱敏后的 provider 请求 JSON。"),
    asr_run_id: int | None = typer.Option(None, "--asr-run", min=1),
    diarization_run_id: int | None = typer.Option(
        None, "--diarization-run", min=1
    ),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """导出给当前 Codex 会话手工评估的脱敏 episode 请求。"""
    payload = export_manual_semantic_bundle(
        Database.open(db),
        recording_id,
        output,
        asr_run_id=asr_run_id,
        diarization_run_id=diarization_run_id,
    )
    console.print(
        "[green]V2-E.0.2 手工评估包已导出[/green] | "
        f"sha256={payload['provider_request_sha256']}"
    )
    console.print(
        "[yellow]文件只含脱敏文字证据；项目没有调用 API，也没有发送音频。[/yellow]"
    )
    console.print(f"评估包：{output.resolve()}")


@app.command(name="replay")
def semantic_v2_replay(
    recording_id: int = typer.Argument(..., min=1),
    response: Path = typer.Argument(
        ...,
        exists=True,
        dir_okay=False,
        readable=True,
        help="当前 Codex 会话按导出包生成的语义响应 JSON。",
    ),
    asr_run_id: int | None = typer.Option(None, "--asr-run", min=1),
    diarization_run_id: int | None = typer.Option(
        None, "--diarization-run", min=1
    ),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """校验并回放一次离线 Codex 手工语义评估。"""
    parsed = json.loads(response.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise typer.BadParameter("语义响应 JSON 顶层必须是对象")
    summary = run_semantic_v2e02(
        Database.open(db),
        recording_id,
        asr_run_id=asr_run_id,
        diarization_run_id=diarization_run_id,
        provider=ReplaySemanticProvider(parsed),
    )
    console.print(
        f"[green]Codex 手工语义响应已校验并入库[/green] run={summary.run_id} | "
        f"episodes={summary.episode_count} | scenes={summary.scene_count} | "
        f"claims={summary.claim_count} | actions={summary.action_count} | "
        f"unresolved={summary.unresolved_count}"
    )
    console.print(
        "[yellow]这是当前 Codex 会话的记录/回放结果，不代表已接入可复现的运行时 API。[/yellow]"
    )
    console.print(f"运行清单：{summary.manifest_path.resolve()}")


@app.command(name="status")
def semantic_v2_status(
    recording_id: int = typer.Argument(..., min=1),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite 数据库路径。"),
) -> None:
    """查看最新 V2-E 证据包、传输计划和人工审核进度。"""
    payload = semantic_overview(Database.open(db), recording_id)
    if not payload["available"]:
        console.print(f"[yellow]{payload['reason']}[/yellow]")
        return
    summary = payload["summary"]
    if payload.get("version") == "v2-e.0.2":
        console.print(
            f"run={payload['run']['id']} | provider={payload['run']['provider']} | "
            f"episodes={summary['episode_count']} | scenes={summary['scene_count']} | "
            f"claims={summary['claim_count']} | actions={summary['action_count']} | "
            f"unresolved={summary['unresolved_count']} | "
            f"reviewed={payload['reviewed_candidates']}/{len(payload['candidates'])}"
        )
    elif payload.get("version") == "v2-e.0.1":
        console.print(
            f"run={payload['run']['id']} | provider={payload['run']['provider']} | "
            f"conversations={summary['conversation_count']} | "
            f"excluded={summary['excluded_block_count']} | "
            f"jobs={summary['llm_job_count']} | tokens={summary['token_count']} | "
            f"reviewed={payload['reviewed_candidates']}/{len(payload['candidates'])}"
        )
    else:
        console.print(
            f"run={payload['run']['id']} | provider={payload['run']['provider']} | "
            f"events={summary['event_count']} | tokens={summary['token_count']} | "
            f"reviewed={payload['reviewed_candidates']}/{len(payload['candidates'])}"
        )
