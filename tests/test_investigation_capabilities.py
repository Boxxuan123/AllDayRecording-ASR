"""Execution-boundary probes: local synthetic streams only, no live inference."""

import io
from http.client import RemoteDisconnected
import json
import urllib.request
import urllib.error
import pytest
from allday_asr.v3.adapters.codex.investigation_policy import (
    request_body,
    validate_event,
)
from allday_asr.v3.adapters.codex.investigation_gateway import InvestigationGateway
from allday_asr.v3.application.recall_investigation_tools import InvestigationTools


def request():
    return {
        "task": "history-investigation-v1",
        "model": "gpt-6.1-sol",
        "effort": "low",
        "prompt": json.dumps({"records": [{"quote": "fictional only"}]}),
        "instructions": "frozen protocol",
        "base_instructions": "bounded investigator",
        "schema": {"type": "object", "properties": {}, "additionalProperties": False},
    }


def sse(*events):
    return io.BytesIO(
        b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events)
    )


def post(gateway, payload):
    req = urllib.request.Request(
        gateway.base_url + "/responses",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    return urllib.request.urlopen(req, timeout=3).read()


def test_outbound_exact_input_and_zero_native_tools(tmp_path):
    seen = []

    def upstream(headers, data):
        seen.append(json.loads(data))
        return sse(
            {"type": "response.completed", "response": {"output": [], "tools": []}}
        )

    with InvestigationGateway(
        request(), tmp_path / "receipt.json", transport=upstream
    ) as gate:
        post(
            gate,
            {
                "model": "gpt-6.1-sol",
                "input": [{"text": "PRIVATE_FILE_CANARY"}],
                "tools": [{"type": "web_search"}],
                "instructions": "ambient secret",
            },
        )
        with pytest.raises(urllib.error.HTTPError, match="409"):
            post(gate, {"model": "gpt-6.1-sol"})
    assert len(seen) == 1 and seen[0] == request_body(request())
    assert seen[0]["tools"] == [] and seen[0]["tool_choice"] == "none"
    assert "PRIVATE_FILE_CANARY" not in json.dumps(seen)
    assert "ambient secret" not in json.dumps(seen)


@pytest.mark.parametrize(
    "kind",
    [
        "function_call",
        "custom_tool_call",
        "local_shell_call",
        "computer_call",
        "web_search_call",
        "image_generation_call",
        "mcp_call",
        "future_unknown_tool",
    ],
)
def test_tool_event_rejected_before_forwarding(tmp_path, kind):
    event = {
        "type": "response.output_item.added",
        "item": {"type": kind, "name": "exec_command", "arguments": "READ_OUTSIDE"},
    }
    with pytest.raises(ValueError):
        validate_event(event)

    def upstream(*args):
        return sse(event)

    with InvestigationGateway(
        request(), tmp_path / "receipt.json", transport=upstream
    ) as gate:
        assert post(gate, {"model": "gpt-6.1-sol"}) == b""
    data = json.loads((tmp_path / "receipt.json").read_text())
    assert data["blocked_events"] == 1 and data["validated_events"] == 0
    assert "READ_OUTSIDE" not in (tmp_path / "receipt.json").read_text()


def test_completed_output_and_unknown_events_fail_closed():
    for event in [
        {
            "type": "response.completed",
            "response": {"output": [{"type": "function_call"}]},
        },
        {"type": "response.future_tool.delta", "delta": "x"},
        {"type": "response.created", "response": {"tools": [{"type": "web_search"}]}},
        {
            "type": "response.content_part.added",
            "part": {"type": "image", "url": "file:///private"},
        },
    ]:
        with pytest.raises(ValueError):
            validate_event(event)


def test_business_allowlist_precedes_even_target_resolution():
    tools = object.__new__(InvestigationTools)
    for name in ["exec_command", "read_file", "https://outside", "delete_message"]:
        with pytest.raises(ValueError, match="allowlist"):
            tools.run({"tool": name}, [])


def test_model_mismatch_never_reaches_upstream(tmp_path):
    seen = []

    def upstream(*args):
        seen.append(1)
        raise AssertionError()

    with InvestigationGateway(
        request(), tmp_path / "receipt.json", transport=upstream
    ) as gate:
        with pytest.raises(RemoteDisconnected):
            post(gate, {"model": "different-model"})
    assert not seen
