from __future__ import annotations

import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from allday_asr.interfaces.web.application import WebApplication
from allday_asr.interfaces.web.auth import TokenAuthMixin
from allday_asr.interfaces.web.responses import LocalResponseMixin
from allday_asr.interfaces.web.router import (
    dispatch_get,
    dispatch_post,
    dispatch_put,
)
from allday_asr.paths import DEFAULT_CONFIG_PATH, DEFAULT_DB_PATH

ASSET_ROOT = Path(__file__).parents[2] / "web_assets"


class AllDayRequestHandler(
    TokenAuthMixin,
    LocalResponseMixin,
    BaseHTTPRequestHandler,
):
    asset_root = ASSET_ROOT
    server: "AllDayHTTPServer"

    def do_GET(self) -> None:
        self._handle(lambda: dispatch_get(self))

    def do_POST(self) -> None:
        if self.application.read_only:
            self._send_json(
                HTTPStatus.METHOD_NOT_ALLOWED,
                {"error": "Legacy 工作台为只读入口，写操作已禁用。"},
            )
            return
        self._handle(lambda: dispatch_post(self))

    def do_PUT(self) -> None:
        if self.application.read_only:
            self._send_json(
                HTTPStatus.METHOD_NOT_ALLOWED,
                {"error": "Legacy 工作台为只读入口，写操作已禁用。"},
            )
            return
        self._handle(lambda: dispatch_put(self))

    def _handle(self, callback) -> None:
        try:
            callback()
        except FileNotFoundError as exc:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except KeyError as exc:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": f"本地服务错误：{exc}"},
            )

    @property
    def application(self) -> WebApplication:
        return self.server.application

    def log_message(self, format: str, *args) -> None:
        print(f"[web] {self.address_string()} {format % args}")


class AllDayHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address, application: WebApplication):
        self.application = application
        super().__init__(server_address, AllDayRequestHandler)


def create_web_server(
    *,
    database_path: Path = DEFAULT_DB_PATH,
    config_path: Path = DEFAULT_CONFIG_PATH,
    host: str = "127.0.0.1",
    port: int = 8765,
    token: str | None = None,
    read_only: bool = False,
) -> AllDayHTTPServer:
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("本地工作台只能绑定 127.0.0.1 或 localhost")
    application = WebApplication(
        database_path,
        config_path,
        token=token,
        read_only=read_only,
    )
    server = AllDayHTTPServer((host, port), application)
    bound_host, bound_port = server.server_address[:2]
    application.host = "127.0.0.1" if bound_host in {"0.0.0.0", "::"} else host
    application.port = int(bound_port)
    return server


def serve_web(
    *,
    database_path: Path = DEFAULT_DB_PATH,
    config_path: Path = DEFAULT_CONFIG_PATH,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
    read_only: bool = False,
) -> None:
    server = create_web_server(
        database_path=database_path,
        config_path=config_path,
        host=host,
        port=port,
        read_only=read_only,
    )
    url = f"{server.application.base_url}/?token={server.application.token}"
    label = "Legacy 只读工作台" if read_only else "本地工作台"
    print(f"AllDayRecording {label}：{url}")
    print("仅监听本机；按 Ctrl+C 停止。")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
