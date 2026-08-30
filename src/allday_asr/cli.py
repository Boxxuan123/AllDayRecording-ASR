"""Compatibility entrypoint for the modular Typer application."""

from allday_asr.interfaces.cli.app import app

__all__ = ["app"]


if __name__ == "__main__":
    app()
