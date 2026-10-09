"""Runtime reports reuse the append-only audit store; no schema or version coupling."""
import json
from datetime import datetime, timezone

from allday_asr.build_info import diagnostics
from allday_asr.v3.domain.ids import new_ulid


class RuntimeDiagnosticsRepository:
    def __init__(self, database):
        self.database = database

    def report(self, device_id, payload):
        if not isinstance(payload, dict) or set(payload) != {"phone", "watch"}:
            raise ValueError("runtime report must contain phone and watch")
        phone = payload["phone"]
        if not isinstance(phone, dict) or phone.get("component") != "phone":
            raise ValueError("runtime report requires the reporting phone build")
        self._validate_build(phone)
        watch = payload["watch"]
        if not isinstance(watch, dict) or watch.get("state") not in {"unknown", "reported", "offline_or_stale"}:
            raise ValueError("invalid Watch report state")
        if watch.get("build") is not None:
            self._validate_build(watch["build"])
            if watch["build"].get("component") != "watch":
                raise ValueError("Watch identity must originate at Watch")
        last_seen = watch.get("last_seen", 0)
        if isinstance(last_seen, bool) or not isinstance(last_seen, (int, float)) or last_seen < 0:
            raise ValueError("invalid Watch observation time")
        now = datetime.now(timezone.utc).isoformat()
        # Metadata reported by a device is diagnostic evidence, never an admission rule.
        with self.database.transaction() as c:
            c.execute("INSERT INTO audit_entries "
                      "(audit_id,action,actor,target_type,target_id,details_json,created_at) "
                      "VALUES (?,?,?,?,?,?,?)",
                      (new_ulid(), "runtime.build.reported", "device:" + device_id,
                       "runtime_device", device_id, json.dumps(payload), now))
        return {"status": "recorded", **diagnostics(self.database.schema_version())}

    @staticmethod
    def _validate_build(info):
        if not isinstance(info, dict):
            raise ValueError("build must be an object")
        for field in ("component", "release_version", "git_commit"):
            if not isinstance(info.get(field), str) or not 1 <= len(info[field]) <= 64:
                raise ValueError("invalid build identity")
        if info.get("dirty") not in (True, False, None):
            raise ValueError("invalid dirty marker")
        if len(json.dumps(info)) > 2048:
            raise ValueError("build identity exceeds limit")

    def snapshot(self):
        now = datetime.now(timezone.utc)
        with self.database.read() as c:
            rows = c.execute("SELECT target_id, details_json, created_at FROM audit_entries "
                             "WHERE target_type='runtime_device' AND action='runtime.build.reported' "
                             "ORDER BY created_at DESC, audit_id DESC LIMIT 200").fetchall()
        devices = {}
        for row in rows:
            if row["target_id"] in devices:
                continue
            report = json.loads(row["details_json"])
            seen = datetime.fromisoformat(row["created_at"])
            report.update(reported_at=row["created_at"],
                          state="reported" if (now-seen).total_seconds() <= 120 else "offline_or_stale")
            watch = report["watch"]
            if watch.get("last_seen", 0) < now.timestamp()*1000-120000 and watch.get("build"):
                watch["state"] = "offline_or_stale"
            devices[row["target_id"]] = report
        return {**diagnostics(self.database.schema_version()),
                "devices": devices, "unreported_devices": "unknown"}
