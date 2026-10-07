"""Explicit inspection/retry/backfill commands; normal services start catch-up."""

import json
from pathlib import Path

import typer
from allday_asr.v3.bootstrap import compose_v3_core
from allday_asr.v3.application.daily_backfill import DailyBackfillPass

app = typer.Typer(help="持久每日生成队列与一次性历史回填。")


def coordinator():
    core = compose_v3_core()
    core.initialize()
    if core.daily_automation is None:
        raise typer.BadParameter("Daily Codex generation disabled")
    return core, core.daily_automation


@app.command("inventory")
def inventory():
    core, worker = coordinator()
    try:
        typer.echo(json.dumps(worker.inventory.scan(), ensure_ascii=False, indent=2))
    finally:
        core.close()


@app.command("status")
def status():
    core, worker = coordinator()
    try:
        typer.echo(json.dumps(worker.queue.states(), ensure_ascii=False, indent=2))
    finally:
        core.close()


@app.command("catch-up")
def catch_up():
    core, worker = coordinator()
    try:
        worker.catch_up("explicit_catch_up")
        while (result := worker.run_once()) is not None:
            typer.echo(
                json.dumps(
                    {k: v for k, v in result.items() if k != "result"},
                    ensure_ascii=False,
                )
            )
    finally:
        core.close()


@app.command("retry")
def retry(date: str = typer.Option(..., "--date")):
    core, worker = coordinator()
    try:
        worker.queue.retry(date, worker.version)
        result = worker.run_once(allowed_dates={date})
        typer.echo(json.dumps(result, ensure_ascii=False, default=str))
    finally:
        core.close()


@app.command("backfill")
def backfill(
    manifest: Path = typer.Option(..., "--manifest"),
    results: Path = typer.Option(..., "--results"),
):
    core, worker = coordinator()
    try:
        sealed = json.loads(manifest.read_text(encoding="utf-8"))
        worker.catch_up("frozen_backfill", origin="HISTORICAL")
        outcome = DailyBackfillPass(worker, sealed, results).run()
        typer.echo(
            json.dumps(
                {"BACKFILL_PASS_COMPLETE": outcome["BACKFILL_PASS_COMPLETE"]},
                ensure_ascii=False,
            )
        )
    finally:
        core.close()


def register(parent):
    parent.add_typer(app, name="daily")
