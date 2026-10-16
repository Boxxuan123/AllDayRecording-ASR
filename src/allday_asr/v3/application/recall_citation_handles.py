"""Query-local immutable short handles; only the current input set may be cited."""
from copy import deepcopy
import json


class CitationHandles:
    def __init__(self, result):
        self.mapping = result.setdefault("_citation_handles", {})
        self.allowed = {}

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
        for action in value.get("actions", []):
            action["evidence_ids"] = [evidence(c) for c in action["evidence_ids"]]
            action["targets"] = [evidence(t) if t.startswith("E") or t.startswith("chat:") else t for t in action["targets"]]
        return value

    def receipt(self, rows):
        return {"handles": dict(self.allowed), "records": len(rows),
                "bytes": len(json.dumps(rows, ensure_ascii=False).encode())}
