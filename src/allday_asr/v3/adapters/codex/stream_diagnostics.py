"""Content-free streamed diagnostics; never accepts a partial answer as success."""

import json
import time
from pathlib import Path


class StreamDiagnostics:
    def __init__(self, path: Path):
        self.path = path
        self.start = time.monotonic()
        self.last_write = 0.0
        self.text = ""
        self.data = {
            "started_monotonic": self.start,
            "events": {},
            "usage": None,
            "agent_bytes": 0,
            "partial_json_valid": False,
            "turn_completed": False,
            "item_types": {},
        }

    def save(self):
        from .model_execution_runner import _atomic_json

        _atomic_json(self.path, self.data)
        self.last_write = time.monotonic()

    def observe(self, event):
        now = time.monotonic()
        method = event.method
        elapsed = round(now - self.start, 4)
        entry = self.data["events"].setdefault(method, {"count": 0, "first": elapsed})
        entry.update(count=entry["count"] + 1, last=elapsed)
        payload = event.payload
        if method in {"item/started", "item/completed"}:
            item = getattr(payload, "item", None)
            kind = getattr(getattr(item, "root", item), "type", "unknown")
            key = method + ":" + kind
            self.data["item_types"][key] = self.data["item_types"].get(key, 0) + 1
        if method == "item/agentMessage/delta":
            delta = getattr(payload, "delta", "")
            self.data["agent_bytes"] += len(delta.encode("utf-8"))
            if len(self.text) < 200000:
                self.text += delta
            try:
                json.loads(self.text)
                self.data["partial_json_valid"] = True
            except ValueError:
                self.data["partial_json_valid"] = False
        if method == "thread/tokenUsage/updated":
            usage = getattr(payload, "token_usage", None)
            if usage is not None:
                self.data["usage"] = usage.model_dump(by_alias=True, mode="json")
        if method == "turn/completed":
            self.data["turn_completed"] = True
            self.data["server_duration_ms"] = getattr(payload.turn, "duration_ms", None)
        if (
            entry["count"] == 1
            or now - self.last_write >= 0.25
            or method == "turn/completed"
        ):
            self.save()

    def consume(self, turn):
        # SDK is pinned to 0.160.0. Reuse its collector, including failed-turn handling.
        from openai_codex.api import _collect_turn_result

        self.data["turn_id"] = turn.id
        self.save()
        stream = turn.stream()

        def observed():
            for event in stream:
                self.observe(event)
                yield event

        wrapped = observed()
        try:
            return _collect_turn_result(wrapped, turn_id=turn.id)
        except BaseException as exc:
            self.data["error_type"] = type(exc).__name__
            raise
        finally:
            wrapped.close()
            stream.close()
            self.save()
