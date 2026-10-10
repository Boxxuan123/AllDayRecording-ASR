from __future__ import annotations

import json
from urllib.parse import unquote

from allday_asr.v3.ports.chat_data import ChatDataError
from .desktop_followup_routes import DesktopFollowupRoutesMixin

MESSAGES = {
    "CHAT_NOT_CONFIGURED": "请在本机配置 Mac 服务地址和凭据后重启工作台。",
    "MAC_OFFLINE": "Mac 数据服务不可达；可继续查询已缓存的范围。",
    "UNAUTHORIZED": "Mac 凭据不匹配，请核对两端本地配置。",
    "WORKSET_BUDGET_REACHED": "首批工作集达到 5000 条，已暂停扩大范围。",
    "INITIALIZING": "Mac 投影仍在初始化，请稍后重试。",
    "ANSWER_SOURCE_CHANGED": "回答期间来源发生变化，请重新生成回答。",
    "QUERY_TIMEOUT": "查询超时，请缩小会话或时间范围。",
    "EVIDENCE_SIZE_BUDGET_REACHED": "证据超出单次预算，请缩小范围。",
}


class DesktopChatRoutesMixin(DesktopFollowupRoutesMixin):
    def _log_chat_request(self, args):
        # A scope/cursor/record ID can expose private chat metadata in access logs.
        path = getattr(self, "path", "").split("?", 1)[0]
        if not path.startswith("/api/v3/chat/"):
            return False
        route = path.removeprefix("/api/v3/chat/")
        if route not in {
            "status",
            "accounts",
            "conversations",
            "messages",
            "context",
            "connection",
            "sync",
            "control",
            "answer",
            "cancel",
            "jobs",
        }:
            route = "unknown"
        status = str(args[1]) if len(args) > 1 and str(args[1]).isdigit() else "event"
        print(
            f"[v3-desktop] {self.address_string()} {self.command} /api/v3/chat/{route} {status}"
        )
        return True

    def _chat_call(self, call):
        try:
            self._send_json(200, call())
        except ChatDataError as exc:
            self._send_error(
                exc.status,
                exc.code,
                MESSAGES.get(exc.code, "聊天查询未完成：" + exc.code),
            )

    def _dispatch_chat_get(self, path, query):
        if self._followup_get(path, query):
            return True
        if not path.startswith("/api/v3/chat/"):
            return False

        def run():
            if any(len(v) != 1 for v in query.values()):
                raise ChatDataError("INVALID_REQUEST", 400)

            def one(key, default=None):
                return query.get(key, [default])[0]

            service = self.application.chat
            if path == "/api/v3/chat/status":
                return service.status()
            if path == "/api/v3/chat/jobs":
                job = service.cache.job(one("id"))
                value = job.get("value")
                job["stale"] = bool(
                    value
                    and value.get("generation") != service.cache.get("generation", 0)
                )
                return job
            try:
                scope = json.loads(one("scope", "{}"))
                limit = int(one("limit", "50"))
                before, after = int(one("before", "10")), int(one("after", "10"))
            except (ValueError, TypeError):
                raise ChatDataError("INVALID_REQUEST", 400) from None
            if path == "/api/v3/chat/messages":
                return service.search(scope, one("cursor"), limit, one("origin", "mac"))
            if path == "/api/v3/chat/context":
                return service.context(unquote(one("record_id", "")), before, after)
            for kind in ("accounts", "conversations"):
                if path == "/api/v3/chat/" + kind:
                    return service.directory(kind, scope, one("cursor"))
            raise ChatDataError("NOT_FOUND", 404)

        self._chat_call(run)
        return True

    def _dispatch_chat_post(self, path, body):
        if self._followup_post(path, body):
            return True
        if not path.startswith("/api/v3/chat/"):
            return False

        def run():
            service = self.application.chat
            if path == "/api/v3/chat/connection" and set(body) == {"base_url", "token"}:
                return service.configure(body["base_url"], body["token"])
            if path == "/api/v3/chat/sync" and set(body) <= {"scopes", "reinitialize"}:
                if type(body.get("reinitialize", False)) is not bool:
                    raise ChatDataError("INVALID_REQUEST", 400)
                return service.start_sync(
                    body.get("scopes"), reinitialize=body.get("reinitialize", False)
                )
            if (
                path == "/api/v3/chat/control"
                and "action" in body
                and set(body) <= {"action", "scope_id"}
            ):
                return service.controls(body["action"], body.get("scope_id"))
            if path == "/api/v3/chat/answer" and {"question", "scope"} <= set(body) <= {
                "question",
                "scope",
                "origin",
            }:
                return service.submit_answer(
                    body["question"], body["scope"], body.get("origin", "mac")
                )
            if path == "/api/v3/chat/cancel" and set(body) == {"id"}:
                return service.cancel_answer(body["id"])
            raise ChatDataError("INVALID_REQUEST", 400)

        self._chat_call(run)
        return True
