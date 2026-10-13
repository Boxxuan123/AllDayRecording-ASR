"""Explicit bounded cache projection, reused by Desktop and the existing sync owner."""

from datetime import datetime, timezone
from copy import deepcopy
from allday_asr.v3.domain.ids import stable_ulid
from allday_asr.v3.domain.cross_day_memory import INDEX_ID, project
from allday_asr.v3.domain.memory_content import content
from allday_asr.v3.domain.models import CorrectionOperation
from allday_asr.v3.domain.ids import new_ulid


class CrossDayMemoryService:
    def __init__(self, sources, uow_factory, now=None):
        self.sources, self.uow_factory = sources, uow_factory
        self.now = now or (lambda: datetime.now(timezone.utc))

    def inventory(self, first, last, timezone="Asia/Singapore"):
        return self.sources.inventory(first, last, timezone)

    def update(self, first, last, contacts, timezone="Asia/Singapore", continue_pending=False, batch_id=None):
        if (
            not isinstance(contacts, list)
            or not 1 <= len(contacts) <= 3
            or len(set(contacts)) != len(contacts)
        ):
            raise ValueError("select one to three distinct contacts")
        with self.uow_factory().reading() as uow:
            scope, old = uow.cross_day_index.current()
            histories = uow.cross_day_index.history()
            if batch_id and uow.cross_day_index.has_batch(batch_id):
                return memory_page(old)
        if continue_pending and (not isinstance(batch_id,str) or not 1<=len(batch_id)<=80):
            raise ValueError("continuation requires a stable batch ID")
        if scope and [
            scope["first"],
            scope["last"],
            scope["timezone"],
            [p["id"] for p in scope["contacts"]],
        ] == [first, last, timezone, contacts]:
            last_batch=scope.get("_batch_id")
            scope = deepcopy(scope)
            scope["_continue"] = continue_pending
            scope = self.sources.increment(scope)
            archive={r["id"]:r for r in old.get("_archive",[])}
            for hs,hv in histories:
                if window_key(hs)==window_key(scope): continue
                for row in hv["discussions"]:
                    archive.setdefault(row["id"],{**row,"window_label":hs["first"]+" 至 "+hs["last"]})
            scope["_archive"] = list(archive.values())
            scope.pop("_continue",None)
            scope["_history_scopes"] = [[hs,hv] for hs,hv in histories if window_key(hs)!=window_key(scope)]
            scope["_batch_id"] = batch_id or last_batch
            self._publish(scope, self.sources.load(scope), old["revision"])
            return self.get()
        previous=next((hs for hs,hv in histories if [hs["first"],hs["last"],hs["timezone"],[p["id"] for p in hs["contacts"]]]==[first,last,timezone,contacts]),None)
        if previous:
            previous=deepcopy(previous);previous["_continue"]=continue_pending
            scope=self.sources.increment(previous);scope.pop("_continue",None)
        else: scope = self.sources.freeze(first, last, timezone, contacts)
        active=window_key(scope)
        archive={}
        for saved_scope,saved_value in histories:
            if window_key(saved_scope)==active: continue
            for row in saved_value["discussions"]:
                archive[row["id"]]={**archive.get(row["id"],{}),**row,"window_label":saved_scope["first"]+" 至 "+saved_scope["last"]}
        scope["_archive"]=list(archive.values())
        scope["_history_scopes"]=[[hs,hv] for hs,hv in histories if window_key(hs)!=active]
        scope["_batch_id"]=batch_id
        self._publish(scope,self.sources.load(scope),old["revision"] if old else 0)
        return self.get()

    def get(self, page=0, discussion=None, evidence_page=0):
        with self.uow_factory().reading() as uow:
            scope, old = uow.cross_day_index.current()
        if not scope:
            return {
                "status": "not_selected",
                "id": INDEX_ID,
                "revision": 0,
                "people": [],
                "discussions": [],
                "things": [],
                "topics": [],
                "candidates": [],
            }
        self._publish(scope, self.sources.load(scope), old["revision"])
        with self.uow_factory().reading() as uow:
            _,current=uow.cross_day_index.current()
        targets=[(c["source"],c["target"]) for c in current["candidates"][:7]]
        if discussion: targets.append((discussion,discussion))
        elif int(page)>0:
            targets.extend((r["id"],r["id"]) for r in memory_page(current,int(page))["discussions"])
        for source,target in targets: self.refresh_targets(source,target)
        with self.uow_factory().reading() as uow:
            _,current=uow.cross_day_index.current()
        return memory_page(current,int(page),discussion,int(evidence_page))

    def refresh_saved(self):
        return self.get()

    def _publish(self, scope, rows, expected):
        with self.uow_factory() as uow:
            current_scope, old = uow.cross_day_index.current()
            if (old["revision"] if old else 0) != expected:
                raise ValueError("memory input changed; reload before updating")
            if self.sources.load(scope) != rows:
                raise ValueError(
                    "memory sources changed during publication; retry bounded update"
                )
            archive=scope.get("_archive",[])
            all_contacts={p["id"]:p for r in rows+archive for p in r["contacts"]}
            combined_scope={**scope,"contacts":list(all_contacts.values())}
            value = project(combined_scope, rows+archive, uow.cross_day_index.decisions())
            value["scope"]={k:v for k,v in scope.items() if k!="manifest" and not k.startswith("_")}
            value["_archive"] = archive
            value["_active_ids"] = [r["id"] for r in rows]
            value["status"] = "ready" if value["discussions"] else "no_data"
            if (
                old
                and {
                    k: v for k, v in old.items() if k not in {"revision", "updated_at"}
                }
                == value
                and current_scope == scope
            ):
                return memory_page(old)
            value["revision"], value["updated_at"] = (
                expected + 1,
                self.now().astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            )
            uow.cross_day_index.save(scope, value, value["updated_at"])
            publish_pages(uow,value)
            return memory_page(value)

    def decide(
        self,
        source,
        target,
        action,
        expected_revision,
        actor="desktop-user",
        operation_id=None,
    ):
        self.get()  # Validate active originals before accepting any human association.
        self.refresh_targets(source,target)
        with self.uow_factory() as uow:
            return apply_decision(
                uow,
                source,
                target,
                action,
                expected_revision,
                self.now(),
                actor,
                operation_id,
            )

    def refresh_targets(self, source, target):
        with self.uow_factory().reading() as uow:
            scope,old=uow.cross_day_index.current()
        if not scope: return
        archive={r["id"]:r for r in scope.get("_archive",[])}
        changed=False
        for saved_scope,_ in scope.get("_history_scopes",[]):
            ids={r["id"] for r in saved_scope["manifest"]}
            wanted=ids & {source,target}
            if not wanted: continue
            selected={**saved_scope,"manifest":[r for r in saved_scope["manifest"] if r["id"] in wanted]}
            for row in self.sources.load(selected):
                if row["id"] in wanted and row["id"] in archive:
                    # Archive rows already contain projected titles. Normalize a
                    # refreshed source row before comparison so unchanged evidence
                    # cannot alternate raw/projected labels and consume a revision.
                    row=deepcopy(row)
                    row["title"],row["summary"]=content({**row,"evidence":[e for e in row["evidence"] if e.get("role","core")=="core"]})
                    archive[row["id"]]={**archive.get(row["id"],{}),**row,"window_label":saved_scope["first"]+" 至 "+saved_scope["last"]}
                    changed=True
        if changed:
            scope["_archive"]=list(archive.values())
            self._publish(scope,self.sources.load(scope),old["revision"])

def window_key(scope):
    return (scope["first"],scope["last"],scope["timezone"],tuple(p["id"] for p in scope["contacts"]))

def memory_page(value,page=0,discussion=None,evidence_page=0):
    result=deepcopy(value)
    rows=value["discussions"]
    if page<0 or evidence_page<0: raise ValueError("invalid memory page")
    active=set(value.get("_active_ids",[r["id"] for r in rows]))
    windows={"current":[r for r in rows if r["id"] in active]}
    for row in rows:
        if row["id"] not in active: windows.setdefault(row.get("window_label","history"),[]).append(row)
    pages=[chunk[i:i+7] for chunk in windows.values() for i in range(0,len(chunk),7)] or [[]]
    if not windows["current"] and len(windows)>1: pages.insert(0,[])
    chosen=[r for r in rows if r["id"]==discussion] if discussion else (pages[page] if page<len(pages) else [])
    if discussion and not chosen: raise ValueError("discussion unavailable")
    if discussion and evidence_page*8>=len(chosen[0]["evidence"]): raise ValueError("evidence page unavailable")
    result["discussions"]=deepcopy(chosen)
    for row in result["discussions"]:
        row["evidence_total"]=len(row["evidence"])
        row["evidence_page"]=evidence_page if discussion else 0
        start=row["evidence_page"]*8
        row["evidence"]=row["evidence"][start:start+8]
    ids={r["id"] for r in chosen}
    result["people"]=[{**p,"discussion_ids":[i for i in p["discussion_ids"] if i in ids]} for p in value["people"] if any(i in ids for i in p["discussion_ids"])][:3]
    result["things"]=[{**t,"discussion_ids":[i for i in t["discussion_ids"] if i in ids]} for t in value["things"] if any(i in ids for i in t["discussion_ids"])][:7]
    result["topics"]=[{**t,"discussion_ids":[i for i in t["discussion_ids"] if i in ids]} for t in value["topics"] if any(i in ids for i in t["discussion_ids"])][:3]
    result["candidates"]=[c for c in value["candidates"] if {c["source"],c["target"]}<=ids][:21]
    result["page"]={"number":page,"total":len(pages),"discussion_total":len(rows),"kind":"evidence" if discussion else "discussions","discussion_id":discussion}
    result={k:v for k,v in result.items() if not k.startswith("_")}
    if page or discussion:
        result["id"]="7"+stable_ulid("memory-page",page,discussion,evidence_page)[1:]
    return result

def publish_pages(uow,value):
    for page in range(1,memory_page(value)["page"]["total"]):
        item=memory_page(value,page)
        uow.changes.append("memory_index",item["id"],value["revision"],"upsert",item)
    for row in value["discussions"]:
        for page in range(1,(len(row["evidence"])+7)//8):
            item=memory_page(value,0,row["id"],page)
            uow.changes.append("memory_index",item["id"],value["revision"],"upsert",item)
    item=memory_page(value)
    uow.changes.append("memory_index",INDEX_ID,value["revision"],"upsert",item)


def apply_decision(
    uow, source, target, action, expected_revision, now, actor, operation_id=None
):
    scope, old = uow.cross_day_index.current()
    if (
        old is None
        or type(expected_revision) is not int
        or expected_revision != old["revision"]
    ):
        raise ValueError("memory revision conflict; synchronize and reload")
    rows = {r["id"]: r for r in old["discussions"]}
    if (
        action not in {"link", "reject", "detach"}
        or source not in rows
        or target not in rows
        or source == target
    ):
        raise ValueError("invalid memory association")
    if action == "link":
        if (
            rows[source]["evidence_status"] != "valid"
            or rows[target]["evidence_status"] != "valid"
        ):
            raise ValueError("invalid evidence cannot confirm an association")
        if rows[source]["thing_id"] == rows[target]["thing_id"]:
            return memory_page(old)
        # Only move an independent record, never a whole merged group implicitly.
        if sum(r["thing_id"] == rows[source]["thing_id"] for r in rows.values()) > 1:
            raise ValueError("detach this record before moving it to another thing")
    decisions = uow.cross_day_index.decisions()
    if any(
        d == {"source": source, "target": target, "action": action} for d in decisions
    ):
        return memory_page(old)
    uow.cross_day_index.decide(source, target, action)
    merged_scope={**scope,"contacts":list({p["id"]:p for r in rows.values() for p in r["contacts"]}.values())}
    value = project(merged_scope, list(rows.values()), uow.cross_day_index.decisions())
    value["scope"]=old["scope"]
    value["_archive"]=old.get("_archive",[])
    value["_active_ids"]=old.get("_active_ids",list(rows))
    value["excluded"] = old.get("excluded", [])
    value.update(
        status=old["status"], revision=old["revision"] + 1, updated_at=now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    uow.cross_day_index.save(scope, value, value["updated_at"])
    uow.corrections.add(
        CorrectionOperation(
            operation_id or new_ulid(),
            "memory_index",
            INDEX_ID,
            expected_revision,
            {"source": source, "target": target, "action": action},
            actor,
            now,
            None,
        )
    )
    publish_pages(uow,value)
    return memory_page(value)


def apply_memory_operation(operation, uow, now):
    from allday_asr.v3.domain.device_sync import OperationReceipt, ClientOperationStatus

    try:
        if set(operation.payload) != {"source", "target", "action"}:
            raise ValueError("invalid memory command")
        with uow.savepoint():
            value = apply_decision(
                uow,
                **operation.payload,
                expected_revision=operation.base_revision,
                now=now,
                actor="phone-user",
                operation_id=operation.operation_id,
            )
    except (ValueError, KeyError, TypeError):
        return OperationReceipt(
            operation.operation_id,
            ClientOperationStatus.CONFLICT,
            None,
            {
                "code": "MEMORY_REVISION_CONFLICT",
                "message": "关联或证据已变化；请同步后核对，原人工决定保留",
                "retryable": False,
            },
        )
    return OperationReceipt(
        operation.operation_id,
        ClientOperationStatus.APPLIED,
        value["revision"],
        None,
        ({"resource_id": INDEX_ID, "revision": value["revision"]},),
    )
