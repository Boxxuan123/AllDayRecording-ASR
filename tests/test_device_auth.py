from __future__ import annotations

import base64
import json
import shutil
import struct
import threading
import unittest
import urllib.error
import urllib.request
from http import HTTPStatus
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from allday_asr.v3.interfaces.transfer.devices import (
    DEVICE_ALGORITHM,
    DeviceAuthManager,
    DeviceCredentialStore,
    DeviceUnauthorizedError,
    build_device_signature_payload,
)
from allday_asr.v3.interfaces.transfer.pairing import (
    PAIRING_PROTOCOL,
    PAIRING_URI_PREFIX,
    build_pairing_uri,
)
from allday_asr.v3.interfaces.transfer.passkeys import RequestBinding
from allday_asr.v3.interfaces.transfer.server import create_transfer_server


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


@contextmanager
def _workspace_directory():
    path = Path(__file__).parent / f"device-auth-{uuid4().hex}"
    path.mkdir()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class DeviceAuthTests(unittest.TestCase):
    def test_server_accepts_silent_device_signature_and_rejects_replay(self) -> None:
        with _workspace_directory() as root:
            private_key = ec.generate_private_key(ec.SECP256R1())
            public_key = private_key.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            manager = DeviceAuthManager(DeviceCredentialStore(root / "devices.json"))
            record = manager.store.register(
                device_name="Harmony phone",
                algorithm=DEVICE_ALGORITHM,
                public_key=_b64(public_key),
                passkey_credential_id="passkey-id",
            )
            server = create_transfer_server(
                inbox=root / "inbox",
                host="127.0.0.1",
                port=0,
                token="pairing-code-123456789",
                allow_insecure_http=True,
                device_manager=manager,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.port}"
            binding = RequestBinding.for_request(
                method="GET",
                path="/api/v1/status",
                body=b"",
            )
            try:
                challenge_request = urllib.request.Request(
                    base_url + "/api/v1/devices/authenticate/challenge",
                    data=json.dumps(
                        {
                            "device_id": record.device_id,
                            "request": {
                                "method": binding.method,
                                "path": binding.path,
                                "body_sha256": binding.body_sha256,
                            },
                        }
                    ).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(challenge_request, timeout=3) as response:
                    challenge = json.loads(response.read())
                signature = private_key.sign(
                    build_device_signature_payload(
                        challenge_id=challenge["challenge_id"],
                        nonce=challenge["nonce"],
                        binding=binding,
                    ),
                    ec.ECDSA(hashes.SHA256()),
                )
                headers = {
                    "X-AllDay-Device-ID": record.device_id,
                    "X-AllDay-Device-Challenge": challenge["challenge_id"],
                    "X-AllDay-Device-Signature": _b64(signature),
                }
                status_request = urllib.request.Request(
                    base_url + "/api/v1/status",
                    headers=headers,
                )
                with urllib.request.urlopen(status_request, timeout=3) as response:
                    status = json.loads(response.read())
                self.assertEqual(status["version"], 2)
                self.assertEqual(status["authentication"], "device-signature")

                with self.assertRaises(urllib.error.HTTPError) as replay:
                    urllib.request.urlopen(status_request, timeout=3)
                self.assertEqual(replay.exception.code, HTTPStatus.UNAUTHORIZED)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_device_key_persists_and_authenticates_without_passkey(self) -> None:
        with _workspace_directory() as temporary:
            path = temporary / "devices.json"
            private_key = ec.generate_private_key(ec.SECP256R1())
            public_key = private_key.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            store = DeviceCredentialStore(path)
            record = store.register(
                device_name="Harmony phone",
                algorithm=DEVICE_ALGORITHM,
                public_key=_b64(public_key),
                passkey_credential_id="passkey-id",
            )
            restarted = DeviceAuthManager(DeviceCredentialStore(path))
            binding = RequestBinding.for_request(
                method="POST",
                path="/api/v1/uploads",
                body=b'{"kind":"recording"}',
            )
            challenge = restarted.start_authentication(
                device_id=record.device_id,
                binding=binding,
            )
            der_signature = private_key.sign(
                build_device_signature_payload(
                    challenge_id=challenge["challenge_id"],
                    nonce=challenge["nonce"],
                    binding=binding,
                ),
                ec.ECDSA(hashes.SHA256()),
            )
            signature_r, signature_s = decode_dss_signature(der_signature)
            signature = signature_r.to_bytes(32, "big") + signature_s.to_bytes(
                32, "big"
            )
            authenticated = restarted.verify_request(
                device_id=record.device_id,
                challenge_id=challenge["challenge_id"],
                encoded_signature=_b64(signature),
                binding=binding,
            )
            self.assertEqual(authenticated.device_name, "Harmony phone")
            self.assertIsNotNone(authenticated.last_used_at)
            self.assertNotIn("PRIVATE", path.read_text(encoding="utf-8"))

            with self.assertRaisesRegex(
                DeviceUnauthorizedError,
                "不存在、已使用或已过期",
            ):
                restarted.verify_request(
                    device_id=record.device_id,
                    challenge_id=challenge["challenge_id"],
                    encoded_signature=_b64(signature),
                    binding=binding,
                )

    def test_harmony_huks_public_material_is_normalized_to_spki(self) -> None:
        with _workspace_directory() as temporary:
            private_key = ec.generate_private_key(ec.SECP256R1())
            numbers = private_key.public_key().public_numbers()
            huks_public_material = struct.pack(
                "<IIIII",
                0,
                256,
                32,
                32,
                0,
            ) + numbers.x.to_bytes(32, "big") + numbers.y.to_bytes(32, "big")
            store = DeviceCredentialStore(temporary / "devices.json")
            record = store.register(
                device_name="Harmony HUKS phone",
                algorithm=DEVICE_ALGORITHM,
                public_key=_b64(huks_public_material),
                passkey_credential_id="passkey-id",
            )
            canonical = private_key.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            self.assertEqual(record.public_key, _b64(canonical))

    def test_device_signature_is_bound_to_request_body_and_offset(self) -> None:
        with _workspace_directory() as temporary:
            private_key = ec.generate_private_key(ec.SECP256R1())
            public_key = private_key.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            manager = DeviceAuthManager(
                DeviceCredentialStore(temporary / "devices.json")
            )
            record = manager.store.register(
                device_name="Harmony phone",
                algorithm=DEVICE_ALGORITHM,
                public_key=_b64(public_key),
                passkey_credential_id="passkey-id",
            )
            original = RequestBinding.for_request(
                method="PUT",
                path="/api/v1/uploads/" + "a" * 32,
                body=b"correct",
                upload_offset=10,
            )
            challenge = manager.start_authentication(
                device_id=record.device_id,
                binding=original,
            )
            signature = private_key.sign(
                build_device_signature_payload(
                    challenge_id=challenge["challenge_id"],
                    nonce=challenge["nonce"],
                    binding=original,
                ),
                ec.ECDSA(hashes.SHA256()),
            )
            tampered = RequestBinding.for_request(
                method="PUT",
                path=original.path,
                body=b"tampered",
                upload_offset=10,
            )
            with self.assertRaisesRegex(
                DeviceUnauthorizedError,
                "实际 HTTP 请求不匹配",
            ):
                manager.verify_request(
                    device_id=record.device_id,
                    challenge_id=challenge["challenge_id"],
                    encoded_signature=_b64(signature),
                    binding=tampered,
                )

    def test_pairing_qr_contains_one_complete_bootstrap_payload(self) -> None:
        uri = build_pairing_uri(
            receiver_id="ab" * 32,
            addresses=("https://192.168.1.20:8766",),
            ca_fingerprint=":".join(["AB"] * 32),
            ca_pem="-----BEGIN CERTIFICATE-----\nCA\n-----END CERTIFICATE-----\n",
            pairing_code="pairing-code-123456789",
        )
        self.assertTrue(uri.startswith(PAIRING_URI_PREFIX))
        encoded = uri.removeprefix(PAIRING_URI_PREFIX)
        payload = json.loads(
            base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        )
        self.assertEqual(payload["protocol"], PAIRING_PROTOCOL)
        self.assertEqual(payload["receiver_id"], "ab" * 32)
        self.assertEqual(payload["addresses"], ["https://192.168.1.20:8766"])
        self.assertEqual(payload["pairing_code"], "pairing-code-123456789")


if __name__ == "__main__":
    unittest.main()
