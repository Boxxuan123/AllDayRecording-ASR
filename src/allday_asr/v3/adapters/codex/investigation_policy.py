"""Fail-closed model boundary: no native tools and no ambient SDK context."""

import hashlib
import json

POLICY_VERSION = "investigation-no-native-tools-v2"
EVENTS = {
    "response.created",
    "response.in_progress",
    "response.queued",
    "response.output_item.added",
    "response.output_item.done",
    "response.content_part.added",
    "response.content_part.done",
    "response.output_text.delta",
    "response.output_text.done",
    "response.refusal.delta",
    "response.refusal.done",
    "response.reasoning_summary_part.added",
    "response.reasoning_summary_part.done",
    "response.reasoning_summary_text.delta",
    "response.reasoning_summary_text.done",
    "response.reasoning_text.delta",
    "response.reasoning_text.done",
    "response.completed",
    "response.failed",
    "response.incomplete",
    "error",
}
ITEMS = {"message", "reasoning"}
PARTS = {"output_text", "refusal", "summary_text", "reasoning_text"}


def request_body(request):
    if (
        request.get("task") != "history-investigation-v1"
        or request.get("effort") != "low"
    ):
        raise ValueError("unapproved investigation request")
    payload = json.loads(request["prompt"])
    if not isinstance(payload, dict):
        raise ValueError("investigation input must be a data object")
    return {
        "model": request["model"],
        "instructions": request["base_instructions"] + "\n" + request["instructions"],
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": request["prompt"]}],
            }
        ],
        "tools": [],
        "tool_choice": "none",
        "parallel_tool_calls": False,
        "reasoning": {"effort": request["effort"]},
        "text": {
            "format": {
                "type": "json_schema",
                "name": "history_investigation",
                "strict": True,
                "schema": request["schema"],
            }
        },
        "stream": True,
        "store": False,
    }


def validate_event(event):
    if not isinstance(event, dict) or event.get("type") not in EVENTS:
        raise ValueError("unapproved response event")
    items = []
    if "item" in event:
        items.append(event["item"])
    if "response" in event:
        response = event["response"]
        if not isinstance(response, dict) or response.get("tools", []) != []:
            raise ValueError("response introduces tools")
        items.extend(response.get("output", []))
    for item in items:
        if not isinstance(item, dict) or item.get("type") not in ITEMS:
            raise ValueError(
                "native tool or unknown output blocked before SDK dispatch"
            )
        for part in item.get("content", []):
            if part.get("type") not in PARTS:
                raise ValueError("nontext output blocked")
    if "part" in event and event["part"].get("type") not in PARTS:
        raise ValueError("unapproved content part")
    return event


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
