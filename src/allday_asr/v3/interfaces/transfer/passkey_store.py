from __future__ import annotations

import json
import os
import secrets
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable

from .passkey_types import (
    PASSKEY_REGISTRY_VERSION,
    PasskeyConflictError,
    PasskeyCredentialRecord,
    PasskeyUnauthorizedError,
    _b64decode,
    _b64encode,
    _is_secure_origin,
    _normalize_credential_id,
    _record_payload,
    _utc_now,
)


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
