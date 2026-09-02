from __future__ import annotations

import base64
import hashlib
import json
import shutil
import ssl
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import contextmanager
from http import HTTPStatus
from pathlib import Path
from uuid import uuid4

import cbor2
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from allday_asr.v3.interfaces.transfer.passkeys import (
    PASSKEY_ASSERTION_HEADER,
    PASSKEY_CEREMONY_HEADER,
    PasskeyCredentialStore,
    PasskeyManager,
    RequestBinding,
    encode_assertion_header,
)
from allday_asr.v3.interfaces.transfer.server import (
    _automatic_workflow_startup_message,
    _prioritize_interface_addresses,
    create_transfer_server,
)
from allday_asr.v3.interfaces.transfer.store import (
    UploadConflictError,
    UploadDigestError,
    UploadOffsetError,
    UploadStore,
    UploadStoreError,
)
from allday_asr.v3.interfaces.transfer.tls import ensure_tls_identity


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class _SyntheticPasskey:
    def __init__(self) -> None:
        self.private_key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = b"allday-recording-test-passkey"
        self.user_id = b""
        self.sign_count = 0

    def registration_credential(
        self,
        public_key_options: dict,
        *,
        origin: str,
    ) -> dict:
        rp_id = public_key_options["rp"]["id"]
        self.user_id = _decode_base64url(public_key_options["user"]["id"])
        client_data = self._client_data(
            ceremony_type="webauthn.create",
            challenge=public_key_options["challenge"],
            origin=origin,
        )
        numbers = self.private_key.public_key().public_numbers()
        cose_public_key = cbor2.dumps(
            {
                1: 2,
                3: -7,
                -1: 1,
                -2: numbers.x.to_bytes(32, "big"),
                -3: numbers.y.to_bytes(32, "big"),
            }
        )
        authenticator_data = b"".join(
            (
                hashlib.sha256(rp_id.encode("utf-8")).digest(),
                bytes([0x45]),  # user present, user verified, attested credential data
                (0).to_bytes(4, "big"),
                bytes(16),
                len(self.credential_id).to_bytes(2, "big"),
                self.credential_id,
                cose_public_key,
            )
        )
        attestation_object = cbor2.dumps(
            {
                "fmt": "none",
                "attStmt": {},
                "authData": authenticator_data,
            }
        )
        credential_id = _base64url(self.credential_id)
        return {
            "id": credential_id,
            "rawId": credential_id,
            "type": "public-key",
            "authenticatorAttachment": "platform",
            "clientExtensionResults": {},
            "response": {
                "clientDataJSON": _base64url(client_data),
                "attestationObject": _base64url(attestation_object),
                "transports": ["internal"],
            },
        }

    def authentication_credential(
        self,
        public_key_options: dict,
        *,
        origin: str,
    ) -> dict:
        self.sign_count += 1
        rp_id = public_key_options["rpId"]
        client_data = self._client_data(
            ceremony_type="webauthn.get",
            challenge=public_key_options["challenge"],
            origin=origin,
        )
        authenticator_data = b"".join(
            (
                hashlib.sha256(rp_id.encode("utf-8")).digest(),
                bytes([0x05]),  # user present and user verified
                self.sign_count.to_bytes(4, "big"),
            )
        )
        signed = authenticator_data + hashlib.sha256(client_data).digest()
        signature = self.private_key.sign(signed, ec.ECDSA(hashes.SHA256()))
        credential_id = _base64url(self.credential_id)
        return {
            "id": credential_id,
            "rawId": credential_id,
            "type": "public-key",
            "authenticatorAttachment": "platform",
            "clientExtensionResults": {},
            "response": {
                "authenticatorData": _base64url(authenticator_data),
                "clientDataJSON": _base64url(client_data),
                "signature": _base64url(signature),
                "userHandle": _base64url(self.user_id),
            },
        }

    @staticmethod
    def _client_data(
        *,
        ceremony_type: str,
        challenge: str,
        origin: str,
    ) -> bytes:
        return json.dumps(
            {
                "type": ceremony_type,
                "challenge": challenge,
                "origin": origin,
                "crossOrigin": False,
            },
            separators=(",", ":"),
        ).encode("utf-8")


def _decode_base64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


@contextmanager
def _workspace_directory():
    path = Path(__file__).parent / f"transfer-{uuid4().hex}"
    path.mkdir()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class UploadStoreTests(unittest.TestCase):
    def test_upload_resumes_after_restart_and_finishes_without_overwrite(self) -> None:
        with _workspace_directory() as temporary:
            inbox = temporary / "inbox"
            content = b"complete recording bytes"
            store = UploadStore(inbox, max_chunk_bytes=64)
            record, created = store.create_upload(
                relative_path="pcm_session_123/segment_000001.wav",
                size=len(content),
                sha256=_digest(content),
                kind="recording",
            )
            self.assertTrue(created)
            first = store.append_chunk(
                record.upload_id,
                offset=0,
                data=content[:7],
            )
            self.assertEqual(first.offset, 7)

            restarted = UploadStore(inbox, max_chunk_bytes=64)
            resumed = restarted.get_upload(record.upload_id)
            self.assertEqual(resumed.offset, 7)
            completed = restarted.append_chunk(
                record.upload_id,
                offset=7,
                data=content[7:],
            )

            self.assertEqual(completed.status, "completed")
            self.assertEqual(completed.offset, len(content))
            destination = inbox / "pcm_session_123" / "segment_000001.wav"
            self.assertEqual(destination.read_bytes(), content)
            duplicate, duplicate_created = restarted.create_upload(
                relative_path="pcm_session_123/segment_000001.wav",
                size=len(content),
                sha256=_digest(content),
                kind="recording",
            )
            self.assertFalse(duplicate_created)
            self.assertEqual(duplicate.status, "completed")
            with self.assertRaises(UploadConflictError):
                restarted.append_chunk(
                    duplicate.upload_id,
                    offset=duplicate.size,
                    data=b"unexpected",
                )

            with self.assertRaises(UploadConflictError):
                restarted.create_upload(
                    relative_path="pcm_session_123/segment_000001.wav",
                    size=4,
                    sha256=_digest(b"evil"),
                    kind="recording",
                )
            self.assertEqual(destination.read_bytes(), content)
            destination.write_bytes(b"X" * len(content))
            with self.assertRaises(UploadConflictError):
                restarted.get_upload(record.upload_id)

    def test_invalid_paths_extensions_offsets_and_digests_are_rejected(self) -> None:
        with _workspace_directory() as temporary:
            store = UploadStore(temporary, max_chunk_bytes=64)
            invalid = (
                ("../escape.wav", "recording"),
                ("/absolute.wav", "recording"),
                ("folder\\windows.wav", "recording"),
                ("folder//audio.wav", "recording"),
                ("folder/./audio.wav", "recording"),
                (".uploads/state.json", "manifest"),
                ("folder/program.exe", "recording"),
                ("folder/audio.wav", "manifest"),
            )
            for relative_path, kind in invalid:
                with self.subTest(relative_path=relative_path), self.assertRaises(
                    UploadStoreError
                ):
                    store.create_upload(
                        relative_path=relative_path,
                        size=1,
                        sha256=_digest(b"x"),
                        kind=kind,
                    )

            content = b"correct"
            record, _ = store.create_upload(
                relative_path="session/segment.wav",
                size=len(content),
                sha256=_digest(content),
                kind="recording",
            )
            with self.assertRaises(UploadOffsetError) as offset_error:
                store.append_chunk(record.upload_id, offset=1, data=content)
            self.assertEqual(offset_error.exception.expected_offset, 0)

            wrong = b"incorre"
            with self.assertRaises(UploadDigestError):
                store.append_chunk(record.upload_id, offset=0, data=wrong)
            self.assertEqual(store.get_upload(record.upload_id).offset, 0)
            self.assertFalse((temporary / "session" / "segment.wav").exists())


class PasskeyRegistryTests(unittest.TestCase):
    def test_empty_registry_migrates_origin_but_enrolled_registry_refuses(self) -> None:
        with _workspace_directory() as temporary:
            path = temporary / "passkeys.json"
            first = PasskeyCredentialStore(
                path,
                rp_id="alldayrecording.local",
                expected_origins=("https://alldayrecording.local",),
            )
            original_user_id = first.user_id
            reopened = PasskeyCredentialStore(
                path,
                rp_id="alldayrecording.local",
                expected_origins=("https://alldayrecording.local",),
            )
            self.assertEqual(reopened.user_id, original_user_id)

            binding = RequestBinding.for_request(
                method="GET",
                path="/api/v1/status",
                body=b"",
            )
            with self.assertRaisesRegex(ValueError, "尚未登记 Passkey"):
                PasskeyManager(reopened).start_authentication(binding)

            migrated = PasskeyCredentialStore(
                path,
                rp_id="alldayrecording.local",
                expected_origins=("https://other.example",),
            )
            self.assertEqual(migrated.user_id, original_user_id)
            manager = PasskeyManager(migrated)
            passkey = _SyntheticPasskey()
            registration = manager.start_registration(device_name="Harmony phone")
            manager.finish_registration(
                ceremony_id=registration["ceremony_id"],
                credential=passkey.registration_credential(
                    registration["public_key"],
                    origin="https://other.example",
                ),
            )
            with self.assertRaisesRegex(ValueError, "RP ID 或 origin 不匹配"):
                PasskeyCredentialStore(
                    path,
                    rp_id="alldayrecording.local",
                    expected_origins=("https://third.example",),
                )

    def test_ohos_app_id_origin_is_accepted(self) -> None:
        with _workspace_directory() as temporary:
            origin = (
                "ohos:app-id:"
                "BOMBi4aSxrZhHIwywhkG+VaGo5UD0ztO9VcT8+KjMdXvlKRIwKISNRxKKfCF9zU9sT6QmwWXS/"
                "XSKT8hCt+x5hE"
            )
            store = PasskeyCredentialStore(
                temporary / "passkeys.json",
                rp_id="alldayrecording.local",
                expected_origins=(origin,),
            )
            self.assertEqual(store.expected_origins, (origin,))

    def test_registered_passkey_authenticates_after_backend_restart(self) -> None:
        with _workspace_directory() as temporary:
            path = temporary / "passkeys.json"
            origin = "https://alldayrecording.local"
            store = PasskeyCredentialStore(
                path,
                rp_id="alldayrecording.local",
                expected_origins=(origin,),
            )
            manager = PasskeyManager(store)
            passkey = _SyntheticPasskey()
            registration = manager.start_registration(device_name="Harmony phone")
            self.assertEqual(
                [
                    parameter["alg"]
                    for parameter in registration["public_key"]["pubKeyCredParams"]
                ],
                [-7],
            )
            manager.finish_registration(
                ceremony_id=registration["ceremony_id"],
                credential=passkey.registration_credential(
                    registration["public_key"],
                    origin=origin,
                ),
            )

            restarted = PasskeyManager(
                PasskeyCredentialStore(
                    path,
                    rp_id="alldayrecording.local",
                    expected_origins=(origin,),
                )
            )
            binding = RequestBinding.for_request(
                method="GET",
                path="/api/v1/status",
                body=b"",
            )
            authentication = restarted.start_authentication(binding)
            record = restarted.verify_request(
                ceremony_id=authentication["ceremony_id"],
                encoded_assertion=encode_assertion_header(
                    passkey.authentication_credential(
                        authentication["public_key"],
                        origin=origin,
                    )
                ),
                binding=binding,
            )
            self.assertEqual(record.device_name, "Harmony phone")
            self.assertEqual(record.sign_count, 1)


class TransferTLSIdentityTests(unittest.TestCase):
    def test_pairing_addresses_prefer_physical_lan_over_virtual_adapters(
        self,
    ) -> None:
        addresses = _prioritize_interface_addresses(
            [
                ("VMware Network Adapter VMnet8", "192.168.10.1"),
                ("FlClash", "198.18.0.1"),
                ("WLAN", "192.168.5.97"),
                ("Loopback", "127.0.0.1"),
            ]
        )

        self.assertEqual(addresses, ["192.168.5.97"])

    def test_pairing_addresses_keep_virtual_fallback_when_it_is_all_that_exists(
        self,
    ) -> None:
        addresses = _prioritize_interface_addresses(
            [("Tailscale", "100.64.0.2"), ("Loopback", "127.0.0.1")]
        )

        self.assertEqual(addresses, ["100.64.0.2"])

    def test_pairing_ca_stays_stable_when_wifi_address_changes(self) -> None:
        with _workspace_directory() as temporary:
            identity_root = temporary / "tls"
            first = ensure_tls_identity(
                identity_root,
                addresses=["192.168.10.20"],
            )
            first_ca = first.ca_certificate_path.read_bytes()
            first_leaf = first.certificate_path.read_bytes()

            second = ensure_tls_identity(
                identity_root,
                addresses=["10.0.0.55"],
            )
            second_certificate = x509.load_pem_x509_certificate(
                second.certificate_path.read_bytes()
            )
            alternative_names = second_certificate.extensions.get_extension_for_class(
                x509.SubjectAlternativeName
            ).value
            certificate_addresses = {
                str(value)
                for value in alternative_names.get_values_for_type(x509.IPAddress)
            }

            self.assertEqual(first.ca_sha256_fingerprint, second.ca_sha256_fingerprint)
            self.assertEqual(first_ca, second.ca_certificate_path.read_bytes())
            self.assertNotEqual(first_leaf, second.certificate_path.read_bytes())
            self.assertIn("10.0.0.55", certificate_addresses)
            self.assertNotIn("192.168.10.20", certificate_addresses)

    def test_server_rejects_plaintext_unless_debug_override_is_explicit(self) -> None:
        with _workspace_directory() as temporary:
            with self.assertRaisesRegex(ValueError, "拒绝明文 HTTP"):
                create_transfer_server(
                    inbox=temporary / "secure",
                    host="127.0.0.1",
                    port=0,
                    token="test-transfer-token-123",
                )
            with self.assertRaisesRegex(ValueError, "只允许绑定本机回环地址"):
                create_transfer_server(
                    inbox=temporary / "lan-debug",
                    host="0.0.0.0",
                    port=0,
                    token="test-transfer-token-123",
                    allow_insecure_http=True,
                )
            debug_server = create_transfer_server(
                inbox=temporary / "debug",
                host="127.0.0.1",
                port=0,
                token="test-transfer-token-123",
                allow_insecure_http=True,
            )
            try:
                self.assertFalse(debug_server.tls_enabled)
            finally:
                debug_server.server_close()

    def test_incomplete_pairing_identity_is_not_silently_replaced(self) -> None:
        with _workspace_directory() as temporary:
            identity_root = temporary / "tls"
            identity = ensure_tls_identity(identity_root, addresses=["127.0.0.1"])
            identity_root.joinpath("receiver-ca-key.pem").unlink()

            with self.assertRaisesRegex(ValueError, "配对身份不完整"):
                ensure_tls_identity(identity_root, addresses=["127.0.0.1"])
            self.assertTrue(identity.ca_certificate_path.is_file())


class TransferServerTests(unittest.TestCase):
    token = "test-transfer-token-123"
    origin = "https://alldayrecording.local"

    def test_automatic_workflow_startup_message_explains_admission_semantics(
        self,
    ) -> None:
        shadow = _automatic_workflow_startup_message(shadow=True)
        production = _automatic_workflow_startup_message(shadow=False)

        self.assertIn("shadow 非生产模式", shadow)
        self.assertIn("允许模型执行", shadow)
        self.assertIn("不会解除独立备份准入阻塞", shadow)
        self.assertIn("production", production)
        self.assertIn("独立备份写入与回读校验通过后执行", production)

    def test_passkey_https_protocol_uploads_and_resumes(self) -> None:
        with _workspace_directory() as temporary:
            inbox = temporary / "inbox"
            passkey_state = temporary / "passkeys.json"
            completed_manifests: list[str] = []

            def upload_completed(record):
                if record.kind != "manifest":
                    return None
                completed_manifests.append(record.upload_id)
                return {"status": "queued"}

            identity = ensure_tls_identity(
                temporary / "tls",
                addresses=["127.0.0.1"],
            )
            server = create_transfer_server(
                inbox=inbox,
                host="127.0.0.1",
                port=0,
                token=self.token,
                max_chunk_bytes=16,
                tls_cert=identity.certificate_path,
                tls_key=identity.private_key_path,
                passkey_state=passkey_state,
                passkey_origins=(self.origin,),
                upload_completed=upload_completed,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"https://127.0.0.1:{server.port}"
            tls_context = ssl.create_default_context(
                cafile=str(identity.ca_certificate_path)
            )
            passkey = _SyntheticPasskey()
            try:
                with self.assertRaises(urllib.error.HTTPError) as bearer_denied:
                    self._request(
                        f"{base_url}/api/v1/status",
                        headers={"Authorization": f"Bearer {self.token}"},
                        context=tls_context,
                    )
                self.assertEqual(bearer_denied.exception.code, HTTPStatus.UNAUTHORIZED)

                self._register_passkey(
                    base_url,
                    context=tls_context,
                    passkey=passkey,
                )

                status_headers = self._passkey_headers(
                    base_url,
                    method="GET",
                    path="/api/v1/status",
                    body=b"",
                    context=tls_context,
                    passkey=passkey,
                )
                status, _, status_body = self._request(
                    f"{base_url}/api/v1/status",
                    headers=status_headers,
                    context=tls_context,
                )
                self.assertEqual(status, HTTPStatus.OK)
                self.assertEqual(status_body["status"], "ready")
                self.assertEqual(status_body["version"], 2)
                self.assertEqual(status_body["transport"], "tls")
                self.assertEqual(status_body["authentication"], "device-signature")

                with self.assertRaises(urllib.error.HTTPError) as replayed:
                    self._request(
                        f"{base_url}/api/v1/status",
                        headers=status_headers,
                        context=tls_context,
                    )
                self.assertEqual(replayed.exception.code, HTTPStatus.UNAUTHORIZED)

                content = b"phone audio payload"
                metadata = {
                    "relative_path": "pcm_session_456/segment_000002.wav",
                    "size": len(content),
                    "sha256": _digest(content),
                    "kind": "recording",
                }
                metadata_bytes = json.dumps(metadata, separators=(",", ":")).encode()
                tampered_headers = self._passkey_headers(
                    base_url,
                    method="POST",
                    path="/api/v1/uploads",
                    body=metadata_bytes,
                    context=tls_context,
                    passkey=passkey,
                )
                with self.assertRaises(urllib.error.HTTPError) as tampered:
                    self._request(
                        f"{base_url}/api/v1/uploads",
                        method="POST",
                        data=metadata_bytes + b" ",
                        headers=tampered_headers,
                        context=tls_context,
                    )
                self.assertEqual(tampered.exception.code, HTTPStatus.UNAUTHORIZED)

                status, create_headers, create_body = self._passkey_request(
                    base_url,
                    method="POST",
                    path="/api/v1/uploads",
                    data=metadata_bytes,
                    context=tls_context,
                    passkey=passkey,
                )
                self.assertEqual(status, HTTPStatus.CREATED)
                upload = create_body["upload"]
                upload_id = upload["upload_id"]
                self.assertEqual(create_headers["Upload-Offset"], "0")

                upload_path = f"/api/v1/uploads/{upload_id}"
                status, _, first_body = self._passkey_request(
                    base_url,
                    method="PUT",
                    path=upload_path,
                    data=content[:10],
                    upload_offset=0,
                    context=tls_context,
                    passkey=passkey,
                )
                self.assertEqual(status, HTTPStatus.OK)
                self.assertEqual(first_body["upload"]["offset"], 10)

                status, _, query_body = self._passkey_request(
                    base_url,
                    method="GET",
                    path=upload_path,
                    context=tls_context,
                    passkey=passkey,
                )
                self.assertEqual(status, HTTPStatus.OK)
                self.assertEqual(query_body["upload"]["offset"], 10)

                with self.assertRaises(urllib.error.HTTPError) as conflict:
                    self._passkey_request(
                        base_url,
                        method="PUT",
                        path=upload_path,
                        data=b"x",
                        upload_offset=0,
                        context=tls_context,
                        passkey=passkey,
                    )
                self.assertEqual(conflict.exception.code, HTTPStatus.CONFLICT)
                self.assertEqual(conflict.exception.headers["Upload-Offset"], "10")

                status, final_headers, final_body = self._passkey_request(
                    base_url,
                    method="PUT",
                    path=upload_path,
                    data=content[10:],
                    upload_offset=10,
                    context=tls_context,
                    passkey=passkey,
                )
                self.assertEqual(status, HTTPStatus.OK)
                self.assertEqual(final_headers["Upload-Offset"], str(len(content)))
                self.assertEqual(final_body["upload"]["status"], "completed")
                self.assertEqual(
                    (inbox / "pcm_session_456" / "segment_000002.wav").read_bytes(),
                    content,
                )

                status, _, duplicate = self._passkey_request(
                    base_url,
                    method="POST",
                    path="/api/v1/uploads",
                    data=metadata_bytes,
                    context=tls_context,
                    passkey=passkey,
                )
                self.assertEqual(status, HTTPStatus.OK)
                self.assertEqual(duplicate["upload"]["status"], "completed")

                manifest = b"{}"
                manifest_metadata = {
                    "relative_path": (
                        "pcm_session_456/session_summary.json"
                    ),
                    "size": len(manifest),
                    "sha256": _digest(manifest),
                    "kind": "manifest",
                }
                manifest_metadata_bytes = json.dumps(
                    manifest_metadata,
                    separators=(",", ":"),
                ).encode()
                _, _, manifest_create = self._passkey_request(
                    base_url,
                    method="POST",
                    path="/api/v1/uploads",
                    data=manifest_metadata_bytes,
                    context=tls_context,
                    passkey=passkey,
                )
                manifest_id = manifest_create["upload"]["upload_id"]
                _, _, manifest_complete = self._passkey_request(
                    base_url,
                    method="PUT",
                    path=f"/api/v1/uploads/{manifest_id}",
                    data=manifest,
                    upload_offset=0,
                    context=tls_context,
                    passkey=passkey,
                )
                self.assertEqual(manifest_complete["upload"]["status"], "completed")
                self.assertEqual(
                    manifest_complete["automation"]["status"],
                    "queued",
                )
                self.assertEqual(completed_manifests, [manifest_id])

                registry = PasskeyCredentialStore(
                    passkey_state,
                    rp_id="alldayrecording.local",
                    expected_origins=(self.origin,),
                )
                records = registry.list_credentials()
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0].sign_count, passkey.sign_count)
                self.assertNotIn("private", passkey_state.read_text(encoding="utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def _register_passkey(
        self,
        base_url: str,
        *,
        context: ssl.SSLContext,
        passkey: _SyntheticPasskey,
    ) -> None:
        pairing_headers = {"Authorization": f"Bearer {self.token}"}
        _, _, options = self._request(
            f"{base_url}/api/v1/passkeys/register/options",
            method="POST",
            data=json.dumps({"device_name": "Harmony phone"}).encode(),
            headers=pairing_headers,
            context=context,
        )
        credential = passkey.registration_credential(
            options["public_key"],
            origin=self.origin,
        )
        status, _, _ = self._request(
            f"{base_url}/api/v1/passkeys/register/verify",
            method="POST",
            data=json.dumps(
                {
                    "ceremony_id": options["ceremony_id"],
                    "credential": credential,
                }
            ).encode(),
            headers=pairing_headers,
            context=context,
        )
        self.assertEqual(status, HTTPStatus.CREATED)

    def _passkey_headers(
        self,
        base_url: str,
        *,
        method: str,
        path: str,
        body: bytes,
        context: ssl.SSLContext,
        passkey: _SyntheticPasskey,
        upload_offset: int | None = None,
    ) -> dict[str, str]:
        binding = {
            "method": method,
            "path": path,
            "body_sha256": _digest(body),
        }
        if upload_offset is not None:
            binding["upload_offset"] = upload_offset
        _, _, options = self._request(
            f"{base_url}/api/v1/passkeys/authenticate/options",
            method="POST",
            data=json.dumps({"request": binding}).encode(),
            context=context,
        )
        assertion = passkey.authentication_credential(
            options["public_key"],
            origin=self.origin,
        )
        return {
            PASSKEY_CEREMONY_HEADER: options["ceremony_id"],
            PASSKEY_ASSERTION_HEADER: encode_assertion_header(assertion),
        }

    def _passkey_request(
        self,
        base_url: str,
        *,
        method: str,
        path: str,
        context: ssl.SSLContext,
        passkey: _SyntheticPasskey,
        data: bytes | None = None,
        upload_offset: int | None = None,
    ):
        body = data or b""
        headers = self._passkey_headers(
            base_url,
            method=method,
            path=path,
            body=body,
            upload_offset=upload_offset,
            context=context,
            passkey=passkey,
        )
        if upload_offset is not None:
            headers["Upload-Offset"] = str(upload_offset)
        return self._request(
            f"{base_url}{path}",
            method=method,
            data=data,
            headers=headers,
            context=context,
        )

    def _request(
        self,
        url: str,
        *,
        method: str = "GET",
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        context: ssl.SSLContext | None = None,
    ):
        request = urllib.request.Request(
            url,
            data=data,
            headers=headers or {},
            method=method,
        )
        response = urllib.request.urlopen(request, timeout=3, context=context)
        with response:
            return (
                response.status,
                response.headers,
                json.loads(response.read()),
            )


if __name__ == "__main__":
    unittest.main()
