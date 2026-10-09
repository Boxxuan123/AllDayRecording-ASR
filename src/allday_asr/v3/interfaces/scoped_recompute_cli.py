"""Explicit local regeneration; this command is never called by upgrade/startup."""
import json
from pathlib import Path
import typer

from allday_asr.v3.application.scoped_recompute import ScopedRecomputePass
from allday_asr.v3.bootstrap import V3CorePaths, compose_v3_core

app = typer.Typer(help="有界局部重算：最多8个明确目标、2次尝试；不重传或转写音频。")


@app.command("run")
def run(
    execution_id: str = typer.Option(..., "--execution-id"),
    stage: str = typer.Option(..., "--stage"),
    target: list[str] = typer.Option(..., "--target"),
    timeout_seconds: int = typer.Option(180, "--timeout-seconds"),
    state_dir: Path = typer.Option(..., "--state-dir"),
    timezone_name: str = typer.Option("Asia/Singapore", "--timezone"),
):
    core = compose_v3_core(V3CorePaths.from_state_dir(state_dir))
    core.initialize()
    try:
        job = ScopedRecomputePass(core, core.paths.state_dir / "model-execution-receipts",
            execution_id=execution_id, stage=stage, targets=target,
            timezone_name=timezone_name, timeout_seconds=timeout_seconds)
        result = job.run()
        typer.echo(json.dumps(result, ensure_ascii=False, default=str))
        if result["status"] != "succeeded":
            raise typer.Exit(1)
    finally:
        core.close()


@app.command("cancel")
def cancel(
    execution_id: str = typer.Option(..., "--execution-id"),
    state_dir: Path = typer.Option(..., "--state-dir"),
):
    import re
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", execution_id):
        raise typer.BadParameter("invalid execution_id")
    path = state_dir / "model-execution-receipts" / f"scope-{execution_id}.pass.cancel"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("cancelled", encoding="utf-8")
    typer.echo("cancel requested")


@app.command("status")
def status(
    execution_id: str = typer.Option(..., "--execution-id"),
    state_dir: Path = typer.Option(..., "--state-dir"),
):
    import re
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", execution_id):
        raise typer.BadParameter("invalid execution_id")
    path = state_dir / "model-execution-receipts" / f"scope-{execution_id}.pass.json"
    typer.echo(path.read_text("utf-8"))


def register(parent):
    parent.add_typer(app, name="recompute")
