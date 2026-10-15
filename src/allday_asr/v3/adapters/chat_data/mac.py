from __future__ import annotations

import json
import os
import socket
from pathlib import Path

from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from allday_asr.v3.ports.chat_data import ChatDataError

ROUTES = {
    "/v1/" + name
    for name in (
        "status",
        "coverage",
        "contract",
        "accounts",
        "conversations",
        "messages",
        "changes",
        "snapshots",
        "records",
        "context",
    )
}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward a bearer to a redirect target.


class MacChatData:
    def __init__(
        self, base_url=None, token=None, timeout=15, config_path: Path | None = None
    ):
        self.base_url = (base_url or os.getenv("CHAT_DATA_BASE_URL", "")).rstrip("/")
        self._token = token or os.getenv("CHAT_DATA_TOKEN", "")
        self.config_path = config_path
        if config_path and config_path.exists():
            try:
                if config_path.stat().st_size > 16384:
                    raise ValueError()
                saved = json.loads(config_path.read_text(encoding="utf-8"))
                if (
                    not isinstance(saved, dict)
                    or not isinstance(saved.get("base_url", ""), str)
                    or not isinstance(saved.get("token", ""), str)
                ):
                    raise ValueError()
                self.base_url = self.base_url or saved.get("base_url", "")
                self._token = self._token or saved.get("token", "")
            except (ValueError, OSError):
                self.base_url, self._token = "", ""
        self.timeout = timeout
        self._opener = build_opener(NoRedirect())

    @property
    def configured(self):
        return bool(self.base_url and self._token)

    @staticmethod
    def validate_url(base_url):
        if not isinstance(base_url, str) or len(base_url) > 500:
            raise ChatDataError("INVALID_CHAT_BASE_URL", 400)
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ChatDataError("INVALID_CHAT_BASE_URL", 400)

    def configure(self, base_url, token):
        self.validate_url(base_url)
        if (
            not isinstance(token, str)
            or len(token) < 32
            or len(token) > 1000
            or not token.isascii()
            or any(c.isspace() for c in token)
        ):
            raise ChatDataError("INVALID_CHAT_TOKEN", 400)
        if self.config_path is None:
            raise ChatDataError("CHAT_CONFIG_STORAGE_UNAVAILABLE")
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.config_path.with_suffix(".json.part")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump({"base_url": base_url.rstrip("/"), "token": token}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.config_path)
        self.base_url, self._token = base_url.rstrip("/"), token
        return {"configured": True, "base_url": self.base_url}

    def request(self, route, body=None, **query):
        if not self.configured:
            raise ChatDataError("CHAT_NOT_CONFIGURED")
        self.validate_url(self.base_url)
        if route not in ROUTES:
            raise ChatDataError("UNSUPPORTED_CHAT_ROUTE", 400)
        suffix = urlencode(
            {
                k: json.dumps(v, ensure_ascii=False, separators=(",", ":"))
                if isinstance(v, dict)
                else v
                for k, v in query.items()
                if v is not None
            }
        )
        data = (
            json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        )
        if data and len(data) > 65536:
            raise ChatDataError("REQUEST_TOO_LARGE", 413)
        request = Request(
            self.base_url + route + ("?" + suffix if suffix else ""),
            data=data,
            headers={
                "Authorization": "Bearer " + self._token,
                "Content-Type": "application/json",
            },
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(8388609)
            if len(raw) > 8388608:
                raise ChatDataError("RESPONSE_TOO_LARGE", 413)
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ChatDataError("INVALID_CHAT_RESPONSE", 502)
            return value
        except HTTPError as exc:
            try:
                code = (
                    json.loads(exc.read(65536))
                    .get("error", {})
                    .get("code", "MAC_HTTP_ERROR")
                )
            except (ValueError, AttributeError):
                code = "MAC_HTTP_ERROR"
            raise ChatDataError(str(code), exc.code) from None
        except (URLError, TimeoutError, socket.timeout, OSError):
            raise ChatDataError("MAC_OFFLINE") from None
        except (ValueError, UnicodeError):
            raise ChatDataError("INVALID_CHAT_RESPONSE", 502) from None
