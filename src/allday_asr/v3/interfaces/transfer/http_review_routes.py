from __future__ import annotations

from http import HTTPStatus

from .passkeys import RequestBinding
from .protocol import (
    MAX_JSON_BODY_BYTES,
    _V3_REVIEW_ACTION_PATH,
    _V3_REVIEW_AUDIO_PATH,
    _V3_REVIEWS_PATH,
)
from .store import UploadStoreError


def dispatch_review_get(handler, path: str) -> bool:
    if path != _V3_REVIEWS_PATH:
        return False
    if handler.server.v3_gateway is None:
        handler._send_error(HTTPStatus.NOT_FOUND, "接口不存在")
        return True
    binding = RequestBinding.for_request(method="GET", path=path, body=b"")
    device = handler._authenticate_device_request(binding)
    handler._send_json(
        HTTPStatus.OK, handler.server.v3_gateway.reviews(device.device_id)
    )
    return True


def dispatch_review_post(handler, path: str) -> bool:
    if path not in {_V3_REVIEW_ACTION_PATH, _V3_REVIEW_AUDIO_PATH, "/device/v3/annotations"}:
        return False
    if handler.server.v3_gateway is None:
        handler._send_error(HTTPStatus.NOT_FOUND, "接口不存在")
        return True
    raw = handler._read_body(max_bytes=MAX_JSON_BODY_BYTES)
    binding = RequestBinding.for_request(method="POST", path=path, body=raw)
    device = handler._authenticate_device_request(binding)
    try:
        payload = handler._decode_json(raw)
        response = handler.server.v3_gateway.annotations(device.device_id, payload) if path == "/device/v3/annotations" else (
            handler.server.v3_gateway.resolve_review(device.device_id, payload)
            if path == _V3_REVIEW_ACTION_PATH
            else handler.server.v3_gateway.review_audio(device.device_id, payload)
        )
    except (KeyError, LookupError, ValueError) as exc:
        raise UploadStoreError(str(exc)) from exc
    handler._send_json(HTTPStatus.OK, response)
    return True


__all__ = ["dispatch_review_get", "dispatch_review_post"]
