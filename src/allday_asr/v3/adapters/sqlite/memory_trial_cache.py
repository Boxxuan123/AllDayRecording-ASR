"""Trial receipts extend the existing durable chat tasks, never another sync queue."""
import json, time

from allday_asr.v3.ports.memory_trial import TRIAL_KEY

class MemoryTrialCache:
    def initialize_trial_columns(self, db):
        existing={r[1] for r in db.execute("PRAGMA table_info(tasks)")}
        for name,kind in {"received_at":"REAL","memory_trial":"TEXT","memory_class":"TEXT","memory_state":"TEXT","memory_completed_at":"REAL","memory_revision":"INTEGER","memory_cursor":"TEXT","memory_eligible":"INTEGER","memory_committed_at":"REAL"}.items():
            if name not in existing: db.execute(f"ALTER TABLE tasks ADD COLUMN {name} {kind}")
        db.execute("CREATE INDEX IF NOT EXISTS idx_chat_memory_trial ON tasks(memory_trial,memory_state,id)")

    def tag_trial_receipt(self, db, dataset, record, revision, operation, old, scope_id, progress, changes):
        saved=db.execute("SELECT value FROM metadata WHERE key=?",(TRIAL_KEY,)).fetchone()
        if not saved: return
        trial=json.loads(saved[0]); now=time.time()
        if trial['state']!='running' or now>=trial['ends_at'] or scope_id not in trial['scope_ids'] or dataset!=trial['dataset']: return
        previous=db.execute("SELECT 1 FROM tasks WHERE dataset=? AND record_id=? AND memory_trial=? AND memory_eligible=1 LIMIT 1",(dataset,record['record_id'],trial['id'])).fetchone()
        scope=db.execute('SELECT phase,receive_cursor FROM scopes WHERE id=?',(scope_id,)).fetchone()
        cursor_proof=bool(changes and progress and scope and scope['phase']=='changes' and scope['receive_cursor'] and trial['baseline_cursors'].get(scope_id,{}).get('boundary'))
        live=cursor_proof and not old and isinstance(record.get('sent_at'),int) and trial['started_at']<=record['sent_at']<trial['ends_at']
        kind='revision' if old else 'live' if live else 'backfill'
        eligible=1 if live or (old is not None and previous is not None) else 2 if old is not None else 0
        db.execute("UPDATE tasks SET received_at=?,memory_trial=?,memory_class=?,memory_state=?,memory_cursor=?,memory_eligible=? WHERE dataset=? AND record_id=? AND revision=? AND operation=? AND memory_trial IS NULL",(now,trial['id'],kind,'pending' if eligible else 'ignored_history',json.dumps({'previous':scope['receive_cursor'] if scope else None,'committed':(progress or {}).get('receive_cursor')}),eligible,dataset,record['record_id'],revision,operation))

    def mark_trial_committed(self, dataset, items, changes):
        # Called only after the receive transaction exits successfully. If the
        # process dies here the durable receipt survives with an unknown timestamp.
        with self.connect() as db:
            stamp=time.time()
            for item in items:
                record=item.get('record') if changes else item
                rid=(record or item).get('record_id');revision=(record or item).get('record_revision')
                db.execute("UPDATE tasks SET memory_committed_at=? WHERE dataset=? AND record_id=? AND revision=? AND memory_trial IS NOT NULL AND memory_committed_at IS NULL",(stamp,dataset,rid,revision))

    def trial_tasks(self, run_id, *, pending=False, ready=False, limit=5001):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM tasks WHERE memory_trial=?"+(" AND memory_state='pending'" if pending else "")+(" AND state='done'" if ready else "")+" ORDER BY id LIMIT ?",(run_id,limit))]

    def trial_counts(self, run_id):
        with self.connect() as db:
            counts={r[0]:r[1] for r in db.execute("SELECT memory_class,count(*) FROM tasks WHERE memory_trial=? GROUP BY memory_class",(run_id,))}
            states={r[0]:r[1] for r in db.execute("SELECT memory_state,count(*) FROM tasks WHERE memory_trial=? GROUP BY memory_state",(run_id,))}
            failures=db.execute("SELECT count(*) FROM tasks WHERE memory_trial=? AND state='failed'",(run_id,)).fetchone()[0]
        return {'new_messages':counts.get('live',0),'historical_backfill':counts.get('backfill',0),'source_revisions':counts.get('revision',0),'processed_messages':states.get('processed',0),'pending_messages':max(0,states.get('pending',0)-failures),'excluded_messages':states.get('excluded',0),'failed_index_messages':failures}

    def finish_trial_tasks(self, run_id, rows, revision):
        with self.connect() as db:
            for task_id,state in rows:
                db.execute("UPDATE tasks SET memory_state=?,memory_completed_at=?,memory_revision=? WHERE id=? AND memory_trial=? AND memory_state!=?",(state,time.time(),revision,task_id,run_id,state))
