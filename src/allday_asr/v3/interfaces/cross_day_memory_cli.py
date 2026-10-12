"""Finite operator entry; no scheduler and no provider invocation."""

import json
from pathlib import Path
import typer
from allday_asr.v3.adapters.sqlite import V3Database, SqliteUnitOfWork
from allday_asr.v3.adapters.sqlite.cross_day_memory_sources import CrossDayMemorySources
from allday_asr.v3.application.cross_day_memory import CrossDayMemoryService
from allday_asr.v3.bootstrap.core import V3CorePaths


def register(app):
    @app.command("memory-inventory")
    def inventory(first: str, last: str, state_dir: Path):
        paths = V3CorePaths.from_state_dir(state_dir)
        db = V3Database(paths.database_path)
        sources = CrossDayMemorySources(
            db, lambda: SqliteUnitOfWork(db), state_dir / "chat/cache.sqlite3"
        )
        typer.echo(
            json.dumps(
                sources.inventory(first, last, "Asia/Singapore"),
                ensure_ascii=False,
                indent=2,
            )
        )

    @app.command("memory-update")
    def update(first: str, last: str, state_dir: Path, contacts_json: str):
        paths = V3CorePaths.from_state_dir(state_dir)
        db = V3Database.open(paths.database_path)

        def factory():
            return SqliteUnitOfWork(db)

        sources = CrossDayMemorySources(db, factory, state_dir / "chat/cache.sqlite3")
        typer.echo(
            json.dumps(
                CrossDayMemoryService(sources, factory).update(
                    first, last, json.loads(contacts_json)
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
