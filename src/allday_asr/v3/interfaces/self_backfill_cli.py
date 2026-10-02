"""Persist a private dry-run plan, then apply exactly that frozen cohort."""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated

import typer

from allday_asr.v3.application.historical_self_backfill import HistoricalSelfBackfillService, metrics
from allday_asr.v3.bootstrap import V3CorePaths, compose_v3_core
from allday_asr.v3.adapters.files import ContentAddressedStore
from allday_asr.v3.adapters.models.funasr import FunASRBackend
from allday_asr.v3.adapters.speaker_embeddings import FunASRSpeakerEmbeddingProvider


def register(app):
    @app.command('self-backfill')
    def command(
        plan: Annotated[Path, typer.Option('--plan', help='Private JSON plan outside runtime state.')],
        days: Annotated[int | None, typer.Option('--days', min=1)] = None,
        limit: Annotated[int, typer.Option('--limit', min=1, max=100000)] = 100,
        session_id: Annotated[str | None, typer.Option('--session-id')] = None,
        since: Annotated[str | None, typer.Option('--since')] = None,
        until: Annotated[str | None, typer.Option('--until')] = None,
        apply: Annotated[bool, typer.Option('--apply/--dry-run')] = False,
        state_dir: Annotated[Path | None, typer.Option('--state-dir')] = None,
    ):
        paths = V3CorePaths.from_state_dir(state_dir) if state_dir else V3CorePaths.from_environment()
        plan = plan.resolve()
        if plan.suffix != '.json' or plan.is_relative_to(paths.state_dir):
            raise typer.BadParameter('plan must be a private JSON file outside runtime state')
        if days and since:
            raise typer.BadParameter('choose --days or --since')
        backend = FunASRBackend(device=os.environ.get('ALLDAY_V3_SPEAKER_DEVICE', 'auto'))
        provider = FunASRSpeakerEmbeddingProvider(ContentAddressedStore(paths.audio_store_path),
            backend_factory=lambda: backend, temp_root=plan.parent/'clips')
        core = compose_v3_core(paths, speaker_embedding_provider=provider)
        service = HistoricalSelfBackfillService(core.people, core.audio_store)
        try:
            if apply:
                # Apply never expands/reselects the saved cohort or reruns embeddings.
                data = json.loads(plan.read_text(encoding='utf-8'))
                core.initialize()
                result = service.apply(data)
            else:
                if plan.exists():
                    raise typer.BadParameter('plan already exists; use a new path or --apply')
                start = since or ((datetime.now(timezone.utc)-timedelta(days=days)).isoformat() if days else None)
                data = service.dry_run(since=start, until=until, limit=limit, session_id=session_id)
                plan.parent.mkdir(parents=True, exist_ok=True)
                plan.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
                result = {'run_id': data['run_id'], **metrics(data)}
            typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            core.close()
