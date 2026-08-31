"""V3 interface adapters."""
from allday_asr.v3.interfaces.desktop_server import (
    create_v3_desktop_server,
    serve_v3_desktop,
)

__all__ = ["create_v3_desktop_server", "serve_v3_desktop"]
