from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit


PASSKEY_ASSERTION_HEADER = "X-AllDay-Passkey-Assertion"
PASSKEY_CEREMONY_HEADER = "X-AllDay-Passkey-Ceremony"
PASSKEY_REGISTRY_VERSION = 1
PASSKEY_CHALLENGE_TTL_SECONDS = 120.0
MAX_PENDING_CEREMONIES = 256
EMPTY_BODY_SHA256 = hashlib.sha256(b"").hexdigest()
_BASE64URL_ALPHABET = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)


class PasskeyError(ValueError):
    pass


class PasskeyUnauthorizedError(PasskeyError):
    pass


class PasskeyConflictError(PasskeyError):
    pass


@dataclass(frozen=True)
class PasskeyCredentialRecord:
    credential_id: str
    credential_public_key: str
    sign_count: int
    transports: tuple[str, ...]
    device_type: str
    backed_up: bool
    device_name: str
    created_at: str
    last_used_at: str | None = None

    def public_dict(self) -> dict[str, Any]:
        return {
            "credential_id": self.credential_id,
            "device_name": self.device_name,
            "device_type": self.device_type,
            "backed_up": self.backed_up,
            "transports": list(self.transports),
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
        }


@dataclass(frozen=True)
class RequestBinding:
    method: str
    path: str
    body_sha256: str
    upload_offset: int | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> RequestBinding:
        allowed = {"method", "path", "body_sha256", "upload_offset"}
        unknown = sorted(payload.keys() - allowed)
        if unknown:
            raise PasskeyError(f"请求绑定包含未知字段：{', '.join(unknown)}")
        try:
            method = payload["method"]
            path = payload["path"]
            body_sha256 = payload["body_sha256"]
        except KeyError as exc:
            raise PasskeyError(f"请求绑定缺少字段：{exc.args[0]}") from exc
        if not all(isinstance(value, str) for value in (method, path, body_sha256)):
            raise PasskeyError("method、path 和 body_sha256 必须是字符串")
        binding = cls(
            method=method.upper(),
            path=path,
            body_sha256=body_sha256.lower(),
            upload_offset=payload.get("upload_offset"),
        )
        binding.validate()
        return binding

    @classmethod
    def for_request(
        cls,
        *,
        method: str,
        path: str,
        body: bytes,
        upload_offset: int | None = None,
    ) -> RequestBinding:
        binding = cls(
            method=method.upper(),
            path=path,
            body_sha256=hashlib.sha256(body).hexdigest(),
            upload_offset=upload_offset,
        )
        binding.validate()
        return binding

    def validate(self) -> None:
        upload_path = _is_upload_item_path(self.path)
        valid_target = (
            (
                self.method == "GET"
                and self.path in {"/api/v1/status", "/device/v3/status"}
            )
            or (
                self.method == "POST"
                and self.path in {"/api/v1/uploads", "/device/v3/sync"}
            )
            or (self.method == "PUT" and upload_path)
            or (self.method == "GET" and upload_path)
        )
        if not valid_target:
            raise PasskeyError("认证请求绑定的接口或方法不受支持")
        if len(self.body_sha256) != 64 or any(
            value not in "0123456789abcdef" for value in self.body_sha256
        ):
            raise PasskeyError("body_sha256 必须是 64 位十六进制字符串")
        if self.method in {"GET", "POST"} and self.upload_offset is not None:
            raise PasskeyError("只有 PUT 上传分片可以绑定 upload_offset")
        if self.method == "PUT" and (
            isinstance(self.upload_offset, bool)
            or not isinstance(self.upload_offset, int)
            or self.upload_offset < 0
        ):
            raise PasskeyError("PUT 请求必须绑定非负 upload_offset")


@dataclass(frozen=True)
class _Ceremony:
    kind: str
    challenge: bytes
    expires_at: float
    device_name: str | None = None
    request_binding: RequestBinding | None = None


def encode_assertion_header(credential: dict[str, Any]) -> str:
    payload = json.dumps(
        credential,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _b64encode(payload)


def _decode_assertion_header(value: str) -> dict[str, Any]:
    if not isinstance(value, str) or not value or len(value) > 24_000:
        raise PasskeyUnauthorizedError("Passkey assertion 请求头缺失或过长")
    try:
        payload = json.loads(_b64decode(value, label="assertion").decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PasskeyUnauthorizedError("Passkey assertion 请求头不是有效 JSON") from exc
    if not isinstance(payload, dict):
        raise PasskeyUnauthorizedError("Passkey assertion 必须是 JSON 对象")
    return payload


def _credential_transports(credential: dict[str, Any]) -> tuple[str, ...]:
    response = credential.get("response")
    if not isinstance(response, dict):
        return ()
    transports = response.get("transports", ())
    if not isinstance(transports, list):
        return ()
    return tuple(dict.fromkeys(str(value) for value in transports))


def _record_payload(record: PasskeyCredentialRecord) -> dict[str, Any]:
    payload = asdict(record)
    payload["transports"] = list(record.transports)
    return payload


def _normalize_credential_id(value: Any) -> str:
    return _b64encode(_b64decode(value, label="credential_id"))


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: Any, *, label: str = "base64url") -> bytes:
    if (
        not isinstance(value, str)
        or not value
        or any(character not in _BASE64URL_ALPHABET for character in value)
    ):
        raise ValueError(f"{label} 不是有效 base64url 字符串")
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.b64decode(
            value + padding,
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"{label} 不是有效 base64url 字符串") from exc
    if not decoded:
        raise ValueError(f"{label} 不能为空")
    return decoded


def _is_upload_item_path(path: str) -> bool:
    prefix = "/api/v1/uploads/"
    upload_id = path.removeprefix(prefix) if path.startswith(prefix) else ""
    return len(upload_id) == 32 and all(value in "0123456789abcdef" for value in upload_id)


def _is_secure_origin(value: str) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    if (
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and parsed.path == ""
        and not parsed.query
        and not parsed.fragment
    ):
        return True
    prefix = "ohos:app-id:"
    if not value.startswith(prefix):
        return False
    encoded_app_id = value.removeprefix(prefix)
    try:
        padding = "=" * (-len(encoded_app_id) % 4)
        decoded_app_id = base64.b64decode(encoded_app_id + padding, validate=True)
    except (binascii.Error, ValueError):
        return False
    return 32 <= len(decoded_app_id) <= 128


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
