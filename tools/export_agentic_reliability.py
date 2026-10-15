"""Export fictional offline reliability receipts; never calls a real model."""

import argparse
import json
from pathlib import Path
from time import monotonic
from pytest import MonkeyPatch
from tests.test_agentic_evidence_reliability import CASES, evaluate, unfamiliar
from tests.test_agentic_recall import response, action


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    summaries = []
    for case in [*CASES, unfamiliar()]:
        started = monotonic()
        plan = (
            response(actions=[action("find_conversations", terms=["组"])])
            if case["id"] == "NEW"
            else None
        )
        with MonkeyPatch.context() as mp:
            result, inputs, source = evaluate(
                case, args.output / case["id"], mp, plan=plan
            )
        receipt = {
            "case": case,
            "result": result,
            "model_inputs": inputs,
            "source_calls": source.calls,
            "real_model_calls": 0,
            "real_reasoning": "NOT_RUN",
            "elapsed_seconds": round(monotonic() - started, 4),
        }
        (args.output / ("case-" + case["id"] + ".json")).write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        summaries.append(
            {
                "id": case["id"],
                "elapsed_seconds": receipt["elapsed_seconds"],
                "metrics": result["metrics"],
                "final_source_ids": [r["record_id"] for r in inputs[-1]["records"]],
                "counts": result["evidence_flow"]["counts"],
                "gaps": result["evidence_flow"]["gaps"],
                "real_reasoning": "NOT_RUN",
            }
        )
    (args.output / "summary.json").write_text(
        json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summaries, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
