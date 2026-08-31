from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from allday_asr.v3.application import LegacyImportCommand
from allday_asr.v3.bootstrap import V3CorePaths, compose_v3_core, start_empty_runtime
from allday_asr.v3.config import V3ConfigurationError, V3Settings


app = typer.Typer(
    help="V3 parallel skeleton; disabled unless ALLDAY_V3_ENABLED is set.",
    no_args_is_help=True,
)


@app.command(name="status")
def status_command() -> None:
    settings = _settings()
    typer.echo(
        json.dumps(
            {
                "enabled": settings.enabled,
                "deployment_mode": settings.deployment_mode.value,
                "state": "disabled" if not settings.enabled else "startable",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


@app.command(name="start")
def start_command() -> None:
    settings = _settings()
    try:
        runtime = start_empty_runtime(settings)
    except V3ConfigurationError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(json.dumps(runtime.as_dict(), ensure_ascii=False, sort_keys=True))


@app.command(name="migrate")
def migrate_command(
    state_dir: Annotated[
        Path | None,
        typer.Option(
            "--state-dir",
            help="V3-owned state directory; never the V2 state directory.",
        ),
    ] = None,
) -> None:
    _enabled_settings()
    paths = (
        V3CorePaths.from_state_dir(state_dir)
        if state_dir is not None
        else V3CorePaths.from_environment()
    )
    core = compose_v3_core(paths)
    version = core.initialize()
    typer.echo(
        json.dumps(
            {
                "database": str(paths.database_path),
                "schema_version": version,
                "state": "ready",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


@app.command(name="legacy-import")
def legacy_import_command(
    source_database: Annotated[
        Path, typer.Argument(exists=True, dir_okay=False)
    ],
    state_dir: Annotated[
        Path | None,
        typer.Option(
            "--state-dir",
            help="V3-owned state directory; never the V2 state directory.",
        ),
    ] = None,
    source_namespace: Annotated[
        str | None, typer.Option("--source-namespace")
    ] = None,
) -> None:
    _enabled_settings()
    paths = (
        V3CorePaths.from_state_dir(state_dir)
        if state_dir is not None
        else V3CorePaths.from_environment()
    )
    core = compose_v3_core(paths)
    core.initialize()
    result = core.import_legacy_v2.execute(
        LegacyImportCommand(
            source_database=source_database,
            source_namespace=source_namespace,
        )
    )
    typer.echo(json.dumps(result.as_dict(), ensure_ascii=False, sort_keys=True))


def _settings() -> V3Settings:
    try:
        return V3Settings.from_environment()
    except V3ConfigurationError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _enabled_settings() -> V3Settings:
    settings = _settings()
    if not settings.enabled:
        raise typer.BadParameter(
            "V3 is disabled; set ALLDAY_V3_ENABLED=1 before modifying V3 state"
        )
    return settings


__all__ = ["app"]
