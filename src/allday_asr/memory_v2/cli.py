"""Offline candidate entry point. No production service integration."""

import argparse
import asyncio
import json
import time
from pathlib import Path
from .contracts import SourceRecord, Grant
from .store import SourceStore
from .model import JsonModel
from .hindsight import HindsightBackend
from .core import MemoryCore


async def execute(args):
    root = args.root.resolve()
    snapshot = json.loads(args.snapshot.read_text("utf-8"))
    rows = [SourceRecord.native(r) for r in snapshot["records"]]
    grant = Grant(
        frozenset((r.platform, r.source_account_id) for r in rows),
        min(r.sent_at for r in rows),
        max(r.sent_at for r in rows) + 1,
    )
    model = JsonModel(root / "model")
    backend = HindsightBackend(root, args.models, model, bank=args.bank)
    store = SourceStore(root / "derived" / args.bank)
    started = time.monotonic()
    try:
        await backend.start()
        core = MemoryCore(store, backend, model)
        if args.command == "preflight":
            from .raw import tokenize

            vec = await backend.embeddings.encode_query(["明晚报名 API更新"])
            scores = await backend.reranker.predict(
                [("报名什么时候截止", "明晚之前报名"), ("报名什么时候截止", "显卡测试")]
            )
            result = {
                "engine": "Hindsight 1.0.0",
                "embedding_dimensions": len(vec[0]),
                "rerank_scores": scores,
                "tokens": tokenize("明晚报名 API更新"),
                "generation_attempts": len(model.ledger.export()["attempts"]),
            }
        elif args.command == "ingest":
            result = await core.ingest(rows)
        elif args.command == "ask":
            result = await core.query(
                args.question, snapshot["reference"], grant, deadline=started + 180
            )
        elif args.command == "overview":
            result = await core.overview(
                args.start, args.end, snapshot["reference"], grant
            )
        elif args.command in {"demo", "metamorphic"}:
            from .demonstrations import demonstrations, metamorphic

            result = await (demonstrations if args.command == "demo" else metamorphic)(
                core, snapshot, grant, root
            )
        elif args.command == "compare":
            from .comparison import compare

            result = await compare(
                core, snapshot, grant, args.cases, args.project, root
            )
        else:
            raise ValueError(args.command)
        result["elapsed_seconds"] = time.monotonic() - started
        result["ledger"] = model.ledger.export()
        destination = root / (args.command + "-result.json")
        if destination.exists():
            destination = root / (
                args.command + "-" + str(time.time_ns()) + "-result.json"
            )
        destination.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), "utf-8"
        )
        print(
            json.dumps(
                {
                    "result": str(destination),
                    "elapsed_seconds": result["elapsed_seconds"],
                    "attempts": len(result["ledger"]["attempts"]),
                },
                ensure_ascii=False,
            )
        )
    finally:
        await backend.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument(
        "command",
        choices=[
            "preflight",
            "ingest",
            "ask",
            "overview",
            "compare",
            "demo",
            "metamorphic",
        ],
    )
    p.add_argument("--root", type=Path, default=Path("outputs/private"))
    p.add_argument(
        "--snapshot",
        type=Path,
        default=Path("outputs/private/corpus/snapshot-multiwindow.json"),
    )
    p.add_argument("--models", type=Path, default=Path("outputs/models"))
    p.add_argument("--bank", default="memory-core-v2-facts-v1")
    p.add_argument("--question", default="")
    p.add_argument(
        "--cases",
        type=Path,
        default=Path("outputs/private/evaluation-private/cases-multiwindow.json"),
    )
    p.add_argument("--project", type=Path, default=Path("../.."))
    p.add_argument("--start", type=int, default=1791907200)
    p.add_argument("--end", type=int, default=1791993600)
    asyncio.run(execute(p.parse_args()))


if __name__ == "__main__":
    main()
