"""Local timeline inputs; no task or identity writes."""
import json
from .sound_eligibility import usable_content


class DailySourceRepositoryMixin:
    def daily_semantic_cache(self, digest: str, producer: str = 'daily-semantic') -> dict | None:
        row = self.connection.execute("""SELECT input_scope_json FROM generation_records
            WHERE producer=? AND input_sha256=? AND status='succeeded'
            ORDER BY created_at DESC,generation_number DESC LIMIT 1""", (producer,digest)).fetchone()
        return json.loads(row[0]) if row else None

    def daily_source_starts(self) -> tuple[str, ...]:
        return tuple(r[0] for r in self.connection.execute(
            "SELECT DISTINCT start_at FROM utterances WHERE status='active'"))

    def daily_utterances(self, start: str, end: str) -> tuple[dict, ...]:
        rows = self.connection.execute(f"""
            SELECT u.* FROM utterances u
            WHERE u.status='active' AND trim(u.text)!=''
              AND julianday(u.start_at)>=julianday(?)
              AND julianday(u.start_at)<julianday(?) AND {usable_content('u')}
            ORDER BY julianday(u.start_at), u.utterance_id
        """, (start, end)).fetchall()
        ranges = {}
        ids = json.dumps([r['utterance_id'] for r in rows])
        for r in self.connection.execute("""
            SELECT u.utterance_id,s.asset_id,a.media_id,
              MAX(u.start_ms,s.session_start_ms) AS session_start_ms,
              MIN(u.end_ms,s.session_end_ms) AS session_end_ms,
              s.source_start_ms+MAX(u.start_ms,s.session_start_ms)-s.session_start_ms AS start_ms,
              s.source_start_ms+MIN(u.end_ms,s.session_end_ms)-s.session_start_ms AS end_ms
            FROM json_each(?) ids JOIN utterances u ON u.utterance_id=ids.value
            JOIN capture_segments s ON s.session_id=u.session_id
              AND s.session_start_ms<u.end_ms AND s.session_end_ms>u.start_ms
            JOIN audio_assets a ON a.asset_id=s.asset_id
            ORDER BY u.utterance_id,s.sequence,s.segment_id
        """, (ids,)):
            value = dict(r)
            ranges.setdefault(value.pop('utterance_id'), []).append(value)
        values = []
        for row in rows:
            value = dict(row)
            value['evidence'] = json.loads(value.pop('evidence_json'))
            value['identity_evidence'] = json.loads(value.pop('identity_evidence_json'))
            value['audio_ranges'] = ranges.get(row['utterance_id'], [])
            values.append(value)
        return tuple(values)

    def daily_identity_references(self, track_ids):
        rows = self.connection.execute("""
            SELECT t.speaker_track_id,m.cluster_id AS speaker_cluster_id,
                   l.person_id,p.display_name AS person_name
            FROM json_each(?) ids JOIN speaker_tracks t ON t.speaker_track_id=ids.value
            LEFT JOIN speaker_cluster_memberships m ON m.speaker_track_id=t.speaker_track_id AND m.state='active'
            LEFT JOIN person_cluster_links l ON l.cluster_id=m.cluster_id AND l.status='active'
            LEFT JOIN persons p ON p.person_id=l.person_id
            ORDER BY t.label,t.speaker_track_id,m.cluster_id,l.person_id
        """, (json.dumps(track_ids),))
        return {r['speaker_track_id']: dict(r) for r in rows}

    def daily_person_names(self, person_ids):
        return {r[0]: r[1] for r in self.connection.execute(
            'SELECT person_id,display_name FROM persons JOIN json_each(?) ids ON ids.value=person_id',
            (json.dumps(person_ids),))}

    def daily_event_states(self, day: str, timezone: str) -> tuple[dict, ...]:
        rows = self.connection.execute("""SELECT event_id FROM event_current_states
            WHERE json_extract(payload_json,'$.daily_event_version') IS NOT NULL
              AND json_extract(payload_json,'$.local_date')=?
              AND json_extract(payload_json,'$.timezone')=? ORDER BY created_at,event_id
        """, (day, timezone)).fetchall()
        return tuple(dict(row) for row in rows)

    def daily_tasks(self, utterance_ids: tuple[str, ...]) -> tuple[dict, ...]:
        if not utterance_ids:
            return ()
        # Existing task events are the sole truth; no inferred completion.
        rows = self.connection.execute("""SELECT DISTINCT e.*,
            schedule.scheduled_at, schedule.status AS schedule_status,
            (SELECT MAX(o.created_at) FROM event_operations o WHERE o.event_id=e.event_id
              AND o.operation_kind='complete') AS completed_at
            FROM event_current_states e
            JOIN evidence_links l ON l.subject_type='event' AND l.subject_id=e.event_id
              AND l.evidence_type='utterance'
            JOIN utterances old ON old.utterance_id=l.evidence_id
            JOIN utterances current ON current.session_id=old.session_id
              AND current.start_ms<old.end_ms AND current.end_ms>old.start_ms
              AND julianday(current.start_at)<julianday(old.end_at)
              AND julianday(current.end_at)>julianday(old.start_at)
            JOIN json_each(?) ids ON ids.value=current.utterance_id
            LEFT JOIN reminder_schedules schedule ON schedule.event_id=e.event_id
            WHERE e.event_kind IN ('task','request','commitment','appointment')
              AND json_extract(e.payload_json,'$.daily_event_version') IS NULL
        """, (json.dumps(utterance_ids),)).fetchall()
        evidence = {}
        for row in self.connection.execute("""
            SELECT DISTINCT l.subject_id,current.utterance_id
            FROM evidence_links l JOIN utterances old ON old.utterance_id=l.evidence_id
            JOIN utterances current ON current.session_id=old.session_id
              AND current.start_ms<old.end_ms AND current.end_ms>old.start_ms
              AND julianday(current.start_at)<julianday(old.end_at)
              AND julianday(current.end_at)>julianday(old.start_at)
            JOIN json_each(?) ids ON ids.value=current.utterance_id
            JOIN json_each(?) events ON events.value=l.subject_id
            WHERE l.subject_type='event' AND l.evidence_type='utterance'
            ORDER BY l.subject_id,current.utterance_id
        """, (json.dumps(utterance_ids), json.dumps([r['event_id'] for r in rows]))):
            evidence.setdefault(row[0], []).append(row[1])
        result = []
        for row in rows:
            value = dict(row)
            value['payload'] = json.loads(value.pop('payload_json'))
            value['evidence_ids'] = evidence.get(value['event_id'], [])
            result.append(value)
        return tuple(result)
