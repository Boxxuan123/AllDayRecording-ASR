"""Bounded continuation harness. Questions and fixtures are caller-owned local files."""

import argparse
import asyncio
import hashlib
import json
import time
from pathlib import Path
from .contracts import SourceRecord, Grant
from .model import JsonModel
from .store import SourceStore
from .hindsight import HindsightBackend
from .core import MemoryCore
from .comparison import baseline


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(path)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str), "utf-8"
    )


async def run(args):
    out = args.stage
    root = args.root
    snapshot = json.loads(args.snapshot.read_text("utf-8"))
    cases = json.loads(args.cases.read_text("utf-8"))
    rows = [SourceRecord.native(r) for r in snapshot["records"]]
    assert all(r.sent_at <= snapshot["reference"] for r in rows)
    grant = Grant(
        frozenset((r.platform, r.source_account_id) for r in rows),
        min(r.sent_at for r in rows),
        snapshot["reference"] + 1,
    )
    model = JsonModel(out / "model", max_calls=24, max_tokens=400000)
    ledger = model.ledger.export()
    if args.mode == "status" or (args.mode == "run" and len(ledger["attempts"]) >= 24):
        print(
            json.dumps(
                {
                    "attempts": len(ledger["attempts"]),
                    "max_calls": 24,
                    "accounted_tokens": ledger["accounted_tokens"],
                    "max_tokens": 400000,
                    "checkpoint": str(out.resolve()),
                    "new_generation": False,
                    "status": "stage budget exhausted; preserve checkpoints, no automatic new stage",
                },
                ensure_ascii=False,
            )
        )
        return
    backend = HindsightBackend(
        root, args.models, model, bank=args.bank, extraction_limit=4
    )
    store = SourceStore(root / "derived" / args.bank)
    start = time.monotonic()
    try:
        await backend.start()
        core = MemoryCore(
            store,
            backend,
            model,
            preferences=json.loads((out / "preferences.json").read_text("utf-8")),
        )
        core.audit = out / "queries"
        core.audit.mkdir(exist_ok=True)
        if args.mode == "preflight":
            changed = store.update(rows)
            await core.raw.sync(store.build_episodes())
            pending, _ = store.plan()
            assert len(pending) <= 4
            result = {
                "changed": len(changed),
                "pending_documents": len(pending),
                "generation_attempts": len(model.ledger.export()["attempts"]),
                "source_sha256": hashlib.sha256(args.snapshot.read_bytes()).hexdigest(),
                "cases": [],
            }
            for case in cases:
                b = baseline(
                    snapshot, case, out / "frozen-preflight", model, dry_run=True
                )
                r = await core.retrieve(
                    case["question"], case["reference"], grant, plan=False
                )
                result["cases"].append(
                    {"case": case["id"], "baseline": b, "selected": len(r["records"])}
                )
                save(
                    out / "frozen-preflight" / ("C-" + case["id"] + ".json"),
                    {
                        **r,
                        "records": [x.__dict__ | {"key": x.key} for x in r["records"]],
                    },
                )
            assert len(model.ledger.export()["attempts"]) == 0
            save(out / "preflight-complete.json", result)
        elif args.mode == "run":
            assert (out / "preflight-complete.json").exists()
            initial = time.monotonic() - start
            if not (out / "increment-result.json").exists():
                save(out / "increment-result.json", await core.ingest(rows))
            selected = [
                c for ident in args.order.split(",") for c in cases if c["id"] == ident
            ]
            for case in selected:
                paired = case["id"] in args.paired.split(",")
                model.ledger.max_calls = 18 if paired else 24
                if (
                    paired
                    and not (out / "results" / ("B-" + case["id"] + ".json")).exists()
                ):
                    save(
                        out / "results" / ("B-" + case["id"] + ".json"),
                        await asyncio.to_thread(baseline, snapshot, case, out, model),
                    )
                target = out / "results" / ("C-" + case["id"] + ".json")
                if target.exists():
                    continue
                began = time.monotonic()
                try:
                    result = await core.query(
                        case["question"], case["reference"], grant, deadline=began + 180
                    )
                except Exception as exc:
                    result = {"error": type(exc).__name__, "detail": str(exc)}
                save(
                    target,
                    {
                        "result": result,
                        "elapsed_seconds": time.monotonic() - began,
                        "initialization_seconds": initial,
                        "case": case,
                        "source_sha256": hashlib.sha256(
                            args.snapshot.read_bytes()
                        ).hexdigest(),
                    },
                )
                print(
                    case["id"],
                    result.get("query_id", result.get("error")),
                    len(model.ledger.export()["attempts"]),
                    flush=True,
                )
        elif args.mode == "components":
            for case in cases:
                p = out / "results" / ("C-" + case["id"] + ".json")
                if not p.exists():
                    continue
                result = json.loads(p.read_text("utf-8"))["result"]
                if "query_id" not in result:
                    continue
                prior = json.loads(
                    (core.audit / result["query_id"] / "retrieval.json").read_text(
                        "utf-8"
                    )
                )
                plan = prior["plan"]
                comparison = {}
                for mode in ("raw", "hybrid"):
                    r = await core.retrieve(
                        case["question"],
                        case["reference"],
                        grant,
                        planned=plan,
                        component=mode,
                    )
                    comparison[mode] = {
                        **r,
                        "records": [x.__dict__ | {"key": x.key} for x in r["records"]],
                    }
                save(out / "components" / (case["id"] + ".json"), comparison)
    finally:
        await backend.close()
        model.ledger.export()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("mode", choices=["preflight", "run", "components", "status"])
    p.add_argument("--root", type=Path, default=Path("outputs/private"))
    p.add_argument("--stage", type=Path, default=Path("outputs/private/stage2"))
    p.add_argument(
        "--snapshot",
        type=Path,
        default=Path("outputs/private/stage2/snapshot-expanded.json"),
    )
    p.add_argument(
        "--cases",
        type=Path,
        default=Path("outputs/private/evaluation-private/cases-multiwindow.json"),
    )
    p.add_argument("--models", type=Path, default=Path("outputs/models"))
    p.add_argument("--bank", default="memory-core-v2-facts-v1")
    p.add_argument("--order", default="T1,D3,D1,D2,M1")
    p.add_argument("--paired", default="T1,D3")
    asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    main()
