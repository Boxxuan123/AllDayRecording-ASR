"""Read only automatic evidence, never human person/sound labels or truth."""

import json
import hashlib
from pathlib import Path

from allday_asr.v3.application.blind_scoring import digest
from allday_asr.v3.domain.sound_kind import sound_uses
from allday_asr.v3.domain.speaker_turns import (
    QUERY_BUILDER_VERSION, LEGACY_QUERY_BUILDER_VERSION,
)
from allday_asr.v3.domain.blind_windows import plan_source_windows, MAX_QUERY_WINDOWS


def latest_run(connection, session_id):
    row = connection.execute("""SELECT p.run_id FROM processing_runs p
        LEFT JOIN processing_jobs j ON j.run_id=p.run_id
        WHERE p.session_id=? AND p.status='succeeded'
          AND COALESCE(json_extract(j.request_json,'$.admission_mode'),'production')='production'
        ORDER BY p.created_at DESC,p.run_id DESC LIMIT 1""", (session_id,)).fetchone()
    return row[0] if row else None


def automatic_queries_legacy(connection, session_id, experiment_id, run_id):
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


def _evidence(connection, run_id, artifact_root):
    data = {}
    for row in connection.execute("SELECT kind,storage_ref,sha256 FROM artifacts WHERE run_id=? AND kind IN "
                                  "('v3_asr_evidence','v3_diarization_evidence','v3_transcript_evidence')",
                                  (run_id,)):
        path = (Path(artifact_root) / row["storage_ref"]).resolve()
        if not path.is_relative_to(Path(artifact_root).resolve()):
            raise ValueError("artifact escaped store")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != row["sha256"]:
            raise ValueError("query evidence checksum mismatch")
        data[row["kind"]] = json.loads(raw.decode("utf-8"))
    if not {"v3_asr_evidence", "v3_diarization_evidence"} <= data.keys():
        raise ValueError("turn-aware query requires persisted ASR and diarization evidence")
    return data


def automatic_queries(connection, session_id, experiment_id, run_id, *,
                      builder_version=LEGACY_QUERY_BUILDER_VERSION, artifact_root=None):
    if builder_version == LEGACY_QUERY_BUILDER_VERSION:
        return automatic_queries_legacy(connection, session_id, experiment_id, run_id)
    if builder_version != QUERY_BUILDER_VERSION or artifact_root is None:
        raise ValueError("unsupported query builder or missing artifact store")
    data = _evidence(connection, run_id, artifact_root)
    asr = data["v3_asr_evidence"]
    turns = data["v3_diarization_evidence"]["exclusive_turns"]
    tokens = asr["primary_tokens"]
    vad = []
    for hypothesis in asr.get("hypotheses", []):
        if hypothesis.get("role") == "primary":
            offset = hypothesis["analysis_start_ms"]
            vad.extend((a + offset, b + offset)
                       for a, b in hypothesis.get("raw_response", {}).get("speech_ranges_ms", []))
    projection = data.get("v3_transcript_evidence", {}).get("projection_version", "legacy-unversioned")
    session = connection.execute("SELECT captured_start FROM recording_sessions WHERE session_id=?",
                                 (session_id,)).fetchone()
    captures = [dict(row) for row in connection.execute(
        "SELECT s.*,a.media_id,a.sha256,a.duration_ms,r.storage_key FROM capture_segments s "
        "JOIN audio_assets a USING(asset_id) JOIN audio_replicas r USING(replica_id) "
        "WHERE s.session_id=? AND r.state='available' ORDER BY s.sequence,s.segment_id", (session_id,))]
    queries = []
    for track in connection.execute("SELECT speaker_track_id,label FROM speaker_tracks WHERE run_id=? "
                                    "AND label NOT LIKE 'manual:%' AND label NOT LIKE 'sample:%' "
                                    "ORDER BY speaker_track_id", (run_id,)):
        windows, exclusions, omitted = [], [], []
        utterances = connection.execute("SELECT utterance_id,start_ms,end_ms,evidence_json FROM utterances "
            "WHERE run_id=? AND original_speaker_track_id=? ORDER BY (end_ms-start_ms) DESC,start_ms,utterance_id "
            "LIMIT 12", (run_id, track["speaker_track_id"])).fetchall()
        for u in utterances:
            if not sound_uses(json.loads(u["evidence_json"]))["sample_candidate_allowed"]:
                continue
            plan = plan_source_windows(dict(u), track["label"], track["speaker_track_id"], turns,
                                       tokens, vad, captures, projection)
            exclusions.extend({"utterance_id": u["utterance_id"], **e} for e in plan["exclusions"])
            for w in plan["windows"]:
                if any(x["sha256"] == w["sha256"] and x["start_ms"] < w["end_ms"]
                       and x["end_ms"] > w["start_ms"] for x in windows):
                    omitted.append({"reason": "duplicate_source", "window": w})
                elif len(windows) >= MAX_QUERY_WINDOWS:
                    omitted.append({"reason": "explicit_query_window_budget", "window": w})
                else:
                    windows.append(w)
        if not windows:
            continue
        identity = sorted((w["sha256"], w["start_ms"], w["end_ms"]) for w in windows)
        queries.append({"query_id": digest([experiment_id, session_id, builder_version, identity]),
                        "session_id": session_id, "run_id": run_id,
                        "speaker_track_id": track["speaker_track_id"], "date": session[0][:10],
                        "windows": windows, "query_builder_version": builder_version,
                        "projection_version": projection, "review_schema_version": 2,
                        "model_window_order": list(range(len(windows))),
                        "review_window_order": sorted(range(len(windows)),
                            key=lambda i: (windows[i]["session_start_ms"], windows[i]["sha256"], windows[i]["start_ms"])),
                        "audio_composition": "multiple_clips" if len(windows) > 1 else "single_clip",
                        "planning_exclusions": exclusions, "unselected_windows": omitted,
                        "session_start_ms": min(w["session_start_ms"] for w in windows),
                        "session_end_ms": max(w["session_end_ms"] for w in windows),
                        "duration_s": sum(w["end_ms"] - w["start_ms"] for w in windows) / 1000,
                        "window_count": len(windows)})
    return queries
