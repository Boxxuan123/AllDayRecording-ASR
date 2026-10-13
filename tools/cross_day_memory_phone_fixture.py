"""Fictional cross-day sync peer, explicitly restricted to temporary test state."""

from copy import deepcopy
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


class ArrivalSources(Sources):
    def freeze(self,first,last,*args):
        result=deepcopy(self.scope);result.update(first=first,last=last)
        result["manifest"]=self.selected(first)
        return result
    def selected(self,first):
        if first=='2026-10-05':return deepcopy(self.rows)
        row=deepcopy(self.rows[2]);row.update(id=stable_ulid('fictional-new-window'),title='新窗口独立互动')
        row['evidence'][0].update(record_id='new-window',reply_record_id=None,quote='新窗口讨论另一件独立事情')
        return [row]
    def load(self,scope):
        ids={r['id'] for r in scope['manifest']}
        return [r for r in self.selected(scope['first']) if r['id'] in ids]
    def increment(self, scope):
        updated = deepcopy(scope)
        updated["coverage"] = deepcopy(self.scope["coverage"])
        updated["manifest"]=self.selected(scope["first"])
        return updated

sources = ArrivalSources()
if request.get("arrival"):
    (root / "arrival").write_text("fictional", encoding="utf-8")
if (root / "arrival").exists():
    new = deepcopy(sources.rows[2])
    new["id"] = stable_ulid("fictional-new-arrival")
    new["title"] = "新互动：讨论独立的项目交付"
    new["evidence"][0].update(record_id="new-arrival", reply_record_id=None, quote="新聊天讨论项目交付，尚未确认完成")
    sources.rows.append(new)
    sources.scope["coverage"]["incremental"] = {"deferred_discussions": 2, "excluded_discussions": 1, "discussion_limit": 7}
if request.get("invalidate"):
    sources.rows[0]["evidence_status"] = "source_changed_or_missing"
    sources.rows[0]["evidence"][0]["status"] = "source_changed_or_missing"
service = CrossDayMemoryService(sources, factory, now=lambda: NOW)
if request.get('window'):
    (root/'window').write_text('fictional',encoding='utf-8')
first,last=('2026-10-08','2026-10-10') if (root/'window').exists() else ('2026-10-05','2026-10-07')
service.update(first,last,[sources.scope["contacts"][0]["id"]])
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
sync.memory_target_refresh=service.refresh_targets
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
