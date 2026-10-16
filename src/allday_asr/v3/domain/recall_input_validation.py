"""Validate the exact outbound evidence payload before the model executor is called."""
from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo
import re


def prepare_payload(payload):
    value = deepcopy(payload)
    timezone = value["scope"]["timezone"]
    def visit(item):
        if isinstance(item,dict):
            if all(k in item for k in ("sent_from","sent_to")):
                zone = ZoneInfo(item.get("timezone",timezone))
                if not 0 <= item["sent_from"] <= item["sent_to"]:
                    raise ValueError("invalid input window")
                for field in ("sent_from","sent_to"):
                    actual = datetime.fromtimestamp(item[field],zone).isoformat()
                    if field+"_local" in item and item[field+"_local"] != actual:
                        raise ValueError("inconsistent input window: " + field)
                    item[field+"_local"] = actual
            for v in item.values(): visit(v)
        elif isinstance(item,list):
            for v in item: visit(v)
    visit(value)
    rows = value["records"]
    if len(rows)>48 or len({r["id"] for r in rows}) != len(rows):
        raise ValueError("invalid input evidence count/identity")
    local_now = value.get("local_now")
    cutoff = int(datetime.fromisoformat(local_now).timestamp()) if local_now else value["scope"]["sent_to"]
    handles = {r["id"] for r in rows}
    for row in rows:
        if not re.fullmatch(r"E[1-9]\d*",row["id"]) or not row.get("revision",0)>0:
            raise ValueError("invalid input evidence handle/revision")
        if not isinstance(row.get("quote"),str) or row["sent_at"]>cutoff:
            raise ValueError("invalid/future input evidence")
        expected = datetime.fromtimestamp(row["sent_at"],ZoneInfo(timezone)).isoformat()
        if row.get("sent_at_local",expected)!=expected:
            raise ValueError("inconsistent input message timestamp")
        row["sent_at_local"] = expected
    for obj in value.get("objects",[]):
        for key in ("citations","evidence_ids","relation_evidence"):
            if any(h not in handles for h in obj.get(key,[])):
                raise ValueError("object refers to unpresented evidence")
    for step in value.get("steps",[]):
        for key in ("record_refs","support"):
            if any(h not in handles for h in step.get(key,[])):
                raise ValueError("step refers to unpresented evidence")
    # Model-facing operands contain only query-local handles. Full source IDs remain
    # in the server's exact evidence/object maps, not as competing tool operands.
    for row in rows:
        row.pop("record_id",None)
        if row.get("conversation_ref"):
            row["conversation_id"] = row["conversation_ref"]
    for obj in value.get("objects",[]):
        obj.pop("native_ref",None)
        obj.pop("key",None)
        obj.pop("conversation_id",None)
    return value
