"""One-shot loopback capability gate, owned by the same killable SDK worker.

SDK credentials stay in memory and are forwarded only to the fixed original
OpenAI endpoint. Neither model text nor SDK-supplied URLs select a destination.
"""

import json
import secrets
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from .investigation_policy import request_body, validate_event, digest, POLICY_VERSION

UPSTREAM = "https://chatgpt.com/backend-api/codex/responses"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("model endpoint redirect refused")


def forward(headers, body):
    allowed = {
        "authorization",
        "chatgpt-account-id",
        "openai-beta",
        "originator",
        "user-agent",
    }
    filtered = {k: v for k, v in headers.items() if k.lower() in allowed}
    filtered.update({"Content-Type": "application/json", "Accept": "text/event-stream"})
    opener = urllib.request.build_opener(NoRedirect())
    return opener.open(
        urllib.request.Request(UPSTREAM, data=body, headers=filtered), timeout=25
    )


class InvestigationGateway:
    def __init__(self, request, receipt_path, *, transport=forward):
        self.body = request_body(request)
        self.receipt_path = receipt_path
        self.transport = transport
        self.lock = threading.Lock()
        self.used = False
        self.started = time.monotonic()
        self.state = {
            "policy": POLICY_VERSION,
            "native_tools": [],
            "tool_choice": "none",
            "request_sha256": digest(self.body),
            "upstream_requests": 0,
            "validated_events": 0,
            "blocked_events": 0,
            "upstream": UPSTREAM,
        }
        token = secrets.token_urlsafe(32)
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # Never log headers, tokens, prompts, or model output.

            def do_GET(self):
                self.send_error(405)

            def do_POST(self):
                if self.path != "/" + token + "/responses":
                    self.send_error(403)
                    return
                with gateway.lock:
                    if gateway.used:
                        self.send_error(
                            409, "model call already reserved; retries prohibited"
                        )
                        return
                    gateway.used = True
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 2_000_000 or self.headers.get(
                        "Content-Encoding"
                    ):
                        raise ValueError("invalid SDK request encoding")
                    incoming = json.loads(self.rfile.read(size))
                    if incoming.get("model") != gateway.body["model"]:
                        raise ValueError("model changed before dispatch")
                    # Deliberately discard ALL SDK input/tools/instructions. Only the
                    # caller's frozen, source-authorized request crosses this boundary.
                    body = json.dumps(gateway.body, ensure_ascii=False).encode()
                    gateway.state["upstream_requests"] += 1
                    gateway.state["dispatch_seconds"] = round(
                        time.monotonic() - gateway.started, 4
                    )
                    gateway.save()
                    with gateway.transport(dict(self.headers), body) as upstream:
                        gateway.state["headers_seconds"] = round(
                            time.monotonic() - gateway.started, 4
                        )
                        gateway.save()
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.send_header("Connection", "close")
                        self.end_headers()
                        pending = []
                        total = 0
                        for line in upstream:
                            total += len(line)
                            if total > 4_000_000:
                                raise ValueError("model stream size exceeded")
                            if line.strip():
                                pending.append(line)
                                continue
                            data = b"\n".join(
                                x[5:].strip() for x in pending if x.startswith(b"data:")
                            )
                            pending = []
                            if not data:
                                continue
                            if data == b"[DONE]":
                                break
                            event = validate_event(json.loads(data))
                            gateway.state["validated_events"] += 1
                            if "first_event_seconds" not in gateway.state:
                                gateway.state["first_event_seconds"] = round(
                                    time.monotonic() - gateway.started, 4
                                )
                                gateway.save()
                            if event["type"] == "response.completed":
                                gateway.state["completed"] = True
                                gateway.state["completed_seconds"] = round(
                                    time.monotonic() - gateway.started, 4
                                )
                                usage = event.get("response", {}).get("usage")
                                if isinstance(usage, dict):
                                    gateway.state["usage"] = {
                                        k: usage[k]
                                        for k in (
                                            "input_tokens",
                                            "output_tokens",
                                            "total_tokens",
                                        )
                                        if isinstance(usage.get(k), int)
                                    }
                                gateway.save()
                            # Validation happens BEFORE any byte of this event reaches
                            # the SDK, including output_item.added and final outputs.
                            self.wfile.write(
                                b"data: " + json.dumps(event).encode() + b"\n\n"
                            )
                            self.wfile.flush()
                        if pending or not gateway.state.get("completed"):
                            raise ValueError("incomplete model stream")
                except Exception as exc:
                    gateway.state["blocked_events"] += 1
                    gateway.state["error_type"] = type(exc).__name__
                    if isinstance(exc, urllib.error.HTTPError):
                        gateway.state["http_status"] = exc.code
                    self.close_connection = True
                finally:
                    gateway.save()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.base_url = f"http://127.0.0.1:{self.server.server_port}/{token}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def save(self):
        from .model_execution_runner import _atomic_json

        _atomic_json(self.receipt_path, self.state)

    def __enter__(self):
        self.save()
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        self.save()

    def overrides(self):
        return [
            'model_provider="investigation_gate"',
            'model_providers.investigation_gate.name="Bounded investigation gateway"',
            "model_providers.investigation_gate.base_url=" + json.dumps(self.base_url),
            'model_providers.investigation_gate.wire_api="responses"',
            "model_providers.investigation_gate.requires_openai_auth=true",
            "model_providers.investigation_gate.supports_websockets=false",
            "model_providers.investigation_gate.request_max_retries=0",
            "model_providers.investigation_gate.stream_max_retries=0",
            "features.enable_request_compression=false",
            "features.hooks=false",
            'web_search="disabled"',
            "features.image_generation=false",
            "features.remote_plugin=false",
            "features.browser_use=false",
            "features.computer_use=false",
            "features.view_image=false",
        ]
