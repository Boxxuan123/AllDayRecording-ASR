from __future__ import annotations

import secrets
from http import HTTPStatus
from http.cookies import SimpleCookie
from urllib.parse import parse_qs


class TokenAuthMixin:
    """Loopback token/cookie authentication shared by local web handlers."""

    session_cookie_name = "allday_session"
    invalid_link_message = "安全链接已失效"
    unauthorized_message = "未授权"

    def _consume_token(self, parsed) -> bool:
        token = parse_qs(parsed.query).get("token", [None])[0]
        if token is None:
            return False
        if not secrets.compare_digest(token, self.application.token):
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {"error": self.invalid_link_message},
            )
            return True
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/")
        self.send_header(
            "Set-Cookie",
            f"{self.session_cookie_name}={self.application.token}; "
            "Path=/; HttpOnly; SameSite=Strict",
        )
        self._security_headers()
        self.end_headers()
        return True

    def _authenticated(self) -> bool:
        header_token = self.headers.get("X-AllDay-Token")
        if header_token and secrets.compare_digest(
            header_token, self.application.token
        ):
            return True
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        session = cookie.get(self.session_cookie_name)
        return bool(
            session
            and secrets.compare_digest(session.value, self.application.token)
        )

    def _authorized_mutation(self) -> bool:
        if not self._authenticated():
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {"error": self.unauthorized_message},
            )
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {
            self.application.base_url,
            f"http://localhost:{self.application.port}",
        }:
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "拒绝跨站请求"})
            return False
        return True
