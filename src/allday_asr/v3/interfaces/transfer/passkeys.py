"""Public facade for Passkey request binding, storage and ceremonies."""

from .passkey_manager import PasskeyManager
from .passkey_store import PasskeyCredentialStore
from .passkey_types import (
    EMPTY_BODY_SHA256,
    PASSKEY_ASSERTION_HEADER,
    PASSKEY_CEREMONY_HEADER,
    PasskeyConflictError,
    PasskeyCredentialRecord,
    PasskeyError,
    PasskeyUnauthorizedError,
    RequestBinding,
    encode_assertion_header,
)

__all__ = [
    "EMPTY_BODY_SHA256",
    "PASSKEY_ASSERTION_HEADER",
    "PASSKEY_CEREMONY_HEADER",
    "PasskeyConflictError",
    "PasskeyCredentialRecord",
    "PasskeyCredentialStore",
    "PasskeyError",
    "PasskeyManager",
    "PasskeyUnauthorizedError",
    "RequestBinding",
    "encode_assertion_header",
]
