"""The existing model runner executes the bounded investigation protocol."""

import json
from allday_asr.v3.domain.chat_data import packed
from allday_asr.v3.domain.recall_investigation import SCHEMA, INSTRUCTIONS
from .model_execution_runner import ModelExecutionRunner


class RecallInvestigatorAdapter:
    def __init__(self, root):
        self.root = root

    def __call__(self, payload, cancel):
        seconds = max(1, min(25, payload["model_budget_seconds"]))
        runner = ModelExecutionRunner(
            self.root / "chat/investigation-receipts",
            timeout_seconds=seconds,
            max_attempts=1,
            max_batch_calls=2,
            batch_deadline_seconds=60,
            max_prompt_bytes=60000,
        )
        workdir = self.root / "chat/model-workspace"
        workdir.mkdir(parents=True, exist_ok=True)
        answer = runner.run(
            task="history-investigation-v1",
            batch_id="history-investigation:" + payload["query_id"],
            prompt=packed(payload),
            instructions=INSTRUCTIONS,
            schema=SCHEMA,
            model=None,
            effort="low",
            workdir=workdir,
            cancel_event=cancel,
        )
        result = json.loads(answer.final_response)
        return result
