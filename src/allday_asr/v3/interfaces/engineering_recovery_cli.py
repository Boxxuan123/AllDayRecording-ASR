"""Explicit bounded inspection/repair; no automatic historical or remote work."""
import json
from pathlib import Path
import typer
from allday_asr.v3.adapters.transfer.automation_state import AutomaticWorkflowStateStore
from allday_asr.v3.bootstrap import compose_v3_core, V3CorePaths
from allday_asr.v3.config import CodexReminderSettings

app = typer.Typer(help='工程补修：只读差异与显式本地恢复。')


def echo(value):
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2, default=str))


@app.command('retry-report')
def retry_report(state_dir: Path = typer.Option(...), session_id: str = typer.Option(...)):
    echo(AutomaticWorkflowStateStore(state_dir / 'automation').retry_request_report(session_id))


@app.command('retry-coalesce')
def retry_coalesce(state_dir: Path = typer.Option(...), session_id: str = typer.Option(...),
                   legacy_request_id: str = typer.Option(...), manual_request_id: str = typer.Option(...),
                   actor: str = typer.Option(...)):
    echo(AutomaticWorkflowStateStore(state_dir / 'automation').coalesce_legacy_duplicate(
        session_id, legacy_request_id, manual_request_id, actor))


def local_core(state_dir):
    core = compose_v3_core(V3CorePaths.from_state_dir(state_dir), codex_settings=CodexReminderSettings(enabled=False))
    core.initialize()
    return core


@app.command('daily-sync-report')
def daily_report(state_dir: Path = typer.Option(...), first_date: str = typer.Option(...), last_date: str = typer.Option(...)):
    core = local_core(state_dir)
    try:
        echo(core.insights.daily_sync_differences(first_date, last_date))
    finally:
        core.close()


@app.command('daily-sync-repair')
def daily_repair(state_dir: Path = typer.Option(...), first_date: str = typer.Option(...), last_date: str = typer.Option(...), actor: str = typer.Option(...)):
    core = local_core(state_dir)
    try:
        echo(core.insights.repair_daily_sync(first_date, last_date, actor=actor))
    finally:
        core.close()


def register(parent):
    parent.add_typer(app, name='engineering-recovery')
