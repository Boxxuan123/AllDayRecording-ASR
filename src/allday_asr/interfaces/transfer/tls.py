from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


CA_CERTIFICATE_FILE = "receiver-ca-cert.pem"
CA_PRIVATE_KEY_FILE = "receiver-ca-key.pem"
SERVER_CERTIFICATE_FILE = "receiver-cert.pem"
SERVER_PRIVATE_KEY_FILE = "receiver-key.pem"


@dataclass(frozen=True)
class TransferTLSIdentity:
    ca_certificate_path: Path
    certificate_path: Path
    private_key_path: Path
    ca_sha256_fingerprint: str


def ensure_tls_identity(
    identity_dir: Path,
    *,
    addresses: Iterable[str],
) -> TransferTLSIdentity:
    """Keep one pairing CA and issue a fresh leaf for the current LAN addresses."""
    root = identity_dir.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    ca_certificate_path = root / CA_CERTIFICATE_FILE
    ca_private_key_path = root / CA_PRIVATE_KEY_FILE
    certificate_path = root / SERVER_CERTIFICATE_FILE
    private_key_path = root / SERVER_PRIVATE_KEY_FILE

    ca_certificate, ca_private_key = _load_or_create_ca(
        ca_certificate_path,
        ca_private_key_path,
    )
    certificate, private_key = _issue_server_certificate(
        ca_certificate,
        ca_private_key,
        addresses=addresses,
    )
    _atomic_write(
        certificate_path,
        certificate.public_bytes(serialization.Encoding.PEM),
        private=False,
    )
    _atomic_write(
        private_key_path,
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        private=True,
    )
    return TransferTLSIdentity(
        ca_certificate_path=ca_certificate_path,
        certificate_path=certificate_path,
        private_key_path=private_key_path,
        ca_sha256_fingerprint=certificate_sha256_fingerprint(ca_certificate),
    )


def certificate_sha256_fingerprint(certificate: x509.Certificate | Path) -> str:
    resolved = (
        x509.load_pem_x509_certificate(certificate.read_bytes())
        if isinstance(certificate, Path)
        else certificate
    )
    digest = resolved.fingerprint(hashes.SHA256()).hex().upper()
    return ":".join(digest[index : index + 2] for index in range(0, len(digest), 2))


def _load_or_create_ca(
    certificate_path: Path,
    private_key_path: Path,
) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    certificate_exists = certificate_path.exists()
    private_key_exists = private_key_path.exists()
    if certificate_exists != private_key_exists:
        raise ValueError(
            "TLS 配对身份不完整；为避免静默更换身份，服务已停止。"
            f"请检查 {certificate_path.parent}"
        )
    if certificate_exists:
        try:
            certificate = x509.load_pem_x509_certificate(
                certificate_path.read_bytes()
            )
            private_key = serialization.load_pem_private_key(
                private_key_path.read_bytes(),
                password=None,
            )
        except (OSError, ValueError) as exc:
            raise ValueError("TLS 配对身份无法读取，服务不会自动覆盖") from exc
        if not isinstance(private_key, ec.EllipticCurvePrivateKey):
            raise ValueError("TLS 配对私钥类型无效")
        _verify_key_matches(certificate, private_key, label="TLS 配对 CA")
        basic_constraints = _extension(
            certificate,
            x509.BasicConstraints,
            "TLS 配对 CA 缺少 BasicConstraints",
        )
        if not basic_constraints.ca:
            raise ValueError("TLS 配对证书不是 CA")
        if certificate.not_valid_after_utc <= datetime.now(timezone.utc):
            raise ValueError("TLS 配对 CA 已过期；必须明确重新配对，服务不会静默轮换")
        return certificate, private_key

    private_key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    subject = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "AllDayRecording Local Pairing CA")]
    )
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=0),
            critical=True,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(private_key.public_key()),
            critical=False,
        )
        .sign(private_key, hashes.SHA256())
    )
    _atomic_write(
        certificate_path,
        certificate.public_bytes(serialization.Encoding.PEM),
        private=False,
    )
    _atomic_write(
        private_key_path,
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        private=True,
    )
    return certificate, private_key


def _issue_server_certificate(
    ca_certificate: x509.Certificate,
    ca_private_key: ec.EllipticCurvePrivateKey,
    *,
    addresses: Iterable[str],
) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    private_key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    alternative_names: list[x509.GeneralName] = [
        x509.DNSName("alldayrecording.local"),
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
    ]
    seen = {"127.0.0.1"}
    for value in addresses:
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            if value and value not in {"0.0.0.0", "::"}:
                alternative_names.append(x509.DNSName(value))
            continue
        canonical = str(address)
        if address.is_unspecified or canonical in seen:
            continue
        seen.add(canonical)
        alternative_names.append(x509.IPAddress(address))

    certificate = (
        x509.CertificateBuilder()
        .subject_name(
            x509.Name(
                [
                    x509.NameAttribute(
                        NameOID.COMMON_NAME,
                        "AllDayRecording Receiver",
                    )
                ]
            )
        )
        .issuer_name(ca_certificate.subject)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=397))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=True,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .add_extension(x509.SubjectAlternativeName(alternative_names), False)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(private_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(
                ca_private_key.public_key()
            ),
            critical=False,
        )
        .sign(ca_private_key, hashes.SHA256())
    )
    return certificate, private_key


def _verify_key_matches(
    certificate: x509.Certificate,
    private_key: ec.EllipticCurvePrivateKey,
    *,
    label: str,
) -> None:
    certificate_public = certificate.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    private_public = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    if certificate_public != private_public:
        raise ValueError(f"{label} 证书和私钥不匹配")


def _extension(certificate, extension_type, message):
    try:
        return certificate.extensions.get_extension_for_class(extension_type).value
    except x509.ExtensionNotFound as exc:
        raise ValueError(message) from exc


def _atomic_write(path: Path, data: bytes, *, private: bool) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    descriptor = os.open(temporary, flags, 0o600 if private else 0o644)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if private:
            path.chmod(0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
