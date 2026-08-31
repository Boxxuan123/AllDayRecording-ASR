from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from allday_asr.interfaces.transfer.devices import (
    DeviceAuthManager,
    DeviceCredentialRecord as LegacyDeviceCredentialRecord,
    DeviceForbiddenError,
    DeviceUnauthorizedError,
)
from allday_asr.interfaces.transfer.passkeys import RequestBinding
from allday_asr.v3.domain.device_sync import (
    DEFAULT_PHONE_SCOPES,
    DeviceCredential,
    DeviceScope,
    PairingRecord,
)
from allday_asr.v3.domain.ids import stable_ulid
from allday_asr.v3.domain.models import (
    Device,
    DeviceKind,
    DeviceStatus,
)
from allday_asr.v3.ports.repositories import UnitOfWork


UnitOfWorkFactory = Callable[[], UnitOfWork]
DateTimeClock = Callable[[], datetime]


class TransferDeviceTrustAdapter:
    """Add V3 scopes/revocation/audit around the proven V2 signature verifier."""

    def __init__(
        self,
        legacy: DeviceAuthManager,
        uow_factory: UnitOfWorkFactory,
        receiver_id: str,
        *,
        now: DateTimeClock | None = None,
    ) -> None:
        self.legacy = legacy
        self.store = legacy.store
        self._uow_factory = uow_factory
        self.receiver_id = receiver_id
        self._now = now or (lambda: datetime.now(timezone.utc))

    def reconcile(self) -> int:
        created = 0
        for record in self.store.list_devices():
            created += int(self.enroll(record))
        return created

    def enroll(self, record: LegacyDeviceCredentialRecord) -> bool:
        now = self._now().astimezone(timezone.utc)
        device_id = stable_ulid("phone-device", record.device_id)
        device = Device(
            device_id=device_id,
            kind=DeviceKind.PHONE,
            name=record.device_name,
            status=DeviceStatus.ACTIVE,
            revision=1,
            last_seen_at=now,
            created_at=now,
            updated_at=now,
        )
        credential = DeviceCredential(
            credential_id=stable_ulid("device-credential", record.device_id),
            device_id=device_id,
            key_id=record.device_id,
            algorithm=record.algorithm,
            public_key=record.public_key,
            scopes=DEFAULT_PHONE_SCOPES,
            passkey_credential_ref=record.passkey_credential_id,
            created_at=now,
        )
        pairing = PairingRecord(
            pairing_id=stable_ulid(
                "pairing", self.receiver_id, record.device_id
            ),
            device_id=device_id,
            receiver_id=self.receiver_id,
            passkey_credential_ref=record.passkey_credential_id,
            paired_at=now,
        )
        with self._uow_factory() as uow:
            uow.devices.add(device)
            added = uow.device_trust.enroll(credential, pairing)
            if added:
                uow.audit.append(
                    "device.enroll",
                    "passkey",
                    "device",
                    device_id,
                    {
                        "key_id": record.device_id,
                        "receiver_id": self.receiver_id,
                        "scopes": [scope.value for scope in credential.scopes],
                    },
                    legacy_ref=f"device-enroll:{record.device_id}",
                )
        return added

    def start_authentication(
        self, *, device_id: str, binding: RequestBinding
    ) -> dict[str, object]:
        self._authorize(device_id, _required_scopes(binding))
        return self.legacy.start_authentication(
            device_id=device_id,
            binding=binding,
        )

    def verify_request(
        self,
        *,
        device_id: str,
        challenge_id: str,
        encoded_signature: str,
        binding: RequestBinding,
    ) -> LegacyDeviceCredentialRecord:
        credential = self._authorize(device_id, _required_scopes(binding))
        record = self.legacy.verify_request(
            device_id=device_id,
            challenge_id=challenge_id,
            encoded_signature=encoded_signature,
            binding=binding,
        )
        with self._uow_factory() as uow:
            uow.device_trust.mark_used(device_id)
            uow.audit.append(
                "device.authenticate",
                f"device:{credential.device_id}",
                "device",
                credential.device_id,
                {
                    "method": binding.method,
                    "path": binding.path,
                    "scopes": [
                        scope.value for scope in _required_scopes(binding)
                    ],
                },
            )
        return record

    def revoke(self, key_id: str, *, actor: str, reason: str) -> bool:
        revoked_at = self._now().astimezone(timezone.utc)
        text = revoked_at.isoformat().replace("+00:00", "Z")
        with self._uow_factory() as uow:
            credential = uow.device_trust.find_by_key_id(key_id)
            if credential is None:
                return False
            changed = uow.device_trust.revoke(key_id, text)
            if changed:
                uow.audit.append(
                    "device.revoke",
                    actor,
                    "device",
                    credential.device_id,
                    {"key_id": key_id, "reason": reason},
                )
        return changed

    def domain_device_id(self, key_id: str) -> str:
        credential = self._authorize(key_id, ())
        return credential.device_id

    def _authorize(
        self, key_id: str, required: tuple[DeviceScope, ...]
    ) -> DeviceCredential:
        try:
            with self._uow_factory() as uow:
                return uow.device_trust.authorize(key_id, required)
        except LookupError as exc:
            raise DeviceUnauthorizedError(
                "设备密钥未登记、已撤销或不属于当前 V3 Core"
            ) from exc
        except PermissionError as exc:
            raise DeviceForbiddenError(str(exc)) from exc


def _required_scopes(binding: RequestBinding) -> tuple[DeviceScope, ...]:
    if binding.path == "/device/v3/status":
        return (DeviceScope.DEVICE_STATUS,)
    if binding.path == "/device/v3/sync":
        return (DeviceScope.DATA_SYNC_READ, DeviceScope.DATA_SYNC_WRITE)
    if binding.path == "/api/v1/uploads" or binding.path.startswith(
        "/api/v1/uploads/"
    ):
        return (DeviceScope.AUDIO_UPLOAD,)
    if binding.path == "/api/v1/status":
        return (DeviceScope.DEVICE_STATUS,)
    return ()


__all__ = ["TransferDeviceTrustAdapter"]
