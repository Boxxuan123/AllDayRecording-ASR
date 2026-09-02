from __future__ import annotations

import json
import secrets
import threading
import time
from typing import Any, Callable

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.cose import COSEAlgorithmIdentifier
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
    AuthenticatorAttachment,
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    PublicKeyCredentialHint,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from .passkey_store import PasskeyCredentialStore
from .passkey_types import (
    MAX_PENDING_CEREMONIES,
    PASSKEY_CHALLENGE_TTL_SECONDS,
    PasskeyConflictError,
    PasskeyCredentialRecord,
    PasskeyError,
    PasskeyUnauthorizedError,
    RequestBinding,
    _Ceremony,
    _b64decode,
    _b64encode,
    _credential_transports,
    _decode_assertion_header,
    _utc_now,
)


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
