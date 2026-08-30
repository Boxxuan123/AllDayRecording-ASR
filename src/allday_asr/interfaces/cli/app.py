from __future__ import annotations

import typer

from allday_asr.interfaces.cli.commands.asr import app as asr_app
from allday_asr.interfaces.cli.commands.benchmark import app as benchmark_app
from allday_asr.interfaces.cli.commands.diarization import app as diarization_app
from allday_asr.interfaces.cli.commands.evaluation import app as evaluation_app
from allday_asr.interfaces.cli.commands.legacy import app as legacy_app
from allday_asr.interfaces.cli.commands.library import app as library_app
from allday_asr.interfaces.cli.commands.semantic import app as semantic_app
from allday_asr.interfaces.cli.commands.sessions import app as session_app
from allday_asr.interfaces.cli.commands.utility import app as utility_app
from allday_asr.interfaces.cli.commands.workflow import app as workflow_app

app = typer.Typer(
    name="allday-asr",
    help="本地全天录音转写与时间线工具。",
    no_args_is_help=True,
)

app.add_typer(library_app, name="voice-library")
app.add_typer(evaluation_app, name="evaluation")
app.add_typer(benchmark_app, name="benchmark")
app.add_typer(asr_app, name="asr-v2")
app.add_typer(diarization_app, name="diarization-v2")
app.add_typer(semantic_app, name="semantic-v2")
app.add_typer(session_app, name="session")
app.add_typer(workflow_app, name="workflow-v2")
app.add_typer(utility_app)
app.add_typer(legacy_app)

__all__ = ["app"]
