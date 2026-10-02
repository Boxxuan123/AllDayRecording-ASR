"""Product inference inputs from automatic evidence, without enrollment or truth."""

import json

from allday_asr.v3.domain.blind_windows import plan_source_windows
from allday_asr.v3.domain.sound_kind import sound_uses
from allday_asr.v3.domain.speaker_turns import foreign_turns
from allday_asr.v3.domain.product_self_gate import independent_source_windows
from allday_asr.v3.ports.speaker_embeddings import SpeakerClipInput, SpeakerTrackInput

from .blind_queries import _evidence, latest_run


def product_self_queries(connection, session_id, artifact_root):
    run_id = latest_run(connection, session_id)
    if run_id is None:
        return []
    rows = connection.execute(
        """SELECT u.*,t.label FROM utterances u JOIN speaker_tracks t
        ON t.speaker_track_id=u.speaker_track_id
        WHERE u.run_id=? AND u.status='active' ORDER BY u.ordinal,u.utterance_id""",
        (run_id,),
    ).fetchall()
    try:
        data = _evidence(connection, run_id, artifact_root)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return [{"utterance_id": r["utterance_id"], "revision": r["revision"],
                 "tracks": (), "reason": f"automatic_turn_evidence_unavailable:{type(exc).__name__}"}
                for r in rows]
    captures = [dict(r) for r in connection.execute(
        """SELECT s.*,a.media_id,a.sha256,a.duration_ms,r.storage_key
        FROM capture_segments s JOIN audio_assets a USING(asset_id)
        JOIN audio_replicas r USING(replica_id)
        WHERE s.session_id=? AND r.state='available' ORDER BY s.sequence,s.segment_id""",
        (session_id,),
    )]
    turns = data["v3_diarization_evidence"]["exclusive_turns"]
    tokens = data["v3_asr_evidence"]["primary_tokens"]
    result = []
    for row in rows:
        query = {"utterance_id": row["utterance_id"], "revision": row["revision"],
                 "speaker_track_id": row["speaker_track_id"], "tracks": (),
                 "start_ms": row["start_ms"], "end_ms": row["end_ms"]}
        evidence = json.loads(row["evidence_json"])
        if row["label"].startswith(("manual:", "sample:")) or evidence.get("person_annotation"):
            query["reason"] = "preserved_manual_person"
        elif not sound_uses(evidence)["sample_candidate_allowed"]:
            query["reason"] = "excluded_sound"
        elif foreign_turns(row["start_ms"], row["end_ms"], row["label"],
                           data["v3_diarization_evidence"].get("regular_turns", [])):
            query["reason"] = "overlapping_or_foreign_regular_turn"
        elif not any(t["speaker_label"] == row["label"] and
                     t["start_ms"] <= row["start_ms"] and t["end_ms"] >= row["end_ms"]
                     for t in turns):
            # A track is a grouping, not a guarantee of one speaker throughout.
            query["reason"] = "not_contained_in_single_exclusive_turn"
        else:
            plan = plan_source_windows(dict(row), row["label"], row["speaker_track_id"],
                                       turns, tokens, [], captures, "product-self-v1")
            if plan["exclusions"] and all(
                    e["reason"] == "below_minimum_useful_duration"
                    for e in plan["exclusions"]):
                # Token boundaries can leave a tiny tail at a capture edge.
                # Replan inside the already verified single exclusive turn;
                # source continuity and every mapping check still apply.
                complete = plan_source_windows(
                    dict(row), row["label"], row["speaker_track_id"],
                    turns, [], [], captures, "product-self-v1")
                if not complete["exclusions"]:
                    query["replanning"] = {
                        "reason": "token_boundary_short_tail",
                        "original_exclusions": plan["exclusions"],
                    }
                    plan = complete
            query["windows"] = plan["windows"]
            query["exclusions"] = plan["exclusions"]
            if plan["exclusions"]:
                query["reason"] = "incomplete_or_mixed_query"
            elif not independent_source_windows(plan['windows']):
                query['reason'] = 'non_independent_source_windows'
            else:
                tracks = []
                for window in plan["windows"]:
                    start, end = window["start_ms"], window["end_ms"]
                    pieces = [(start, end)] if end-start < 4000 else [
                        (start, (start+end)//2), ((start+end)//2, end)]
                    for lo, hi in pieces:
                        tracks.append(SpeakerTrackInput(
                            f'{row["utterance_id"]}:{len(tracks)}', session_id,
                            (SpeakerClipInput(window["media_id"], window["storage_key"],
                                              lo, hi, row["utterance_id"]),)))
                query["tracks"] = tuple(tracks)
                query["reason"] = None
        result.append(query)
    return result
