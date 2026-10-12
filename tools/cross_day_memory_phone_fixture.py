"""Fictional cross-day sync peer, explicitly restricted to temporary test state."""

import json
import sys
from pathlib import Path
from tests.test_cross_day_memory_v1 import Sources, NOW
from allday_asr.v3.adapters.sqlite import V3Database, SqliteUnitOfWork
from allday_asr.v3.application.cross_day_memory import CrossDayMemoryService
from allday_asr.v3.application.mobile_sync import (
    MobileSyncService,
    UtteranceCorrectionOperationHandler,
)
from allday_asr.v3.domain.device_sync import (
    SyncRequest,
    ClientOperation,
    PROJECTION_VERSION,
)
from allday_asr.v3.domain.ids import stable_ulid

root = Path(sys.argv[1])
if not root.parent.name.startswith("cross-day-phone-"):
    raise ValueError("temporary fictional state required")
root.mkdir(parents=True, exist_ok=True)
request = json.load(sys.stdin)
db = V3Database.open(root / "core.sqlite3")


def factory():
    return SqliteUnitOfWork(db)


sources = Sources()
if request.get("invalidate"):
    sources.rows[0]["evidence_status"] = "source_changed_or_missing"
    sources.rows[0]["evidence"][0]["status"] = "source_changed_or_missing"
service = CrossDayMemoryService(sources, factory, now=lambda: NOW)
service.update("2026-10-05", "2026-10-07", [sources.scope["contacts"][0]["id"]])
device = stable_ulid("fictional-phone")
with db.transaction() as c:
    c.execute(
        "INSERT OR IGNORE INTO devices VALUES(?,'phone','FICTIONAL','active',1,NULL,NULL,NULL,?,?)",
        (device, NOW.isoformat(), NOW.isoformat()),
    )
sync = MobileSyncService(
    factory,
    operation_handler=UtteranceCorrectionOperationHandler(now=lambda: NOW),
    now=lambda: NOW,
)
sync.memory_refresh = service.refresh_saved
ops = tuple(
    ClientOperation(v["operation_id"], v["kind"], v["base_revision"], v["payload"])
    for v in request.get("operations", [])
)
print(
    json.dumps(
        sync.synchronize(
            device, SyncRequest(PROJECTION_VERSION, request.get("cursor"), ops, 500)
        ).as_dict(),
        ensure_ascii=False,
    )
)
