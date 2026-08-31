from __future__ import annotations

import json
import mimetypes
import re
import secrets
import time
import webbrowser
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from allday_asr.v3.application import (
    CorrectUtteranceCommand,
    processing_snapshot_dict,
)
from allday_asr.v3.bootstrap import V3Core, V3CorePaths, compose_v3_core
from allday_asr.v3.domain import new_ulid


ASSET_ROOT = Path(__file__).parents[1] / "web_assets"
_JOB_ROUTE = re.compile(r"^/api/v3/processing-jobs/([^/]+)$")
_RUN_ROUTE = re.compile(r"^/api/v3/processing-runs/([^/]+)$")
_SESSION_ROUTE = re.compile(r"^/api/v3/recording-sessions/([^/]+)$")
_MEDIA_ROUTE = re.compile(r"^/api/v3/media/([^/]+)$")
_RETRY_ROUTE = re.compile(r"^/api/v3/processing-jobs/([^/]+)/retry$")
_CANCEL_ROUTE = re.compile(r"^/api/v3/processing-jobs/([^/]+)/cancel$")
_CORRECTION_ROUTE = re.compile(r"^/api/v3/utterances/([^/]+)/corrections$")
_FRONTEND_ROUTES = {
    "/",
    "/recordings",
    "/reviews",
    "/processing",
    "/devices",
    "/data",
    "/settings",
    "/lab",
}


class V3DesktopApplication:
    def __init__(self, core: V3Core, *, token: str | None = None) -> None:
        self.core = core
        self.token = token or secrets.token_urlsafe(32)
        self.host = "127.0.0.1"
        self.port = 0

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


class V3DesktopRequestHandler(BaseHTTPRequestHandler):
    asset_root = ASSET_ROOT
    server: "V3DesktopHTTPServer"
    max_json_body = 1024 * 1024
    session_cookie_name = "allday_v3_session"

    @property
    def application(self) -> V3DesktopApplication:
        return self.server.application

    def do_GET(self) -> None:
        self._handle(self._dispatch_get)

    def do_POST(self) -> None:
        self._handle(self._dispatch_post)

    def _handle(self, callback) -> None:
        request_id = new_ulid()
        try:
            callback()
        except KeyError as exc:
            self._send_error(
                HTTPStatus.NOT_FOUND, "not_found", str(exc), request_id=request_id
            )
        except ValueError as exc:
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                str(exc),
                request_id=request_id,
            )
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            self._send_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "internal_error",
                "V3 Core 无法完成请求。",
                request_id=request_id,
            )

    def _dispatch_get(self) -> None:
        parsed = urlparse(self.path)
        if self._consume_token(parsed):
            return
        if not self._authenticated():
            self._send_error(
                HTTPStatus.FORBIDDEN,
                "desktop_session_required",
                "请从启动命令提供的安全链接打开 V3 工作台。",
            )
            return
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        if path == "/api/v3/status":
            self._send_json(HTTPStatus.OK, self.application.core.desktop.status())
            return
        if path == "/api/v3/overview":
            self._send_json(HTTPStatus.OK, self.application.core.desktop.overview())
            return
        if path == "/api/v3/recording-sessions":
            cursor = query.get("cursor", [None])[0]
            limit = _integer(query.get("limit", ["50"])[0], "limit")
            page = self.application.core.desktop.list_sessions(cursor, limit)
            self._send_json(HTTPStatus.OK, page.as_dict())
            return
        if match := _SESSION_ROUTE.fullmatch(path):
            self._send_json(
                HTTPStatus.OK,
                self.application.core.desktop.session_detail(unquote(match.group(1))),
            )
            return
        if path == "/api/v3/processing-jobs":
            status = query.get("status", [None])[0]
            limit = _integer(query.get("limit", ["100"])[0], "limit")
            self._send_json(
                HTTPStatus.OK,
                {
                    "items": self.application.core.desktop.list_processing_jobs(
                        status, limit
                    )
                },
            )
            return
        if match := _JOB_ROUTE.fullmatch(path):
            snapshot = self.application.core.processing.get(unquote(match.group(1)))
            self._send_json(HTTPStatus.OK, processing_snapshot_dict(snapshot))
            return
        if match := _RUN_ROUTE.fullmatch(path):
            snapshot = self.application.core.processing.get_for_run(
                unquote(match.group(1))
            )
            self._send_json(HTTPStatus.OK, processing_snapshot_dict(snapshot)["run"])
            return
        if path == "/api/v3/reviews":
            limit = _integer(query.get("limit", ["100"])[0], "limit")
            self._send_json(
                HTTPStatus.OK,
                {"items": self.application.core.desktop.list_reviews(limit)},
            )
            return
        if path == "/api/v3/devices":
            self._send_json(
                HTTPStatus.OK,
                {"items": self.application.core.desktop.list_devices()},
            )
            return
        if path == "/api/v3/data-health":
            self._send_json(HTTPStatus.OK, self.application.core.desktop.data_health())
            return
        if path == "/api/v3/settings":
            self._send_json(HTTPStatus.OK, self.application.core.desktop.settings())
            return
        if path == "/api/v3/lab":
            self._send_json(HTTPStatus.OK, self.application.core.desktop.lab())
            return
        if match := _MEDIA_ROUTE.fullmatch(path):
            self._send_media(unquote(match.group(1)))
            return
        if path == "/api/v3/events/processing":
            self._send_processing_events(query)
            return
        self._send_frontend(path)

    def _dispatch_post(self) -> None:
        if not self._authorized_mutation():
            return
        path = urlparse(self.path).path.rstrip("/")
        body = self._read_json()
        if match := _RETRY_ROUTE.fullmatch(path):
            if body:
                raise ValueError("retry request body must be empty")
            snapshot = self.application.core.processing.retry(unquote(match.group(1)))
            self._send_json(HTTPStatus.ACCEPTED, processing_snapshot_dict(snapshot))
            return
        if match := _CANCEL_ROUTE.fullmatch(path):
            reason = body.get("reason")
            if set(body) != {"reason"} or not isinstance(reason, str) or not reason:
                raise ValueError("cancel request requires a reason")
            snapshot = self.application.core.processing.cancel(
                unquote(match.group(1)), reason
            )
            self._send_json(HTTPStatus.ACCEPTED, processing_snapshot_dict(snapshot))
            return
        if match := _CORRECTION_ROUTE.fullmatch(path):
            required = {"expected_revision", "text"}
            if set(body) != required:
                raise ValueError("utterance correction fields are invalid")
            revision = body["expected_revision"]
            text = body["text"]
            if (
                isinstance(revision, bool)
                or not isinstance(revision, int)
                or revision < 1
                or not isinstance(text, str)
            ):
                raise ValueError("utterance correction values are invalid")
            value = self.application.core.corrections.correct_utterance(
                CorrectUtteranceCommand(
                    utterance_id=unquote(match.group(1)),
                    expected_revision=revision,
                    text=text,
                    actor="desktop-user",
                )
            )
            self._send_json(HTTPStatus.OK, value)
            return
        self._send_error(HTTPStatus.NOT_FOUND, "not_found", "V3 命令不存在。")

    def _consume_token(self, parsed) -> bool:
        token = parse_qs(parsed.query).get("token", [None])[0]
        if token is None:
            return False
        if not secrets.compare_digest(token, self.application.token):
            self._send_error(
                HTTPStatus.FORBIDDEN, "invalid_desktop_link", "安全链接已失效。"
            )
            return True
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", parsed.path or "/")
        self.send_header(
            "Set-Cookie",
            f"{self.session_cookie_name}={self.application.token}; "
            "Path=/; HttpOnly; SameSite=Strict",
        )
        self._security_headers()
        self.end_headers()
        return True

    def _authenticated(self) -> bool:
        header = self.headers.get("X-AllDay-Desktop-Session")
        if header and secrets.compare_digest(header, self.application.token):
            return True
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        session = cookie.get(self.session_cookie_name)
        return bool(
            session
            and secrets.compare_digest(session.value, self.application.token)
        )

    def _authorized_mutation(self) -> bool:
        if not self._authenticated():
            self._send_error(
                HTTPStatus.FORBIDDEN,
                "desktop_session_required",
                "V3 Desktop Session 无效。",
            )
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {
            self.application.base_url,
            f"http://localhost:{self.application.port}",
        }:
            self._send_error(
                HTTPStatus.FORBIDDEN, "cross_origin_denied", "拒绝跨站写请求。"
            )
            return False
        return True

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Content-Length is invalid") from exc
        if length < 0 or length > self.max_json_body:
            raise ValueError("request body size is invalid")
        if length == 0:
            return {}
        try:
            value = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise ValueError("request body is not valid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("request body must be a JSON object")
        return value

    def _send_frontend(self, path: str) -> None:
        if path in _FRONTEND_ROUTES or path.startswith("/recordings/"):
            self._send_asset("index.html", "text/html; charset=utf-8")
            return
        if path.startswith("/assets/") or path in {"/favicon.svg", "/icons.svg"}:
            filename = unquote(path.lstrip("/"))
            content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            self._send_asset(filename, content_type)
            return
        self._send_error(HTTPStatus.NOT_FOUND, "not_found", "页面不存在。")

    def _send_asset(self, filename: str, content_type: str) -> None:
        root = self.asset_root.resolve()
        path = (root / filename).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise KeyError(f"frontend asset does not exist: {filename}")
        self._send_bytes(HTTPStatus.OK, path.read_bytes(), content_type)

    def _send_media(self, media_id: str) -> None:
        descriptor = self.application.core.desktop.media(media_id)
        size = int(descriptor["size_bytes"])
        start, end = _parse_range(self.headers.get("Range"), size)
        with self.application.core.audio_store.open(str(descriptor["storage_key"])) as source:
            source.seek(start)
            payload = source.read(end - start + 1)
        status = HTTPStatus.PARTIAL_CONTENT if start != 0 or end != size - 1 else HTTPStatus.OK
        content_type = {
            "wav": "audio/wav",
            "flac": "audio/flac",
            "m4a": "audio/mp4",
        }.get(str(descriptor["format"]), "application/octet-stream")
        headers = {"Accept-Ranges": "bytes"}
        if status is HTTPStatus.PARTIAL_CONTENT:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        self._send_bytes(status, payload, content_type, headers=headers)

    def _send_processing_events(self, query: dict[str, list[str]]) -> None:
        after = self.headers.get("Last-Event-ID") or query.get("after", ["0"])[0]
        sequence = _integer(after, "event sequence")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self._security_headers()
        self.end_headers()
        self.wfile.write(b"retry: 1500\n\nevent: ready\ndata: {}\n\n")
        self.wfile.flush()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            events = self.application.core.desktop.processing_events(sequence)
            if events:
                for event in events:
                    sequence = int(event["sequence"])
                    payload = json.dumps(
                        event["payload"], ensure_ascii=False, separators=(",", ":")
                    )
                    data = (
                        f"id: {sequence}\nevent: processing\ndata: {payload}\n\n"
                    ).encode("utf-8")
                    self.wfile.write(data)
                self.wfile.flush()
            else:
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
            time.sleep(0.5)

    def _send_json(self, status: HTTPStatus, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send_bytes(status, data, "application/json; charset=utf-8")

    def _send_error(
        self,
        status: HTTPStatus,
        code: str,
        message: str,
        *,
        request_id: str | None = None,
    ) -> None:
        self._send_json(
            status,
            {
                "code": code,
                "message": message,
                "details": {},
                "request_id": request_id or new_ulid(),
            },
        )

    def _send_bytes(
        self,
        status: HTTPStatus,
        data: bytes,
        content_type: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self._security_headers()
        self.end_headers()
        self.wfile.write(data)

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; media-src 'self'; img-src 'self'; "
            "frame-ancestors 'none'",
        )

    def log_message(self, format: str, *args) -> None:
        print(f"[v3-desktop] {self.address_string()} {format % args}")


class V3DesktopHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address, application: V3DesktopApplication):
        self.application = application
        super().__init__(server_address, V3DesktopRequestHandler)


def create_v3_desktop_server(
    *,
    paths: V3CorePaths,
    host: str = "127.0.0.1",
    port: int = 8766,
    token: str | None = None,
) -> V3DesktopHTTPServer:
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("V3 Desktop API can only bind to loopback")
    core = compose_v3_core(paths)
    core.initialize()
    application = V3DesktopApplication(core, token=token)
    server = V3DesktopHTTPServer((host, port), application)
    bound_host, bound_port = server.server_address[:2]
    application.host = "127.0.0.1" if bound_host in {"0.0.0.0", "::"} else host
    application.port = int(bound_port)
    return server


def serve_v3_desktop(
    *,
    paths: V3CorePaths,
    host: str = "127.0.0.1",
    port: int = 8766,
    open_browser: bool = True,
) -> None:
    server = create_v3_desktop_server(paths=paths, host=host, port=port)
    url = f"{server.application.base_url}/?token={server.application.token}"
    print(f"AllDayRecording V3 工作台：{url}")
    print("仅监听本机；按 Ctrl+C 停止。")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _integer(value: str, label: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be an integer") from exc
    return parsed


def _parse_range(value: str | None, size: int) -> tuple[int, int]:
    if size < 1:
        raise ValueError("media is empty")
    if value is None:
        return 0, size - 1
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip())
    if match is None or (not match.group(1) and not match.group(2)):
        raise ValueError("media range is invalid")
    if not match.group(1):
        length = int(match.group(2))
        if length < 1:
            raise ValueError("media range is invalid")
        return max(0, size - length), size - 1
    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) else size - 1
    if start < 0 or start >= size or end < start:
        raise ValueError("media range is outside the asset")
    return start, min(end, size - 1)


__all__ = [
    "V3DesktopApplication",
    "V3DesktopHTTPServer",
    "V3DesktopRequestHandler",
    "create_v3_desktop_server",
    "serve_v3_desktop",
]
