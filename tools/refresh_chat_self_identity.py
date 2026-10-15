"""Metadata-only fixed-cache-ID backfill; no discovery scan or model inference."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from allday_asr.v3.adapters.sqlite.chat_cache import ChatCache
from allday_asr.v3.adapters.chat_data.mac import MacChatData
from allday_asr.v3.application.chat_identity_refresh import IdentityRefresh


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=Path("state/v3"))
    parser.add_argument("--resume")
    parser.add_argument("--max-batches", type=int, default=50)
    parser.add_argument("--max-records", type=int, default=5000)
    parser.add_argument("--seconds", type=int, default=300)
    args = parser.parse_args()
    cache = ChatCache(args.state_dir / "chat/cache.sqlite3")
    source = MacChatData(config_path=args.state_dir / "chat/connection.json")
    worker = IdentityRefresh(cache, source)
    job = args.resume or worker.create(
        max_records=args.max_records, seconds=args.seconds
    )

    def safe(value):
        print(
            json.dumps(
                {k: v for k, v in value.items() if k != "dataset"}, ensure_ascii=False
            ),
            flush=True,
        )

    result = worker.run(job, max_batches=args.max_batches, progress=safe)
    safe(result)
    if result["success"]:
        from allday_asr.v3.bootstrap import V3CorePaths, compose_v3_core
        from allday_asr.v3.adapters.sqlite.unit_of_work import SqliteUnitOfWork
        from allday_asr.v3.application.chat_followups import ChatFollowups
        from allday_asr.v3.application.chat_followup_identity import rebuild_identity

        core = compose_v3_core(V3CorePaths.from_state_dir(args.state_dir))
        core.initialize()
        try:
            service = ChatFollowups(
                lambda: SqliteUnitOfWork(core.database), reminders=core.reminders
            )
            print(
                json.dumps(
                    {"followup_identity_rebuild": rebuild_identity(service, cache)},
                    ensure_ascii=False,
                )
            )
        finally:
            core.close()
    return 0 if worker.status(job)["state"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
