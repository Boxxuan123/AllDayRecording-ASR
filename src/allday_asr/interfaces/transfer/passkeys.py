from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import secrets
import threading
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.cose import COSEAlgorithmIdentifier
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
    AuthenticatorAttachment,
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    PublicKeyCredentialHint,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)


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
            (self.method == "GET" and (self.path == "/api/v1/status" or upload_path))
            or (self.method == "POST" and self.path == "/api/v1/uploads")
            or (self.method == "PUT" and upload_path)
        )
        if not valid_target:
            raise PasskeyError("Passkey 请求绑定的接口或方法不受支持")
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


class PasskeyCredentialStore:
    def __init__(
        self,
        path: Path,
        *,
        rp_id: str,
        expected_origins: Iterable[str],
    ) -> None:
        if not rp_id.strip() or "://" in rp_id:
            raise ValueError("Passkey RP ID 必须是稳定域名，不能包含 scheme")
        origins = tuple(dict.fromkeys(value.strip() for value in expected_origins))
        if not origins or any(not _is_secure_origin(value) for value in origins):
            raise ValueError("Passkey origin 必须是至少一个不含路径的 HTTPS origin")
        self.path = path.expanduser().resolve()
        self.rp_id = rp_id
        self.expected_origins = origins
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write_payload(
                {
                    "version": PASSKEY_REGISTRY_VERSION,
                    "rp_id": self.rp_id,
                    "expected_origins": list(self.expected_origins),
                    "user_id": _b64encode(secrets.token_bytes(32)),
                    "credentials": [],
                }
            )
        else:
            self._migrate_empty_registry_origins()
        self._read_payload()

    @property
    def user_id(self) -> bytes:
        with self._lock:
            return _b64decode(self._read_payload()["user_id"], label="user_id")

    def list_credentials(self) -> tuple[PasskeyCredentialRecord, ...]:
        with self._lock:
            payload = self._read_payload()
            return tuple(self._record(value) for value in payload["credentials"])

    def get(self, credential_id: str) -> PasskeyCredentialRecord:
        try:
            normalized = _normalize_credential_id(credential_id)
        except ValueError as exc:
            raise PasskeyUnauthorizedError("Passkey credential id 无效") from exc
        for record in self.list_credentials():
            if secrets.compare_digest(record.credential_id, normalized):
                return record
        raise PasskeyUnauthorizedError("Passkey 凭据未登记或已撤销")

    def add(self, record: PasskeyCredentialRecord) -> PasskeyCredentialRecord:
        with self._lock:
            payload = self._read_payload()
            for existing in payload["credentials"]:
                if secrets.compare_digest(existing["credential_id"], record.credential_id):
                    raise PasskeyConflictError("该 Passkey 已经登记")
            payload["credentials"].append(_record_payload(record))
            self._write_payload(payload)
            return record

    def update_authentication(
        self,
        credential_id: str,
        *,
        sign_count: int,
        device_type: str,
        backed_up: bool,
    ) -> PasskeyCredentialRecord:
        normalized = _normalize_credential_id(credential_id)
        with self._lock:
            payload = self._read_payload()
            for index, raw in enumerate(payload["credentials"]):
                record = self._record(raw)
                if not secrets.compare_digest(record.credential_id, normalized):
                    continue
                updated = replace(
                    record,
                    # Concurrent assertions may verify against the same old count.
                    # Never let their completion order roll the persisted counter back.
                    sign_count=max(record.sign_count, sign_count),
                    device_type=device_type,
                    backed_up=backed_up,
                    last_used_at=_utc_now(),
                )
                payload["credentials"][index] = _record_payload(updated)
                self._write_payload(payload)
                return updated
        raise PasskeyUnauthorizedError("Passkey 凭据未登记或已撤销")

    def _read_payload(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("Passkey 注册表损坏，服务不会静默重建") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("version") != PASSKEY_REGISTRY_VERSION
            or payload.get("rp_id") != self.rp_id
            or tuple(payload.get("expected_origins", ())) != self.expected_origins
            or not isinstance(payload.get("credentials"), list)
        ):
            raise ValueError("Passkey 注册表版本、RP ID 或 origin 不匹配")
        _b64decode(payload.get("user_id"), label="user_id")
        for raw in payload["credentials"]:
            self._record(raw)
        return payload

    def _migrate_empty_registry_origins(self) -> None:
        """Allow correcting origin configuration before any credential is trusted."""
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        stored_origins = payload.get("expected_origins") if isinstance(payload, dict) else None
        if (
            payload.get("version") != PASSKEY_REGISTRY_VERSION
            or payload.get("rp_id") != self.rp_id
            or payload.get("credentials") != []
            or not isinstance(stored_origins, list)
            or not stored_origins
            or any(not _is_secure_origin(value) for value in stored_origins)
        ):
            return
        if tuple(stored_origins) != self.expected_origins:
            _b64decode(payload.get("user_id"), label="user_id")
            payload["expected_origins"] = list(self.expected_origins)
            self._write_payload(payload)

    def _record(self, payload: Any) -> PasskeyCredentialRecord:
        if not isinstance(payload, dict):
            raise ValueError("Passkey 凭据记录格式无效")
        try:
            sign_count = payload["sign_count"]
            backed_up = payload["backed_up"]
            transports = payload["transports"]
            if (
                isinstance(sign_count, bool)
                or not isinstance(sign_count, int)
                or not isinstance(backed_up, bool)
                or not isinstance(transports, list)
                or any(not isinstance(value, str) for value in transports)
            ):
                raise ValueError
            record = PasskeyCredentialRecord(
                credential_id=_normalize_credential_id(payload["credential_id"]),
                credential_public_key=_b64encode(
                    _b64decode(
                        payload["credential_public_key"],
                        label="credential_public_key",
                    )
                ),
                sign_count=sign_count,
                transports=tuple(transports),
                device_type=str(payload["device_type"]),
                backed_up=backed_up,
                device_name=str(payload["device_name"]),
                created_at=str(payload["created_at"]),
                last_used_at=(
                    str(payload["last_used_at"])
                    if payload.get("last_used_at") is not None
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Passkey 凭据记录格式无效") from exc
        if record.sign_count < 0 or not record.device_name.strip():
            raise ValueError("Passkey 凭据记录值无效")
        return record

    def _write_payload(self, payload: dict[str, Any]) -> None:
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        data = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)


class PasskeyManager:
    def __init__(
        self,
        store: PasskeyCredentialStore,
        *,
        challenge_factory: Callable[[], bytes] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store = store
        self._challenge_factory = challenge_factory or (lambda: secrets.token_bytes(32))
        self._clock = clock
        self._ceremonies: dict[str, _Ceremony] = {}
        self._lock = threading.Lock()

    def start_registration(self, *, device_name: str) -> dict[str, Any]:
        if not isinstance(device_name, str):
            raise PasskeyError("device_name 必须是字符串")
        normalized_name = device_name.strip()
        if not normalized_name or len(normalized_name) > 80:
            raise PasskeyError("device_name 不能为空且不能超过 80 个字符")
        challenge = self._new_challenge()
        options = generate_registration_options(
            rp_id=self.store.rp_id,
            rp_name="AllDayRecording Receiver",
            user_id=self.store.user_id,
            user_name="allday-recording-phone",
            user_display_name=normalized_name,
            challenge=challenge,
            timeout=120_000,
            attestation=AttestationConveyancePreference.NONE,
            authenticator_selection=AuthenticatorSelectionCriteria(
                authenticator_attachment=AuthenticatorAttachment.PLATFORM,
                resident_key=ResidentKeyRequirement.REQUIRED,
                require_resident_key=True,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
            exclude_credentials=[
                PublicKeyCredentialDescriptor(id=_b64decode(value.credential_id))
                for value in self.store.list_credentials()
            ],
            supported_pub_key_algs=[COSEAlgorithmIdentifier.ECDSA_SHA_256],
            hints=[PublicKeyCredentialHint.CLIENT_DEVICE],
        )
        ceremony_id = self._remember(
            _Ceremony(
                kind="registration",
                challenge=challenge,
                expires_at=self._clock() + PASSKEY_CHALLENGE_TTL_SECONDS,
                device_name=normalized_name,
            )
        )
        return {
            "ceremony_id": ceremony_id,
            "public_key": json.loads(options_to_json(options)),
        }

    def finish_registration(
        self,
        *,
        ceremony_id: str,
        credential: dict[str, Any],
    ) -> PasskeyCredentialRecord:
        ceremony = self._consume(ceremony_id, kind="registration")
        try:
            verification = verify_registration_response(
                credential=credential,
                expected_challenge=ceremony.challenge,
                expected_rp_id=self.store.rp_id,
                expected_origin=list(self.store.expected_origins),
                require_user_presence=True,
                require_user_verification=True,
            )
        except (TypeError, ValueError, WebAuthnException) as exc:
            raise PasskeyUnauthorizedError(f"Passkey 登记验证失败：{exc}") from exc
        transports = _credential_transports(credential)
        record = PasskeyCredentialRecord(
            credential_id=_b64encode(verification.credential_id),
            credential_public_key=_b64encode(verification.credential_public_key),
            sign_count=verification.sign_count,
            transports=transports,
            device_type=verification.credential_device_type.value,
            backed_up=verification.credential_backed_up,
            device_name=ceremony.device_name or "Phone",
            created_at=_utc_now(),
        )
        return self.store.add(record)

    def start_authentication(self, binding: RequestBinding) -> dict[str, Any]:
        if not self.store.list_credentials():
            raise PasskeyConflictError("尚未登记 Passkey，请先完成首次配对")
        challenge = self._new_challenge()
        options = generate_authentication_options(
            rp_id=self.store.rp_id,
            challenge=challenge,
            timeout=120_000,
            allow_credentials=None,
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        ceremony_id = self._remember(
            _Ceremony(
                kind="authentication",
                challenge=challenge,
                expires_at=self._clock() + PASSKEY_CHALLENGE_TTL_SECONDS,
                request_binding=binding,
            )
        )
        return {
            "ceremony_id": ceremony_id,
            "public_key": json.loads(options_to_json(options)),
        }

    def verify_request(
        self,
        *,
        ceremony_id: str,
        encoded_assertion: str,
        binding: RequestBinding,
    ) -> PasskeyCredentialRecord:
        ceremony = self._consume(ceremony_id, kind="authentication")
        if ceremony.request_binding != binding:
            raise PasskeyUnauthorizedError("Passkey assertion 与实际 HTTP 请求不匹配")
        credential = _decode_assertion_header(encoded_assertion)
        credential_id = credential.get("id")
        if not isinstance(credential_id, str):
            raise PasskeyUnauthorizedError("Passkey assertion 缺少 credential id")
        stored = self.store.get(credential_id)
        try:
            verification = verify_authentication_response(
                credential=credential,
                expected_challenge=ceremony.challenge,
                expected_rp_id=self.store.rp_id,
                expected_origin=list(self.store.expected_origins),
                credential_public_key=_b64decode(stored.credential_public_key),
                credential_current_sign_count=stored.sign_count,
                require_user_verification=True,
            )
        except (TypeError, ValueError, WebAuthnException) as exc:
            raise PasskeyUnauthorizedError(f"Passkey assertion 验证失败：{exc}") from exc
        return self.store.update_authentication(
            stored.credential_id,
            sign_count=verification.new_sign_count,
            device_type=verification.credential_device_type.value,
            backed_up=verification.credential_backed_up,
        )

    def _new_challenge(self) -> bytes:
        challenge = self._challenge_factory()
        if not isinstance(challenge, bytes) or len(challenge) < 16:
            raise RuntimeError("Passkey challenge factory 必须返回至少 16 个随机字节")
        return challenge

    def _remember(self, ceremony: _Ceremony) -> str:
        with self._lock:
            self._purge_expired()
            while len(self._ceremonies) >= MAX_PENDING_CEREMONIES:
                oldest = next(iter(self._ceremonies))
                self._ceremonies.pop(oldest)
            ceremony_id = secrets.token_urlsafe(18)
            self._ceremonies[ceremony_id] = ceremony
            return ceremony_id

    def _consume(self, ceremony_id: str, *, kind: str) -> _Ceremony:
        if not isinstance(ceremony_id, str) or not ceremony_id:
            raise PasskeyUnauthorizedError("Passkey ceremony id 缺失")
        with self._lock:
            ceremony = self._ceremonies.pop(ceremony_id, None)
        if ceremony is None or ceremony.kind != kind:
            raise PasskeyUnauthorizedError("Passkey challenge 不存在、已使用或已过期")
        if ceremony.expires_at < self._clock():
            raise PasskeyUnauthorizedError("Passkey challenge 已过期")
        return ceremony

    def _purge_expired(self) -> None:
        now = self._clock()
        expired = [
            ceremony_id
            for ceremony_id, ceremony in self._ceremonies.items()
            if ceremony.expires_at < now
        ]
        for ceremony_id in expired:
            self._ceremonies.pop(ceremony_id, None)


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
