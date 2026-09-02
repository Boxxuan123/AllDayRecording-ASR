from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

from .store import UploadRecord

PROTOCOL_NAME = "ALL_DAY_RECORDING_TRANSFER"
PROTOCOL_VERSION = 2
DEFAULT_TRANSFER_PORT = 8766
DEFAULT_PASSKEY_RP_ID = "alldayrecording.local"
DEFAULT_PASSKEY_ORIGIN = (
    "ohos:app-id:"
    "BOMBi4aSxrZhHIwywhkG+VaGo5UD0ztO9VcT8+KjMdXvlKRIwKISNRxKKfCF9zU9sT6QmwWXS/"
    "XSKT8hCt+x5hE"
)
MAX_JSON_BODY_BYTES = 64 * 1024
UploadCompletedCallback = Callable[
    [UploadRecord], Mapping[str, Any] | None
]
_UPLOAD_PATH = re.compile(r"^/api/v1/uploads/(?P<upload_id>[0-9a-f]{32})$")
_REGISTER_OPTIONS_PATH = "/api/v1/passkeys/register/options"
_REGISTER_VERIFY_PATH = "/api/v1/passkeys/register/verify"
_AUTHENTICATE_OPTIONS_PATH = "/api/v1/passkeys/authenticate/options"
_DEVICE_CHALLENGE_PATH = "/api/v1/devices/authenticate/challenge"
_V3_STATUS_PATH = "/device/v3/status"
_V3_SYNC_PATH = "/device/v3/sync"
_V3_REVIEWS_PATH = "/device/v3/reviews"
_V3_REVIEW_ACTION_PATH = "/device/v3/reviews/action"
_V3_REVIEW_AUDIO_PATH = "/device/v3/reviews/audio"
