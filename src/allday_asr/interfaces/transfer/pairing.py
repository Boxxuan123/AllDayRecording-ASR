from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any, Iterable


PAIRING_PROTOCOL = "ALL_DAY_RECORDING_PAIRING"
PAIRING_VERSION = 1
PAIRING_URI_PREFIX = "alldayrecording://pair/"


def build_pairing_uri(
    *,
    receiver_id: str,
    addresses: Iterable[str],
    ca_fingerprint: str,
    ca_pem: str,
    pairing_code: str,
) -> str:
    payload: dict[str, Any] = {
        "protocol": PAIRING_PROTOCOL,
        "version": PAIRING_VERSION,
        "receiver_id": receiver_id,
        "addresses": list(dict.fromkeys(addresses)),
        "ca_fingerprint": ca_fingerprint,
        "ca_pem": ca_pem,
        "pairing_code": pairing_code,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).rstrip(b"=")
    return PAIRING_URI_PREFIX + encoded.decode("ascii")


def write_pairing_qr(path: Path, pairing_uri: str) -> Path:
    try:
        import qrcode
        from qrcode.constants import ERROR_CORRECT_M
    except ImportError as exc:
        raise RuntimeError("缺少 qrcode 依赖，无法生成首次配对二维码") from exc
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    temporary = resolved.with_name(f".{resolved.name}.tmp")
    code = qrcode.QRCode(
        version=None,
        error_correction=ERROR_CORRECT_M,
        box_size=8,
        border=4,
    )
    code.add_data(pairing_uri)
    code.make(fit=True)
    image = code.make_image(fill_color="black", back_color="white")
    image.save(temporary, format="PNG")
    os.replace(temporary, resolved)
    return resolved
