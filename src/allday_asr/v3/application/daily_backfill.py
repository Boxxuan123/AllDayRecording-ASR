"""A sealed finite pass, one durable invocation marker per manifest date."""

import json
import os
from pathlib import Path

from allday_asr.v3.domain.hashing import canonical_json_sha256
from .file_lock import day_worker_lock


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, default=str)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


class DailyBackfillPass:
    def __init__(self, coordinator, manifest, results_path):
        self.coordinator = coordinator
        self.manifest = manifest
        self.path = Path(results_path)
        self._check_manifest()
        self.document = (
            json.loads(self.path.read_text(encoding="utf-8"))
            if self.path.exists()
            else {
                "manifest_sha256": manifest["sha256"],
                "BACKFILL_PASS_COMPLETE": False,
                "days": [
                    {"date": i["date"], "status": "NOT_ATTEMPTED", "invocations": 0}
                    for i in manifest["days"]
                ],
            }
        )
        if self.document["manifest_sha256"] != manifest["sha256"]:
            raise ValueError("Backfill journal belongs to another manifest")

    def _check_manifest(self):
        days = self.manifest["days"]
        if self.manifest["sha256"] != canonical_json_sha256(days):
            raise ValueError("Frozen manifest checksum mismatch")
        dates = [i["date"] for i in days]
        if len(set(dates)) != len(dates) or dates != sorted(dates, reverse=True):
            raise ValueError("Frozen manifest must contain unique dates newest first")

    def run_day(self, date):
        with day_worker_lock(self.path) as acquired:
            if not acquired:
                raise RuntimeError(
                    "Frozen backfill journal is owned by another coordinator"
                )
            self._reload()
            return self._run_day(date)

    def _reload(self):
        if self.path.exists():
            self.document = json.loads(self.path.read_text(encoding="utf-8"))
        if self.document["manifest_sha256"] != self.manifest["sha256"]:
            raise ValueError("Backfill journal belongs to another manifest")

    def _run_day(self, date):
        self._check_manifest()
        item = next(i for i in self.manifest["days"] if i["date"] == date)
        record = next(r for r in self.document["days"] if r["date"] == date)
        if record["invocations"] or record["status"] != "NOT_ATTEMPTED":
            return record
        current = next(
            (i for i in self.coordinator.inventory.scan() if i["date"] == date), None
        )
        if (
            not current
            or current["source_fingerprint"] != item["source_fingerprint"]
            or current["generation_version"] != item["generation_version"]
        ):
            record["status"] = "NOT_ATTEMPTED_SOURCE_CHANGED"
            atomic_json(self.path, self.document)
            return record
        record.update(status="STARTED", invocations=1)
        atomic_json(
            self.path, self.document
        )  # Crash must not silently start this day twice.
        try:
            result = self.coordinator.run_once(allowed_dates={date}, backfill=True)
            if result is None:
                raise RuntimeError(
                    "Frozen backfill job unavailable or another day worker owns the database"
                )
            record.update(
                result, status="SUCCESS" if result["generation_completed"] else "FAILED"
            )
            if not result["generation_completed"] and item["existing_summary_present"]:
                record["existing_result"] = "UPGRADE_FAILED_EXISTING_RESULT_PRESERVED"
        except Exception as exc:
            record.update(status="INFRASTRUCTURE_ERROR", error=repr(exc))
            atomic_json(self.path, self.document)
            raise
        atomic_json(self.path, self.document)
        return record

    def run(self, *, after_day=None):
        with day_worker_lock(self.path) as acquired:
            if not acquired:
                raise RuntimeError(
                    "Frozen backfill journal is owned by another coordinator"
                )
            self._reload()
            return self._run(after_day=after_day)

    def _run(self, *, after_day=None):
        for item in self.manifest["days"]:
            if self.document.get("stop_reason"):
                break
            record = self._run_day(item["date"])
            completed = [
                r for r in self.document["days"] if r["status"] in ("SUCCESS", "FAILED")
            ]
            tail = completed[-3:]
            kind = record.get("first_request_infrastructure_failure")
            if (
                kind
                and len(tail) == 3
                and all(
                    r.get("first_request_infrastructure_failure") == kind for r in tail
                )
            ):
                self.document["stop_reason"] = "PROVIDER_GLOBALLY_UNAVAILABLE"
                for pending in self.document["days"]:
                    if pending["status"] == "NOT_ATTEMPTED":
                        pending["status"] = "NOT_ATTEMPTED_PROVIDER_OUTAGE"
                pending_dates = {
                    r["date"]
                    for r in self.document["days"]
                    if r["status"] == "NOT_ATTEMPTED_PROVIDER_OUTAGE"
                }
                queue = getattr(self.coordinator, "queue", None)
                if queue is not None:
                    queue.hold_provider_outage(
                        [i for i in self.manifest["days"] if i["date"] in pending_dates]
                    )
                atomic_json(self.path, self.document)
            if after_day is not None:
                after_day(record, self.document)
        self.document["BACKFILL_PASS_COMPLETE"] = all(
            r["status"] not in ("NOT_ATTEMPTED", "STARTED")
            for r in self.document["days"]
        ) and not self.document.get("stop_reason")
        atomic_json(self.path, self.document)
        return self.document
