"""Query-local immutable short handles; only the current input set may be cited."""
from copy import deepcopy
import json


class CitationHandles:
    def __init__(self, result):
        self.mapping = result.setdefault("_citation_handles", {})
        self.allowed = {}
        self.object_mapping = result.setdefault("_object_handles", {})
        self.object_allowed = {}

    def object(self, value):
        native = value["ref"]
        existing = next((h for h,r in self.object_mapping.items() if r==native),None)
        if existing is None:
            prefix = "A" if value["type"]=="account" else "C"
            existing = prefix + str(1+sum(h.startswith(prefix) for h in self.object_mapping))
            self.object_mapping[existing] = native
        self.object_allowed[existing] = native
        return existing

    def encode(self, rows):
        reverse = {tuple(v): k for k, v in self.mapping.items()}
        encoded = []
        for row in rows:
            identity = (row["id"], row.get("dataset"), row["revision"])
            handle = reverse.get(identity)
            if handle is None:
                handle = "E" + str(len(self.mapping) + 1)
                self.mapping[handle] = list(identity)
                reverse[identity] = handle
            self.allowed[handle] = row["id"]
            encoded.append({k: deepcopy(v) for k, v in row.items() if k not in {
                "identity_key", "reply_record_id", "quote_record_id"
            }} | {"id": handle})
        return encoded

    def validate(self, rows, originals):
        for row in rows:
            full = self.allowed.get(row["id"])
            original = originals.get(full)
            if not original or self.mapping[row["id"]] != [full,original.get("dataset"),original["revision"]]:
                raise ValueError("input handle mapping differs from authorized evidence")
            if any(row.get(k) != original.get(k) for k in (
                "record_id","revision","dataset","source","source_account_id",
                "conversation_id","sender_account_id","sent_at","quote")):
                raise ValueError("input evidence differs from authorized original")

    def decode(self, value):
        value = deepcopy(value)
        def evidence(name):
            if name not in self.allowed:
                raise ValueError("citation/operand was not in this round's model input: " + str(name)[:80])
            return self.allowed[name]
        for claim in value.get("claims", []):
            claim["citations"] = [evidence(c) for c in claim["citations"]]
            if claim.get("event_time", {}).get("evidence_id"):
                claim["event_time"]["evidence_id"] = evidence(claim["event_time"]["evidence_id"])
        for candidate in value.get("candidates", []):
            candidate["evidence_ids"] = [evidence(c) for c in candidate["evidence_ids"]]
            key = candidate["contact_key"]
            if key not in self.object_allowed or not self.object_allowed[key].startswith("contact:"):
                raise ValueError("candidate was not supplied in this model input")
            candidate["contact_key"] = self.object_allowed[key].removeprefix("contact:")
        for action in value.get("actions", []):
            action["evidence_ids"] = [evidence(c) for c in action["evidence_ids"]]
            resolved = []
            for target in action["targets"]:
                if target.startswith("$"): resolved.append(target)
                elif target in self.object_allowed: resolved.append(self.object_allowed[target])
                elif target.startswith("E") or target.startswith("chat:"): resolved.append(evidence(target))
                else: raise ValueError("object operand was not supplied in this model input: " + target[:80])
            action["targets"] = resolved
        return value

    def receipt(self, rows):
        return {"handles": dict(self.allowed), "object_handles":dict(self.object_allowed), "records": len(rows),
                "bytes": len(json.dumps(rows, ensure_ascii=False).encode())}
