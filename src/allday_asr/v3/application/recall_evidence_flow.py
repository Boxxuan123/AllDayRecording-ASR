"""Bounded source-ID diagnostics, never private text or model reasoning."""


class EvidenceFlow:
    def __init__(self, result):
        self.seen = {}
        self.value = {
            "counts": {
                k: 0
                for k in (
                    "source_returned",
                    "remote_returned",
                    "cache_returned",
                    "application_received",
                    "candidate_seen",
                    "application_accepted",
                    "hard_rejected",
                    "soft_rejected",
                    "soft_terms_not_matched",
                    "candidate",
                    "model_selected",
                )
            },
            "observations": {},
            "events": [],
            "dropped_events": 0,
            "model_rounds": {},
            "stop_reason": "running",
            "gaps": [],
        }
        result["evidence_flow"] = self.value

    def record(self, stage, row, reason, relations=()):
        identity = str(row.get("record_id", row.get("id", "unknown")))[:200]
        revision = row.get("record_revision", row.get("revision", 0))
        key = (
            row.get("platform", row.get("source", "")),
            row.get("source_account_id", ""),
            identity,
            revision,
        )
        seen = self.seen.setdefault(stage, set())
        seen.add(key)
        self.value["counts"][stage] = len(seen)
        self.value["observations"][stage] = self.value["observations"].get(stage, 0) + 1
        event = {
            "stage": stage,
            "source_id": identity,
            "revision": revision,
            "reason": reason,
            "relations": list(relations)[:6],
        }
        if len(self.value["events"]) < 600:
            self.value["events"].append(event)
        else:
            self.value["dropped_events"] += 1

    def received(self, rows, origin, reason):
        for row in rows:
            self.record("source_returned", row, reason)
            self.record(origin + "_returned", row, reason)
            self.record("application_received", row, reason)

    def selected(self, rows, round_number):
        self.value["model_rounds"][str(round_number)] = [
            r["record_id"] if "record_id" in r else r["id"] for r in rows
        ]
        self.value["final_input_count"] = len(rows)
        for row in rows:
            self.record("model_selected", row, "bounded_evidence_selection")

    def gap(self, reason):
        if reason not in self.value["gaps"]:
            self.value["gaps"].append(reason)
