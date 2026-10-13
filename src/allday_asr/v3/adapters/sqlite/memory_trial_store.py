"""SQLite details for the existing trial; no additional queue or state machine."""

from datetime import datetime
from zoneinfo import ZoneInfo
from allday_asr.v3.ports.memory_trial import TRIAL_KEY


class SqliteMemoryTrialStore:
    def __init__(self, cache, database):
        self.cache, self.database = cache, database

    def ensure_source_baseline(self, run):
        changed = False
        with self.cache.connect() as c:
            for sid in run["scope_ids"]:
                if run["prior_enabled"][sid]:
                    continue
                baseline = run["baseline_cursors"].get(sid, {})
                row = c.execute("SELECT * FROM scopes WHERE id=?", (sid,)).fetchone()
                if (
                    row
                    and row["phase"] == "snapshot"
                    and row["received"] == 0
                    and row["boundary"] == baseline.get("boundary")
                    and row["boundary"]
                ):
                    c.execute(
                        "UPDATE scopes SET phase='changes',receive_cursor=boundary,page_cursor=NULL,error=NULL WHERE id=?",
                        (sid,),
                    )
                    changed = True
            if changed:
                run["initial_snapshot_policy"] = (
                    "verified boundary only; no startup historical import"
                )
                self.cache._put(c, TRIAL_KEY, run)

    def prepare(self, selected, day, end):
        scopes = []
        baseline_records = 0
        with self.cache.connect() as c:
            for p in selected:
                rows = c.execute(
                    "SELECT DISTINCT conversation FROM search_index WHERE platform=? AND source=? AND sender=? AND json_extract(body,'$.conversation_type')='c2c' LIMIT 2",
                    (p["platform"], p["source_account_id"], p["account_id"]),
                ).fetchall()
                if len(rows) != 1:
                    raise ValueError("existing direct conversation must be unique")
                scopes.append(
                    {
                        "platform": p["platform"],
                        "source_account_id": p["source_account_id"],
                        "conversation_id": rows[0][0],
                        "sent_from": int(
                            datetime.combine(
                                day, datetime.min.time(), ZoneInfo("Asia/Singapore")
                            ).timestamp()
                        ),
                        "sent_to": int(end) - 1,
                    }
                )
                baseline_records += c.execute(
                    "SELECT count(*) FROM search_index WHERE platform=? AND source=? AND conversation=?",
                    (p["platform"], p["source_account_id"], rows[0][0]),
                ).fetchone()[0]
            baseline_task = c.execute(
                "SELECT coalesce(max(id),0) FROM tasks"
            ).fetchone()[0]
        return scopes, baseline_records, baseline_task

    def receipts(self, run):
        with self.cache.connect() as c:
            latest = c.execute(
                "SELECT max(json_extract(r.body,'$.sent_at')) FROM tasks t JOIN records r ON r.dataset=t.dataset AND r.id=t.record_id WHERE t.memory_trial=? AND t.memory_class='live'",
                (run["id"],),
            ).fetchone()[0]
            times = [
                dict(r)
                for r in c.execute(
                    "SELECT t.record_id,t.revision,t.memory_class,t.received_at,t.memory_committed_at,t.memory_completed_at,t.memory_revision,json_extract(r.body,'$.sent_at') AS message_at FROM tasks t JOIN records r ON r.dataset=t.dataset AND r.id=t.record_id WHERE t.memory_trial=? ORDER BY t.id DESC LIMIT 20",
                    (run["id"],),
                )
            ]
        return latest, times

    def phone_receipts(self, run, times):
        with self.database.read() as c:
            from allday_asr.v3.domain.cross_day_memory import INDEX_ID

            sequence = c.execute(
                "SELECT max(sequence) FROM change_events WHERE resource_type='memory_index' AND resource_id=? AND revision=?",
                (INDEX_ID, run["last_memory_revision"]),
            ).fetchone()[0]
            phone_sync = [
                {
                    **dict(r),
                    "acknowledged_current_memory": sequence is not None
                    and r["acknowledged_sequence"] >= sequence,
                }
                for r in c.execute(
                    "SELECT acknowledged_sequence,updated_at FROM sync_cursors ORDER BY updated_at DESC LIMIT 3"
                )
            ]
            receipt_sequences = {
                r["memory_revision"]: c.execute(
                    "SELECT max(sequence) FROM change_events WHERE resource_type='memory_index' AND resource_id=? AND revision=?",
                    (INDEX_ID, r["memory_revision"]),
                ).fetchone()[0]
                for r in times
            }
        return sequence, phone_sync, receipt_sequences
