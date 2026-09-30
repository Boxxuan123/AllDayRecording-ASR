"""Read only automatic evidence, never human person/sound labels or truth."""

import json

from allday_asr.v3.application.blind_scoring import digest
from allday_asr.v3.domain.sound_kind import sound_uses


def latest_run(connection, session_id):
    row = connection.execute("""SELECT p.run_id FROM processing_runs p
        LEFT JOIN processing_jobs j ON j.run_id=p.run_id
        WHERE p.session_id=? AND p.status='succeeded'
          AND COALESCE(json_extract(j.request_json,'$.admission_mode'),'production')='production'
        ORDER BY p.created_at DESC,p.run_id DESC LIMIT 1""", (session_id,)).fetchone()
    return row[0] if row else None


def automatic_queries(connection, session_id, experiment_id, run_id):
    session = connection.execute("SELECT captured_start FROM recording_sessions WHERE session_id=?", (session_id,)).fetchone()
    segments = connection.execute("""SELECT s.*,a.media_id,a.sha256,r.storage_key
        FROM capture_segments s JOIN audio_assets a USING(asset_id)
        JOIN audio_replicas r USING(replica_id) WHERE s.session_id=? AND r.state='available'
        ORDER BY s.sequence,s.segment_id""", (session_id,)).fetchall()
    tracks = connection.execute("""SELECT speaker_track_id FROM speaker_tracks WHERE run_id=?
        AND label NOT LIKE 'manual:%' AND label NOT LIKE 'sample:%' ORDER BY speaker_track_id""", (run_id,)).fetchall()
    queries = []
    for track in tracks:
        track_id = track[0]
        utterances = connection.execute("""SELECT utterance_id,start_ms,end_ms,evidence_json
            FROM utterances WHERE run_id=? AND original_speaker_track_id=?
            ORDER BY (end_ms-start_ms) DESC,start_ms,utterance_id LIMIT 12""", (run_id, track_id)).fetchall()
        windows = []
        for utterance in utterances:
            if not sound_uses(json.loads(utterance["evidence_json"]))["sample_candidate_allowed"]:
                continue
            for segment in segments:
                start = max(utterance["start_ms"], segment["session_start_ms"])
                end = min(utterance["end_ms"], segment["session_end_ms"], start + 8000)
                if end - start < 800:
                    continue
                source = segment["source_start_ms"] + start - segment["session_start_ms"]
                if any(w["sha256"] == segment["sha256"] and w["start_ms"] < source + end - start
                       and w["end_ms"] > source for w in windows):
                    continue
                windows.append({"media_id": segment["media_id"], "sha256": segment["sha256"],
                                "storage_key": segment["storage_key"], "start_ms": source,
                                "end_ms": source + end - start, "session_start_ms": start,
                                "session_end_ms": end, "utterance_id": utterance["utterance_id"]})
                break
            if len(windows) == 5:
                break
        if windows:
            # Content/range based key survives regenerated run and track identifiers.
            identity = sorted((w["sha256"], w["start_ms"], w["end_ms"]) for w in windows)
            query_id = digest([experiment_id, session_id, identity])
            queries.append({"query_id": query_id, "session_id": session_id, "run_id": run_id,
                            "speaker_track_id": track_id, "date": session[0][:10], "windows": windows,
                            "session_start_ms": min(w["session_start_ms"] for w in windows),
                            "session_end_ms": max(w["session_end_ms"] for w in windows),
                            "duration_s": sum(w["end_ms"]-w["start_ms"] for w in windows)/1000,
                            "window_count": len(windows)})
    return queries
