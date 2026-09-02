"""AllDayRecording V3 command-line entrypoint."""

import typer

from allday_asr.v3.interfaces.cli import app as core_app
from allday_asr.v3.interfaces.transfer.cli import app as device_app


app = typer.Typer(
    name="allday-asr",
    help=(
        "AllDayRecording V3 本地工作台、设备同步与处理工具。"
        "当前版本不提供 Legacy V2 可执行入口。"
    ),
    no_args_is_help=True,
)
app.add_typer(core_app)
app.add_typer(device_app, name="device")

__all__ = ["app"]


if __name__ == "__main__":
    app()
