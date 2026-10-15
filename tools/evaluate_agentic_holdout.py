"""Budget-gated fictional holdout runner; default only validates, never calls a model.

After separate explicit authorization, --real --max-model-calls 6 --max-seconds 180
runs at most three cases. Evaluation keys are never passed to HistoryRecall/model.
Inspect per-case evidence/claims manually using evaluation_only; this is not a grader.
"""

import argparse
import hashlib
import json
import tempfile
import time
from datetime import datetime
from pathlib import Path
from pytest import MonkeyPatch
from tests.test_agentic_recall import lab
from tests.test_recall_query_loop import message
from allday_asr.v3.adapters.codex.recall_investigator import RecallInvestigatorAdapter


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real", action="store_true")
    parser.add_argument("--max-model-calls", type=int, default=0)
    parser.add_argument("--max-seconds", type=int, default=0)
    parser.add_argument("--output", type=Path, default=Path("outputs/agentic-holdout"))
    args = parser.parse_args()
    path = (
        Path(__file__).resolve().parents[1]
        / "tests/fixtures/agentic_holdout/cases.json"
    )
    raw = path.read_bytes()
    suite = json.loads(raw.decode("utf-8-sig"))
    receipt = {
        "suite_sha256": hashlib.sha256(raw).hexdigest(),
        "status": "NOT_RUN",
        "real_model_calls": 0,
        "cases": len(suite["cases"]),
        "results": [],
    }
    args.output.mkdir(parents=True, exist_ok=True)
    if args.real:
        if not 1 <= args.max_model_calls <= 6 or not 1 <= args.max_seconds <= 180:
            parser.error(
                "real execution requires explicit positive budgets: <=6 calls and <=180 seconds"
            )
        deadline = time.monotonic() + args.max_seconds
        for case in suite["cases"]:
            if (
                receipt["real_model_calls"] >= args.max_model_calls
                or time.monotonic() >= deadline
            ):
                break
            patch = MonkeyPatch()
            try:
                # All state under this fresh fictional sandbox, never the user's state directory.
                root = Path(tempfile.mkdtemp(prefix="agentic-holdout-"))
                env = lab.__wrapped__(root, patch)
                now = int(datetime.fromisoformat(suite["local_now"]).timestamp())
                patch.setattr(
                    "allday_asr.v3.application.history_recall.time.time",
                    lambda now=now: now,
                )
                env.contacts = [
                    {
                        "platform": "qq",
                        "source_account_id": "source-qq",
                        "id": c["id"],
                        "display_name": c["name"],
                        "conversation_type": c["kind"],
                        "peer_account_id": c.get("peer"),
                    }
                    for c in case["contacts"]
                ]
                contacts = {c["id"]: c for c in case["contacts"]}
                rows = [
                    message(
                        r["id"],
                        r["text"],
                        r["sender"],
                        conversation=r["conversation"],
                        sent=int(datetime.fromisoformat(r["at"]).timestamp()),
                        kind=contacts[r["conversation"]]["kind"],
                    )
                    for r in case["messages"]
                ]
                with env.cache.connect() as c:
                    c.execute("DELETE FROM search_index")
                    c.execute("DELETE FROM records")
                    c.execute("DELETE FROM tasks")
                env.source.records = rows
                env.cache.receive(env.source.dataset, rows)
                env.cache.process(100)
                adapter = RecallInvestigatorAdapter(root)

                def bounded(payload, cancel, adapter=adapter):
                    if (
                        receipt["real_model_calls"] >= args.max_model_calls
                        or time.monotonic() >= deadline
                    ):
                        raise TimeoutError("explicit holdout budget exhausted")
                    receipt["real_model_calls"] += 1
                    payload = {
                        **payload,
                        "model_budget_seconds": min(
                            payload["model_budget_seconds"],
                            max(1, int(deadline - time.monotonic())),
                        ),
                    }
                    return adapter(payload, cancel)

                env.service.investigator = bounded
                # No fake summary fallback can report a real-model success.
                env.service.model = lambda p, c: (_ for _ in ()).throw(
                    RuntimeError("unexpected simple-path model")
                )
                result = env.service.execute(
                    "holdout",
                    {
                        "action": "query",
                        "query_id": "holdout-" + case["id"],
                        "question": case["question"],
                        "source": "qq",
                        "timezone": suite["timezone"],
                        "expand": True,
                        "summarize": True,
                    },
                )
                receipt["results"].append(
                    {
                        "id": case["id"],
                        "response": result,
                        "private_receipts": str(root / "chat/investigation-receipts"),
                    }
                )
            finally:
                patch.undo()
        receipt["status"] = "COMPLETED_REQUIRES_HUMAN_SCORING"
    (args.output / "receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", "utf-8"
    )
    print(json.dumps({k: v for k, v in receipt.items() if k != "results"}))


if __name__ == "__main__":
    main()
