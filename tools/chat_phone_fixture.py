"""Executable synthetic sync peer for Phone's on-disk SQLite/Calendar regression.
Never connects to Mac/models/devices or opens a production database.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from allday_asr.v3.adapters.sqlite import V3Database, SqliteUnitOfWork
from allday_asr.v3.adapters.sqlite.chat_cache import ChatCache
from allday_asr.v3.bootstrap.chat_followups import FollowupJobs
from allday_asr.v3.application.chat_followups import ChatFollowups
from allday_asr.v3.application.mobile_sync import (
    MobileSyncService,
    UtteranceCorrectionOperationHandler,
)
from allday_asr.v3.domain.device_sync import (
    ClientOperation,
    SyncRequest,
    PROJECTION_VERSION,
)
from allday_asr.v3.domain.ids import new_ulid
from allday_asr.v3.domain.chat_data import packed
from tests.chat_fixture import record
from tests.test_chat_followups import candidate


def run(root, request):
    if not root.name.startswith("chat-phone-loop-"):
        raise ValueError("Synthetic temporary directory required")
    db = V3Database.open(root / "core.sqlite3")

    def factory():
        return SqliteUnitOfWork(db)

    cache = ChatCache(root / "cache.sqlite3")
    cache.activate("dataset")
    marker = root / "fixture.json"
    if not marker.exists():
        now = datetime.now(timezone.utc)
        r = record(990, "我负责提交资料", sent=int(now.timestamp()) - 60)
        r.update(is_from_self=True, self_identity_basis="synthetic.direction")
        cache.receive("dataset", [r])
        ctl = FollowupJobs(
            SimpleNamespace(
                paths=SimpleNamespace(state_dir=root), database=db, reminders=None
            ),
            SimpleNamespace(cache=cache),
            model=lambda *a: (_ for _ in ()).throw(
                AssertionError("No model calls allowed")
            ),
        )
        job = ctl.start(
            records=[r],
            dataset="dataset",
            coverage=[{"complete": True}],
            prepare_only=True,
        )
        ctl._thread.join(8)
        saved = {
            "source_records": [r],
            "payload": {},
            "response": {
                "items": [candidate(r, title="CHAT_PHONE_LOOP_TEST 提交资料")],
                "unknown": [],
            },
        }
        (ctl.root / (job["id"] + "-0.response.json")).write_text(
            packed(saved), encoding="utf-8"
        )
        ctl.start(resume_id=job["id"])
        ctl._thread.join(8)
        review = ctl.review_candidates()["items"][0]
        ctl.accept_candidate(
            review["job_id"], review["batch"], review["candidate"], review["binding"]
        )
        assert ctl.service.job(job["id"])["calls"] == 0
        item = ctl.service.list()["items"][0]
        device = new_ulid()
        with db.transaction() as c:
            c.execute(
                "INSERT INTO devices VALUES(?, 'phone','SYNTHETIC PHONE','active',1,NULL,NULL,NULL,?,?)",
                (device, now.isoformat(), now.isoformat()),
            )
        marker.write_text(
            packed(
                {"device": device, "event_id": item["event_id"], "job_id": job["id"]}
            ),
            encoding="utf-8",
        )
        ctl.close()
    metadata = json.loads(marker.read_text(encoding="utf-8"))
    if request.get("progress"):
        with factory().reading() as uow:
            current = uow.knowledge.get_event(metadata["event_id"])
        old = cache.record(current.payload["chat_followup"]["anchor_id"])
        date_only = request["progress"] == "date_only"
        ordinary = request["progress"] == "ordinary"
        action = "update" if date_only or ordinary else request["progress"]
        newer = record(
            991 + current.revision,
            "资料已经发给你了"
            if action == "complete"
            else (
                "资料已经整理好，还在等老师确认" if ordinary else "改到2030年10月12日提交" if date_only else "改到2030年10月12日14:00提交"
            ),
            sent=int(datetime.now(timezone.utc).timestamp()) + current.revision,
        )
        newer.update(is_from_self=True, self_identity_basis="synthetic.direction")
        cache.receive("dataset", [newer])
        records = [
            cache.record(e["record_id"])
            for e in current.payload["chat_followup"]["evidence"]
        ] + [newer]
        proposal = candidate(
            old,
            action=action,
            target_event_id=metadata["event_id"],
            evidence=[
                {"record_id": r["record_id"], "quote": r["text"]} for r in records
            ],
        )
        if action == "update" and not ordinary:
            proposal.update(
                time_expression="2030年10月12日"
                if date_only
                else "2030年10月12日14:00",
                time_record_id=newer["record_id"],
                due_date="2030-10-12",
                due_at=None if date_only else "2030-10-12T14:00:00+08:00",
            )
        result = ChatFollowups(factory).apply(
            proposal, records, "dataset", {}, timezone_name="Asia/Shanghai"
        )
        assert result["effect"] == "human_conflict"
    if request.get("replay_original"):
        service = ChatFollowups(factory)
        with factory().reading() as uow:
            current = uow.knowledge.get_event(metadata["event_id"])
        old = cache.record(current.payload["chat_followup"]["anchor_id"])
        result = service.apply(
            candidate(old, title="CHAT_PHONE_LOOP_TEST 提交资料"), [old], "dataset", {}
        )
        assert result["effect"] in {"duplicate", "older_or_same_evidence"}
    service = MobileSyncService(
        factory, operation_handler=UtteranceCorrectionOperationHandler()
    )
    operations = tuple(
        ClientOperation(**op) for op in request.get("client_operations", [])
    )
    response = service.synchronize(
        metadata["device"],
        SyncRequest(
            PROJECTION_VERSION,
            request.get("cursor"),
            operations,
            request.get("pull_limit", 100),
        ),
    ).as_dict()
    with db.read() as c:
        assert c.execute("SELECT count(*) FROM event_current_states").fetchone()[0] == 1
        assert (
            c.execute("SELECT session_id FROM event_current_states").fetchone()[0]
            is None
        )
        assert not c.execute("PRAGMA foreign_key_check").fetchall()
    return response


if __name__ == "__main__":
    print(json.dumps(run(Path(sys.argv[1]), json.load(sys.stdin)), ensure_ascii=False))
