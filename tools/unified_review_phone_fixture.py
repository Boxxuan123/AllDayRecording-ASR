"""Synthetic ASR -> Phone SQLite daily review acceptance peer; zero providers."""

import json
import sys
from pathlib import Path
from allday_asr.v3.adapters.sqlite import V3Database, SqliteUnitOfWork
from allday_asr.v3.application.unified_review import UnifiedReviewService
from allday_asr.v3.application.mobile_sync import MobileSyncService
from allday_asr.v3.domain.device_sync import SyncRequest, PROJECTION_VERSION
from allday_asr.v3.domain.ids import stable_ulid

root = Path(sys.argv[1])
if not root.parent.name.startswith("unified-review-phone-"):
    raise ValueError("Synthetic temporary directory required")
root.mkdir(parents=True, exist_ok=True)
request = json.load(sys.stdin)
db = V3Database.open(root / "core.sqlite3")


def factory():
    return SqliteUnitOfWork(db)


revision = int(request.get("revision", 1))
sid = stable_ulid("synthetic-recording")
device = stable_ulid("synthetic-phone")
with db.transaction() as c:
    c.execute(
        "INSERT OR IGNORE INTO devices VALUES(?,'phone','SYNTHETIC PHONE','active',1,NULL,NULL,NULL,?,?)",
        (device, "2026-09-01T00:00:00Z", "2026-09-01T00:00:00Z"),
    )
chat = {
    "evidence_id": "chat:synthetic",
    "source_type": "chat",
    "record_id": "synthetic-chat",
    "revision": revision,
    "sent_at": 1788220800,
    "platform": "qq",
    "speaker": "合成本人",
    "is_from_self": True,
    "text": f"我准备明天交第{revision}版材料",
    "context": {},
    "event_ids": [],
}
recording = {
    "evidence_id": "recording:synthetic",
    "source_type": "recording",
    "revision": 1,
    "session_id": sid,
    "utterance_id": stable_ulid("synthetic-utterance"),
    "start_ms": 100,
    "end_ms": 2000,
    "start_at": "2026-09-01T08:00:00Z",
    "end_at": "2026-09-01T08:00:02Z",
    "text": "我们讨论材料安排",
    "participant": {
        "key": "unknown",
        "kind": "unknown",
        "label": "身份未确认",
        "status": "unresolved",
    },
    "is_from_self": None,
    "event_ids": [],
}
inputs = {
    "version": "unified-daily-review-v1",
    "date": "2026-09-01",
    "timezone": "Asia/Singapore",
    "coverage": {
        "chat_records_in_day": 1,
        "chat_records_selected": 1,
        "chat_records_excluded": 0,
        "recording_events": 1,
        "recording_evidence": 1,
        "period_start": "2026-08-31T16:00:00Z",
        "period_end": "2026-09-01T16:00:00Z",
        "scope": "合成混合来源验收",
        "missing_sources": [],
    },
    "evidence": [chat, recording],
    "followups": [],
    "recording_events": [],
    "existing_recording_summary": {},
}


class Sources:
    def load(self, *args):
        return inputs


def model(value):
    if request.get("fail"):
        raise ValueError("synthetic provider failure")
    return {
        "headline": "当天回顾",
        "claims": [
            {
                "section": "followup",
                "text": chat["text"],
                "certainty": "fact",
                "perspective": "self",
                "event_ids": [],
                "evidence": [
                    {"evidence_id": chat["evidence_id"], "quote": chat["text"]}
                ],
            },
            {
                "section": "activity",
                "text": "录音讨论材料安排。",
                "certainty": "fact",
                "perspective": "observed",
                "event_ids": [],
                "evidence": [
                    {
                        "evidence_id": recording["evidence_id"],
                        "quote": recording["text"],
                    }
                ],
            },
        ],
        "limitations": ["录音身份未确认，不视为本人完成。"],
    }, {"model": "synthetic-zero-network"}


service = UnifiedReviewService(Sources(), factory, model, root / "receipts")
service.generate("2026-09-01", "Asia/Singapore")
sync = SyncRequest(PROJECTION_VERSION, request.get("cursor"), (), 100)
print(
    json.dumps(
        MobileSyncService(factory).synchronize(device, sync).as_dict(),
        ensure_ascii=False,
    )
)
