"""Offline before/after evaluation. Every record and model response is fictional.

Run with --checkout pointing at a clean baseline or this checkout. No network or
production paths are opened. The stub only validates quote/citation transport;
it does not measure a real model's semantic understanding.
"""

import argparse
import hashlib
import json
import sys
import tempfile
import time
from copy import deepcopy
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sys.path[:0] = [str(args.checkout.resolve()), str(args.checkout.resolve() / "src")]
    from tests.test_history_recall import env, query, decision
    from tests.chat_fixture import record, NOW
    from tests.test_cross_day_memory_quality import row
    from allday_asr.v3.domain.memory_content import content

    corpus = json.loads(args.corpus.read_text(encoding="utf-8"))
    report = {
        "corpus_sha256": hashlib.sha256(args.corpus.read_bytes()).hexdigest(),
        "data": "fictional SQLite",
        "real_model_calls": 0,
        "production_reads_including_metadata": 0,
        "cases": [],
        "model_understanding": "UNVERIFIED: exact-quote stub, no real model",
    }

    def run(case, rows, target, remote=False):
        with tempfile.TemporaryDirectory(prefix="recall-eval-fictional-") as temporary:
            service, cache, db, base, source = env.__wrapped__(Path(temporary))
            for r in rows:
                r.update(conversation_type="c2c", is_from_self=False)
            source.records.extend(deepcopy(rows))
            cache.receive(source.dataset, rows)
            cache.process(200)
            if remote:
                decision(service, base[0])
                source.records.append(target)
            calls = []

            def model(payload, cancel):
                calls.append(deepcopy(payload))
                e = payload["records"][-1]
                return {
                    "claims": [
                        {"kind": "quote", "text": e["quote"], "citations": [e["id"]]}
                    ],
                    "unknown": [
                        "Fictional quote-only test adapter; not a semantic evaluation."
                    ],
                }

            service.model = model
            started = time.perf_counter()
            value = query(
                service,
                question=case["question"],
                keyword=case.get("keyword", ""),
                source="qq",
                person_id="alice-person" if remote else "",
                sent_to=NOW + 500,
                expand=remote,
                summarize=True,
            )
            saved = service._saved("phone", value["query_id"])
            input_rows = calls[0]["records"] if calls else []
            target_id = "chat:" + target["record_id"]
            retrieved = any(e["id"] == target_id for e in saved["evidence"])
            selected = any(e["id"] == target_id for e in input_rows)
            quotes_valid = all(
                any(
                    claim["text"] in e["quote"] and e["id"] in claim["citations"]
                    for e in input_rows
                )
                for claim in value["claims"]
            )
            report["cases"].append(
                {
                    "id": case["id"],
                    "question": case["question"],
                    "expected_quote": target["text"],
                    "correct_evidence_retrieved": retrieved,
                    "correct_evidence_in_model": selected,
                    "quoted_claims_supported": bool(value["claims"]) and quotes_valid,
                    "latest_evidence_selected": selected
                    if case["id"] in {"last-of-31", "five-old-plus-new", "latest"}
                    else None,
                    "failure_layer": "retrieval"
                    if not retrieved
                    else "selection"
                    if not selected
                    else "none (model understanding unverified)",
                    "input_records": len(input_rows),
                    "stub_model_calls": len(calls),
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                    "metrics": {
                        k: v
                        for k, v in value["metrics"].items()
                        if k != "model_input_ids"
                    },
                }
            )

    for case in corpus["queries"]:
        target = record(200, case["evidence"], conversation="fixed")
        run(
            case,
            [
                target,
                record(201, "今天是否需要买茶叶，我们再问一下。", conversation="noise"),
            ],
            target,
        )
    rows = [
        record(200 + i, "星港展览：筹备记录 " + str(i), conversation="exhibit")
        for i in range(31)
    ]
    rows[0]["text"] = "星港展览：因为场地合适，计划周六举办。"
    rows[-1]["text"] = "星港展览已经取消。"
    run(
        {
            "id": "last-of-31",
            "question": "星港展览最终决定是什么",
            "keyword": "星港展览",
        },
        rows,
        rows[-1],
    )
    rows = [
        record(300 + i, "星港展览准备记录 " + str(i), conversation="exhibit")
        for i in range(5)
    ]
    later = record(320, "已经取消，请勿再订车。", conversation="exhibit", sender="bob")
    run(
        {
            "id": "five-old-plus-new",
            "question": "星港展览后来怎么样",
            "keyword": "星港展览",
        },
        rows,
        later,
        True,
    )
    report["semantic_fallback"] = [
        dict(
            case,
            title=content(row(case["text"]))[0],
            summary=content(row(case["text"]))[1],
        )
        for case in corpus["semantics"]
    ]
    denial = "我没有收到招物理老师的消息，也没有准备回复。"
    report["audit_denial_regression"] = {
        "input": denial,
        "output": content(row(denial)),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "cases": len(report["cases"]),
                "retrieved": sum(
                    c["correct_evidence_retrieved"] for c in report["cases"]
                ),
                "selected": sum(
                    c["correct_evidence_in_model"] for c in report["cases"]
                ),
                "real_model_calls": 0,
            }
        )
    )


if __name__ == "__main__":
    main()
