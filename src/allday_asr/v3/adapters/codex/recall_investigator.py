"""The existing model runner executes the bounded investigation protocol."""

import json
from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo
from allday_asr.v3.domain.chat_data import packed
from allday_asr.v3.domain.recall_investigation import (
    SCHEMA,
    INSTRUCTIONS,
    bounded_final_protocol,
    checked,
    checked_final,
)
from .model_execution_runner import ModelExecutionRunner


class RecallInvestigatorAdapter:
    def __init__(self, root):
        self.root = root

    def __call__(self, payload, cancel):
        from allday_asr.v3.domain.recall_input_validation import prepare_payload

        payload = prepare_payload(payload)
        seconds = max(1, min(25, payload["model_budget_seconds"]))
        runner = ModelExecutionRunner(
            self.root / "chat/investigation-receipts",
            timeout_seconds=seconds,
            max_attempts=1,
            max_batch_calls=4,
            batch_deadline_seconds=90,
            max_prompt_bytes=60000,
        )
        workdir = self.root / "chat/model-workspace"
        workdir.mkdir(parents=True, exist_ok=True)
        schema, instructions = (
            bounded_final_protocol(compact=True)
            if payload.get("final_round", payload.get("round") == 2)
            else (SCHEMA, INSTRUCTIONS)
        )
        schema = deepcopy(schema)
        if payload.get("address_resolution_required") is False:
            schema["properties"]["candidates"]["maxItems"] = 0
        if "actions" in schema["properties"]:
            action_schema = schema["properties"]["actions"]["items"]["properties"]
            action_schema["targets"]["items"] = {
                "type": "string",
                "pattern": r"^(?:[ECA][1-9]\d*|\$\d+)$",
            }
            action_schema["evidence_ids"]["items"] = {
                "type": "string",
                "pattern": r"^E[1-9]\d*$",
            }
        schema["properties"]["candidates"]["items"]["properties"]["contact_key"] = {
            "type": "string",
            "pattern": r"^C[1-9]\d*$",
        }
        runner.prepare_request(
            task="history-investigation-v1",
            prompt=packed(payload),
            instructions=instructions,
            schema=schema,
            model=None,
            effort="low",
        )
        answer = runner.run(
            task="history-investigation-v1",
            batch_id="history-investigation:" + payload["query_id"],
            prompt=packed(payload),
            instructions=instructions,
            schema=schema,
            model=None,
            effort="low",
            workdir=workdir,
            cancel_event=cancel,
        )
        result = json.loads(answer.final_response)
        return (
            checked_final(result)
            if payload.get("final_round", payload.get("round") == 2)
            else checked(result, schema)
        )
