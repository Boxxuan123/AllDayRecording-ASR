"""Bounded local acceptance; private evidence never printed or committed."""

import argparse
from pathlib import Path
import json
from types import SimpleNamespace
import time

from allday_asr.v3.adapters.chat_data.mac import MacChatData
from allday_asr.v3.bootstrap.chat import compose_chat_queries
from allday_asr.v3.bootstrap.chat_followups import FollowupJobs
from allday_asr.v3.adapters.sqlite import V3Database
from allday_asr.v3.domain.chat_data import packed


def prepare(root, fixed, connection):
    source = MacChatData(config_path=connection)
    chat = compose_chat_queries(root / "source", source=source, start_worker=False)
    status = chat.discover(force=True)
    original = json.loads(fixed.read_text(encoding="utf8"))
    records = {}
    coverage = []
    scopes = []
    for entry in original:
        scope = entry["scope"]
        items = []
        cursor = None
        while True:
            page = chat.search(scope, cursor, 100, "mac")
            items.extend(page["items"])
            if not page["has_more"]:
                break
            cursor = page["next_cursor"]
        assert packed(items) == packed(entry["ordinary_records"]), (
            "historical body mismatch"
        )
        records.update({r["record_id"]: r for r in items})
        scopes.append(
            {k: scope[k] for k in ("platform", "source_account_id", "conversation_id")}
        )
        coverage.append(
            {
                "kind": "historical_replay",
                "scope": scope,
                "received": len(items),
                "complete": True,
            }
        )
    assert len(records) == 128
    now = int(time.time())
    recent = 0
    for base in scopes:
        scope = {**base, "sent_from": now - 7 * 86400, "sent_to": now}
        cursor = None
        count = 0
        complete = False
        for _ in range(100):
            page = chat.search(scope, cursor, 100, "mac")
            accepted = 0
            for r in page["items"]:
                if r["record_id"] not in records and len(records) >= 1000:
                    break
                recent += r["record_id"] not in records
                records[r["record_id"]] = r
                count += 1
                accepted += 1
            complete = not page["has_more"] and accepted == len(page["items"])
            if complete or len(records) >= 1000:
                break
            cursor = page["next_cursor"]
        coverage.append(
            {
                "kind": "recent_7_days",
                "scope": scope,
                "received": count,
                "complete": complete,
                "next_cursor": None if complete else cursor,
                "partial_page_ids": []
                if complete
                else [r["record_id"] for r in page["items"]],
            }
        )
        if len(records) >= 1000:
            break
    if len(coverage) < 6:
        coverage.append({"complete": False, "reason": "other recent scopes unvisited"})
    # Model needs full original text and namespace, not acquired-media metadata or display names.
    fields = {
        "record_id",
        "record_revision",
        "platform",
        "source_account_id",
        "conversation_id",
        "sender_account_id",
        "sent_at",
        "text",
        "reply",
        "quote",
        "forward",
        "source_locator",
    }
    workset = [{k: v for k, v in r.items() if k in fields} for r in records.values()]
    data = {
        "records": workset,
        "dataset": status["dataset_id"],
        "coverage": coverage,
        "scopes": scopes,
        "status": status,
        "historical_messages": 128,
        "recent_new_messages": recent,
        "unique_messages": len(records),
        "timezone": None,
    }
    (root / "prepared-private.json").write_text(packed(data), encoding="utf8")
    chat.close()
    return data


def controller(root, connection, model=None):
    database = V3Database.open(root / "core.sqlite3")
    core = SimpleNamespace(
        database=database, paths=SimpleNamespace(state_dir=root), reminders=None
    )
    chat = compose_chat_queries(
        root / "source", source=MacChatData(config_path=connection), start_worker=False
    )
    return FollowupJobs(core, chat, model=model), chat


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["prepare", "run", "summary", "replay"])
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--fixed", type=Path)
    parser.add_argument("--connection", required=True, type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    if "state" not in root.parts:
        raise SystemExit("private output must be under state")
    root.mkdir(parents=True, exist_ok=True)
    if args.stage == "prepare":
        data = prepare(root, args.fixed, args.connection)
        ctl, chat = controller(root, args.connection)
        ctl.configure(data["scopes"], data["timezone"])
        value = {
            "records": data["records"],
            "dataset": data["dataset"],
            "timezone": data["timezone"],
            "self_account_links": ctl.links(),
        }
        batches = ctl._batch(value)
        print(
            packed(
                {
                    "historical_messages": 128,
                    "recent_new_messages": data["recent_new_messages"],
                    "unique_messages": data["unique_messages"],
                    "planned_batches": len(batches),
                    "source_version": data["status"]["service_version"],
                    "actual_model_calls": 0,
                }
            )
        )
        ctl.close()
        chat.close()
        return
    data = json.loads((root / "prepared-private.json").read_text(encoding="utf8"))
    if args.stage == "run":
        ctl, chat = controller(root, args.connection)
        if (root / "model-run-started.json").exists():
            raise SystemExit(
                "this acceptance run already started; use summary/replay, never reset budget"
            )
        (root / "model-run-started.json").write_text(
            packed({"started_at": time.time(), "max_calls": 12, "max_seconds": 900}),
            encoding="utf8",
        )
        job = ctl.start(
            records=data["records"], dataset=data["dataset"], coverage=data["coverage"]
        )
        (root / "job-id.json").write_text(packed({"id": job["id"]}), encoding="utf8")
        while ctl._thread.is_alive():
            ctl._thread.join(timeout=20)
            j = ctl.service.job(job["id"])
            print(
                packed(
                    {
                        "state": j["state"],
                        "calls": j["calls"],
                        "processed_batches": j["position"],
                    }
                ),
                flush=True,
            )
        ctl.close()
        chat.close()
    ctl, chat = controller(root, args.connection)
    job_id = json.loads((root / "job-id.json").read_text(encoding="utf8"))["id"]
    job = ctl.service.job(job_id)
    if args.stage == "replay":
        replay, replay_chat = controller(root / "replay-validation", args.connection)
        replay_chat.cache.activate(data["dataset"])
        effects = []
        for file in sorted(
            ctl.root.glob(job_id + "-*.response.json"),
            key=lambda p: int(p.name.split("-")[-1].split(".")[0]),
        ):
            captured = json.loads(file.read_text(encoding="utf8"))
            for c in captured["response"]["items"]:
                try:
                    effects.append(
                        replay.service.apply(
                            c,
                            captured.get(
                                "source_records", captured["payload"]["records"]
                            ),
                            data["dataset"],
                            ctl.links(),
                            timezone_name=data.get("timezone"),
                        )
                    )
                except Exception as exc:
                    effects.append({"error": getattr(exc, "code", type(exc).__name__)})
        (root / "replay-private.json").write_text(packed(effects), encoding="utf8")
        print(
            packed(
                {
                    "replayed_items": len(effects),
                    "errors": sum("error" in x for x in effects),
                    "extra_model_calls": 0,
                }
            )
        )
        replay.close()
        replay_chat.close()
    receipts = [
        json.loads(p.read_text(encoding="utf8"))
        for p in (ctl.root / "model-receipts").glob("*.receipt.json")
    ]
    usage = [r.get("usage") for r in receipts]
    summary = {
        "state": job["state"],
        "calls": len(receipts),
        "scheduled_calls": job["calls"],
        "processed_batches": job["position"],
        "planned_batches": len(job.get("batches") or []),
        "failed_candidates": job["failed_candidates"],
        "error": job.get("error"),
        "historical_messages": data["historical_messages"],
        "recent_new_messages": data["recent_new_messages"],
        "unique_messages": data["unique_messages"],
        "model_seconds": sum(
            r.get("completed_at", r["started_at"]) - r["started_at"] for r in receipts
        ),
        "input_tokens": sum(
            (u or {}).get("last", {}).get("inputTokens", 0) for u in usage
        ),
        "output_tokens": sum(
            (u or {}).get("last", {}).get("outputTokens", 0) for u in usage
        ),
        "usage_available": all(u is not None for u in usage),
        "items": len(ctl.service.list(100)["items"]),
        "calendar_device": "NOT_RUN",
    }
    (root / "summary.json").write_text(packed(summary), encoding="utf8")
    print(packed(summary))
    ctl.close()
    chat.close()


if __name__ == "__main__":
    main()
