"""One bounded invocation of the existing model runner; no background service."""

import json
from allday_asr.v3.domain.chat_data import packed
from allday_asr.v3.domain.recall_answer import INSTRUCTIONS, SCHEMA
from .model_execution_runner import ModelExecutionRunner


class RecallModelAdapter:
    def __init__(self, root):
        self.root = root

    def __call__(self, payload, cancel):
        from allday_asr.v3.domain.recall_input_validation import prepare_payload
        payload = prepare_payload(payload)
        seconds = payload.get("model_budget_seconds", 45)
        runner = ModelExecutionRunner(
            self.root / "chat/model-receipts",
            timeout_seconds=seconds,
            max_attempts=1,
            max_batch_calls=1,
            batch_deadline_seconds=seconds,
            max_prompt_bytes=60000,
        )
        workdir = self.root / "chat/model-workspace"
        workdir.mkdir(parents=True, exist_ok=True)
        instructions = (INSTRUCTIONS
            + " For intent=conversation, summarize actual topics, important discussions, explicit decisions and unresolved points as separate cited claims. Never answer with a retrieval status. Use ONLY the supplied E-number handles for citations. All coverage is partial. Distinguish plans, decisions, reported completion and verified execution. Compare contradictory evidence explicitly.")
        runner.prepare_request(task="history-recall-v2",prompt=packed(payload),
            instructions=instructions,schema=SCHEMA,model=None,effort="low")
        answer = runner.run(
            task="history-recall-v2",
            batch_id=f"history-recall:{payload['query_id']}:{payload.get('summary_attempt', 0)}",
            prompt=packed(payload),
            instructions=instructions,
            schema=SCHEMA,
            model=None,
            effort="low",
            workdir=workdir,
            cancel_event=cancel,
        )
        return json.loads(answer.final_response)
