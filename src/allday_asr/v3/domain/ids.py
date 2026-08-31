from __future__ import annotations

import hashlib
import secrets
import time


_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid(
    *, timestamp_ms: int | None = None, randomness: bytes | None = None
) -> str:
    """Create a sortable ULID without adding a runtime dependency."""
    millis = int(time.time() * 1000) if timestamp_ms is None else timestamp_ms
    if not 0 <= millis < 2**48:
        raise ValueError("ULID timestamp must fit in 48 bits")
    random_bytes = secrets.token_bytes(10) if randomness is None else randomness
    if len(random_bytes) != 10:
        raise ValueError("ULID randomness must contain 10 bytes")
    return _encode((millis << 80) | int.from_bytes(random_bytes, "big"))


def stable_ulid(*parts: object) -> str:
    """Derive one deterministic ULID-shaped identifier for legacy imports."""
    payload = "\0".join(str(part) for part in parts).encode("utf-8")
    value = int.from_bytes(hashlib.sha256(payload).digest()[:16], "big")
    return _encode(value)


def _encode(value: int) -> str:
    if not 0 <= value < 2**128:
        raise ValueError("ULID value must fit in 128 bits")
    characters = ["0"] * 26
    for index in range(25, -1, -1):
        characters[index] = _CROCKFORD[value & 31]
        value >>= 5
    return "".join(characters)


__all__ = ["new_ulid", "stable_ulid"]
