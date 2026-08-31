from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import secrets
import struct
import threading
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from allday_asr.interfaces.transfer.passkeys import RequestBinding


DEVICE_ALGORITHM = "ECDSA_P256_SHA256"
DEVICE_AUTH_VERSION = 1
DEVICE_CHALLENGE_TTL_SECONDS = 120.0
DEVICE_REGISTRY_VERSION = 1
MAX_PENDING_DEVICE_CHALLENGES = 256
DEVICE_ID_HEADER = "X-AllDay-Device-ID"
DEVICE_CHALLENGE_HEADER = "X-AllDay-Device-Challenge"
DEVICE_SIGNATURE_HEADER = "X-AllDay-Device-Signature"
_SIGNATURE_PREFIX = "ALL_DAY_RECORDING_DEVICE_AUTH_V1"
_BASE64URL_ALPHABET = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)


class DeviceAuthError(ValueError):
    pass


class DeviceUnauthorizedError(DeviceAuthError):
    pass


class DeviceConflictError(DeviceAuthError):
    pass


@dataclass(frozen=True)
class DeviceCredentialRecord:
    device_id: str
    device_name: str
    algorithm: str
    public_key: str
    passkey_credential_id: str
    created_at: str
    last_used_at: str | None = None

    def public_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "device_name": self.device_name,
            "algorithm": self.algorithm,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
        }


@dataclass(frozen=True)
class _DeviceChallenge:
    device_id: str
    nonce: str
    request_binding: RequestBinding
    expires_at: float


class DeviceCredentialStore:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write_payload(
                {
                    "version": DEVICE_REGISTRY_VERSION,
                    "devices": [],
                }
            )
        self._read_payload()

    def list_devices(self) -> tuple[DeviceCredentialRecord, ...]:
        with self._lock:
            payload = self._read_payload()
            return tuple(self._record(raw) for raw in payload["devices"])

    def get(self, device_id: str) -> DeviceCredentialRecord:
        normalized = _normalize_device_id(device_id)
        for record in self.list_devices():
            if secrets.compare_digest(record.device_id, normalized):
                return record
        raise DeviceUnauthorizedError("设备密钥未登记或已撤销")

    def register(
        self,
        *,
        device_name: str,
        algorithm: str,
        public_key: str,
        passkey_credential_id: str,
    ) -> DeviceCredentialRecord:
        normalized_name = _normalize_device_name(device_name)
        normalized_key, device_id = validate_device_public_key(
            algorithm=algorithm,
            encoded_public_key=public_key,
        )
        record = DeviceCredentialRecord(
            device_id=device_id,
            device_name=normalized_name,
            algorithm=DEVICE_ALGORITHM,
            public_key=normalized_key,
            passkey_credential_id=passkey_credential_id,
            created_at=_utc_now(),
        )
        with self._lock:
            payload = self._read_payload()
            for raw in payload["devices"]:
                existing = self._record(raw)
                if secrets.compare_digest(existing.device_id, device_id):
                    if not secrets.compare_digest(existing.public_key, normalized_key):
                        raise DeviceConflictError("设备标识与已登记公钥冲突")
                    return existing
            payload["devices"].append(asdict(record))
            self._write_payload(payload)
        return record

    def mark_used(self, device_id: str) -> DeviceCredentialRecord:
        normalized = _normalize_device_id(device_id)
        with self._lock:
            payload = self._read_payload()
            for index, raw in enumerate(payload["devices"]):
                record = self._record(raw)
                if not secrets.compare_digest(record.device_id, normalized):
                    continue
                updated = replace(record, last_used_at=_utc_now())
                payload["devices"][index] = asdict(updated)
                self._write_payload(payload)
                return updated
        raise DeviceUnauthorizedError("设备密钥未登记或已撤销")

    def _read_payload(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("设备密钥注册表损坏，服务不会静默重建") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("version") != DEVICE_REGISTRY_VERSION
            or not isinstance(payload.get("devices"), list)
        ):
            raise ValueError("设备密钥注册表版本或格式无效")
        for raw in payload["devices"]:
            self._record(raw)
        return payload

    def _record(self, payload: Any) -> DeviceCredentialRecord:
        if not isinstance(payload, dict):
            raise ValueError("设备密钥记录格式无效")
        try:
            record = DeviceCredentialRecord(
                device_id=_normalize_device_id(payload["device_id"]),
                device_name=_normalize_device_name(payload["device_name"]),
                algorithm=str(payload["algorithm"]),
                public_key=str(payload["public_key"]),
                passkey_credential_id=str(payload["passkey_credential_id"]),
                created_at=str(payload["created_at"]),
                last_used_at=(
                    str(payload["last_used_at"])
                    if payload.get("last_used_at") is not None
                    else None
                ),
            )
            normalized_key, derived_id = validate_device_public_key(
                algorithm=record.algorithm,
                encoded_public_key=record.public_key,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("设备密钥记录格式无效") from exc
        if (
            normalized_key != record.public_key
            or not secrets.compare_digest(derived_id, record.device_id)
            or not record.passkey_credential_id
        ):
            raise ValueError("设备密钥记录值无效")
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


class DeviceAuthManager:
    def __init__(
        self,
        store: DeviceCredentialStore,
        *,
        nonce_factory: Callable[[], bytes] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store = store
        self._nonce_factory = nonce_factory or (lambda: secrets.token_bytes(32))
        self._clock = clock
        self._challenges: dict[str, _DeviceChallenge] = {}
        self._lock = threading.Lock()

    def start_authentication(
        self,
        *,
        device_id: str,
        binding: RequestBinding,
    ) -> dict[str, Any]:
        record = self.store.get(device_id)
        nonce_bytes = self._nonce_factory()
        if not isinstance(nonce_bytes, bytes) or len(nonce_bytes) < 16:
            raise RuntimeError("设备 challenge factory 必须返回至少 16 个随机字节")
        nonce = _b64encode(nonce_bytes)
        with self._lock:
            self._purge_expired()
            while len(self._challenges) >= MAX_PENDING_DEVICE_CHALLENGES:
                oldest = next(iter(self._challenges))
                self._challenges.pop(oldest)
            challenge_id = secrets.token_urlsafe(18)
            self._challenges[challenge_id] = _DeviceChallenge(
                device_id=record.device_id,
                nonce=nonce,
                request_binding=binding,
                expires_at=self._clock() + DEVICE_CHALLENGE_TTL_SECONDS,
            )
        return {
            "version": DEVICE_AUTH_VERSION,
            "challenge_id": challenge_id,
            "nonce": nonce,
            "expires_in_ms": int(DEVICE_CHALLENGE_TTL_SECONDS * 1000),
        }

    def verify_request(
        self,
        *,
        device_id: str,
        challenge_id: str,
        encoded_signature: str,
        binding: RequestBinding,
    ) -> DeviceCredentialRecord:
        normalized_device_id = _normalize_device_id(device_id)
        if not isinstance(challenge_id, str) or not challenge_id:
            raise DeviceUnauthorizedError("设备 challenge id 缺失")
        with self._lock:
            challenge = self._challenges.pop(challenge_id, None)
        if challenge is None:
            raise DeviceUnauthorizedError("设备 challenge 不存在、已使用或已过期")
        if challenge.expires_at < self._clock():
            raise DeviceUnauthorizedError("设备 challenge 已过期")
        if (
            not secrets.compare_digest(challenge.device_id, normalized_device_id)
            or challenge.request_binding != binding
        ):
            raise DeviceUnauthorizedError("设备签名与实际 HTTP 请求不匹配")
        record = self.store.get(normalized_device_id)
        signature = _b64decode(encoded_signature, label="设备签名", max_bytes=256)
        public_key = _load_device_public_key(
            _b64decode(record.public_key, label="设备公钥", max_bytes=512)
        )
        payload = build_device_signature_payload(
            challenge_id=challenge_id,
            nonce=challenge.nonce,
            binding=binding,
        )
        if len(signature) == 64:
            signature = encode_dss_signature(
                int.from_bytes(signature[:32], "big"),
                int.from_bytes(signature[32:], "big"),
            )
        try:
            public_key.verify(signature, payload, ec.ECDSA(hashes.SHA256()))
        except (InvalidSignature, TypeError, ValueError) as exc:
            raise DeviceUnauthorizedError("设备签名验证失败") from exc
        return self.store.mark_used(record.device_id)

    def _purge_expired(self) -> None:
        now = self._clock()
        expired = [
            challenge_id
            for challenge_id, challenge in self._challenges.items()
            if challenge.expires_at < now
        ]
        for challenge_id in expired:
            self._challenges.pop(challenge_id, None)


def validate_device_public_key(
    *,
    algorithm: str,
    encoded_public_key: str,
) -> tuple[str, str]:
    if algorithm != DEVICE_ALGORITHM:
        raise DeviceAuthError(f"不支持的设备密钥算法：{algorithm}")
    raw = _b64decode(encoded_public_key, label="设备公钥", max_bytes=512)
    public_key = _load_device_public_key(raw)
    canonical = public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    normalized = _b64encode(canonical)
    device_id = _b64encode(hashlib.sha256(canonical).digest())
    return normalized, device_id


def _load_device_public_key(raw: bytes) -> ec.EllipticCurvePublicKey:
    """Accept standard SPKI and the public material exported by Harmony HUKS."""
    try:
        candidate = serialization.load_der_public_key(raw)
    except (TypeError, ValueError):
        candidate = None
    if isinstance(candidate, ec.EllipticCurvePublicKey):
        if not isinstance(candidate.curve, ec.SECP256R1):
            raise DeviceAuthError("设备公钥必须是 P-256 ECC 公钥")
        return candidate

    coordinate_pairs: list[tuple[bytes, bytes]] = []
    if len(raw) == 65 and raw[0] == 4:
        coordinate_pairs.append((raw[1:33], raw[33:65]))
    elif len(raw) == 64:
        coordinate_pairs.append((raw[:32], raw[32:]))
    elif len(raw) >= 20:
        for byte_order in ("<", ">"):
            try:
                _, key_size, x_size, y_size, z_size = struct.unpack(
                    f"{byte_order}IIIII", raw[:20]
                )
            except struct.error:
                continue
            if (
                key_size == 256
                and x_size == 32
                and y_size == 32
                and z_size == 0
                and len(raw) == 20 + x_size + y_size
            ):
                coordinate_pairs.append((raw[20:52], raw[52:84]))

    for x_bytes, y_bytes in coordinate_pairs:
        for x_value, y_value in (
            (x_bytes, y_bytes),
            (x_bytes[::-1], y_bytes[::-1]),
        ):
            try:
                return ec.EllipticCurvePublicNumbers(
                    int.from_bytes(x_value, "big"),
                    int.from_bytes(y_value, "big"),
                    ec.SECP256R1(),
                ).public_key()
            except ValueError:
                continue
    raise DeviceAuthError("设备公钥不是有效的 P-256 公钥")


def build_device_signature_payload(
    *,
    challenge_id: str,
    nonce: str,
    binding: RequestBinding,
) -> bytes:
    for label, value in (("challenge_id", challenge_id), ("nonce", nonce)):
        if not isinstance(value, str) or not value or "\n" in value:
            raise DeviceAuthError(f"{label} 无效")
    offset = "" if binding.upload_offset is None else str(binding.upload_offset)
    return "\n".join(
        (
            _SIGNATURE_PREFIX,
            challenge_id,
            nonce,
            binding.method,
            binding.path,
            binding.body_sha256,
            offset,
        )
    ).encode("utf-8")


def _normalize_device_id(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 43:
        raise DeviceUnauthorizedError("设备标识无效")
    if any(character not in _BASE64URL_ALPHABET for character in value):
        raise DeviceUnauthorizedError("设备标识无效")
    return value


def _normalize_device_name(value: Any) -> str:
    if not isinstance(value, str):
        raise DeviceAuthError("device_name 必须是字符串")
    normalized = value.strip()
    if not normalized or len(normalized) > 80:
        raise DeviceAuthError("device_name 不能为空且不能超过 80 个字符")
    return normalized


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: Any, *, label: str, max_bytes: int) -> bytes:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > max_bytes * 2
        or any(character not in _BASE64URL_ALPHABET for character in value)
    ):
        raise DeviceUnauthorizedError(f"{label} 格式无效")
    try:
        raw = base64.b64decode(
            value + "=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError) as exc:
        raise DeviceUnauthorizedError(f"{label} 格式无效") from exc
    if not raw or len(raw) > max_bytes or _b64encode(raw) != value:
        raise DeviceUnauthorizedError(f"{label} 格式无效")
    return raw


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
