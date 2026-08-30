from __future__ import annotations

import mimetypes
from urllib.parse import unquote


def get_asset(handler, parsed) -> bool:
    if parsed.path == "/":
        handler._send_asset("index.html", "text/html; charset=utf-8")
        return True
    if parsed.path.startswith("/assets/") or parsed.path in {
        "/favicon.svg",
        "/icons.svg",
    }:
        filename = unquote(parsed.path.lstrip("/"))
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        handler._send_asset(filename, content_type)
        return True
    return False
