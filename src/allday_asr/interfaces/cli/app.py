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
from allday_asr.interfaces.cli.commands.transfer import app as transfer_app
from allday_asr.interfaces.cli.commands.utility import app as utility_app
from allday_asr.interfaces.cli.commands.workflow import app as workflow_app
from allday_asr.v3.interfaces.cli import app as v3_app

app = typer.Typer(
    name="allday-asr",
    help="AllDayRecording V3 本地工作台、设备同步与处理工具。",
    no_args_is_help=True,
)

legacy_root = typer.Typer(
    help="V2 只读页面、维护命令与可执行回退入口。",
    no_args_is_help=True,
)
legacy_root.add_typer(library_app, name="voice-library")
legacy_root.add_typer(evaluation_app, name="evaluation")
legacy_root.add_typer(benchmark_app, name="benchmark")
legacy_root.add_typer(asr_app, name="asr")
legacy_root.add_typer(diarization_app, name="diarization")
legacy_root.add_typer(semantic_app, name="semantic")
legacy_root.add_typer(session_app, name="session")
legacy_root.add_typer(workflow_app, name="workflow")
legacy_root.add_typer(utility_app)
legacy_root.add_typer(legacy_app)

app.add_typer(v3_app)
app.add_typer(transfer_app, name="device")
app.add_typer(legacy_root, name="legacy")

__all__ = ["app"]
