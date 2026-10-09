"""Optional, authenticated build reports use the existing device signature binding."""
from http import HTTPStatus
from .passkeys import RequestBinding
from .store import UploadStoreError


def dispatch_diagnostic_post(handler, path):
    if path != "/device/v3/diagnostics":
        return False
    if handler.server.v3_gateway is None:
        handler._send_error(HTTPStatus.NOT_FOUND, "接口不存在")
        return True
    raw = handler._read_body(max_bytes=8192)
    device = handler._authenticate_device_request(RequestBinding.for_request(
        method="POST", path=path, body=raw))
    try:
        response = handler.server.v3_gateway.report_diagnostics(device.device_id, handler._decode_json(raw))
    except ValueError as exc:
        raise UploadStoreError(str(exc)) from exc
    handler._send_json(HTTPStatus.OK, response)
    return True
