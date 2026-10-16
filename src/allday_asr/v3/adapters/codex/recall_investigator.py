"""The existing model runner executes the bounded investigation protocol."""

import json
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
        zone = ZoneInfo(payload["scope"]["timezone"])
        payload = {
            **payload,
            "records": [
                {
                    **row,
                    "sent_at_local": datetime.fromtimestamp(
                        row["sent_at"], zone
                    ).isoformat(),
                }
                for row in payload["records"]
            ],
        }
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
