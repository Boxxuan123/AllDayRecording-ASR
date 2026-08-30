from __future__ import annotations

from http import HTTPStatus
from urllib.parse import urlparse

from allday_asr.interfaces.web.routes.actions import get_actions, post_actions
from allday_asr.interfaces.web.routes.assets import get_asset
from allday_asr.interfaces.web.routes.audio import get_audio
from allday_asr.interfaces.web.routes.evaluation import (
    get_evaluation,
    post_evaluation,
    put_evaluation,
)
from allday_asr.interfaces.web.routes.semantic import get_semantic, post_semantic
from allday_asr.interfaces.web.routes.timeline import get_timeline, post_timeline
from allday_asr.interfaces.web.routes.workspace import (
    get_workspace,
    post_workspace,
)

GET_ROUTES = (
    get_asset,
    get_workspace,
    get_evaluation,
    get_actions,
    get_semantic,
    get_timeline,
    get_audio,
)

POST_ROUTES = (
    post_workspace,
    post_timeline,
    post_evaluation,
    post_semantic,
    post_actions,
)


def dispatch_get(handler) -> None:
    parsed = urlparse(handler.path)
    if parsed.path == "/" and handler._consume_token(parsed):
        return
    if not handler._authenticated():
        handler._send_json(
            HTTPStatus.FORBIDDEN,
            {"error": "请从命令输出的安全链接打开网页"},
        )
        return
    for route in GET_ROUTES:
        if route(handler, parsed):
            return
    handler._send_json(HTTPStatus.NOT_FOUND, {"error": "页面不存在"})


def dispatch_post(handler) -> None:
    if not handler._authorized_mutation():
        return
    parsed = urlparse(handler.path)
    body = handler._read_json()
    for route in POST_ROUTES:
        if route(handler, parsed, body):
            return
    handler._send_json(HTTPStatus.NOT_FOUND, {"error": "接口不存在"})


def dispatch_put(handler) -> None:
    if not handler._authorized_mutation():
        return
    parsed = urlparse(handler.path)
    body = handler._read_json()
    if put_evaluation(handler, parsed, body):
        return
    handler._send_json(HTTPStatus.NOT_FOUND, {"error": "接口不存在"})
