"""Product inference inputs from automatic evidence, without enrollment or truth."""

import json

from allday_asr.v3.domain.blind_windows import plan_source_windows
from allday_asr.v3.domain.sound_kind import sound_uses
from allday_asr.v3.domain.speaker_turns import foreign_turns
from allday_asr.v3.domain.product_self_gate import independent_source_windows
from allday_asr.v3.domain.product_query_ownership import owned_ranges, omitted_ranges, captures_cover_range
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
        evidence_rows = connection.execute("""SELECT kind,status FROM artifacts WHERE run_id=?
            AND kind IN ('v3_asr_evidence','v3_diarization_evidence','v3_transcript_evidence')""", (run_id,)).fetchall()
        # _evidence is shared with frozen experiments. Keep product-only
        # stale/ambiguous guards here, without changing their builder semantics.
        if any(r['status'] != 'active' for r in evidence_rows) or len({r['kind'] for r in evidence_rows}) != len(evidence_rows):
            raise ValueError('product query evidence is stale or ambiguous')
        data = _evidence(connection, run_id, artifact_root)
        if any(d.get('session_id', session_id) != session_id for d in data.values()):
            raise ValueError('product query evidence belongs to another session')
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
        elif not captures_cover_range(row['start_ms'], row['end_ms'], captures):
            query['reason'] = 'incomplete_or_mixed_query'
            query['exclusions'] = [{'start_ms': row['start_ms'], 'end_ms': row['end_ms'],
                                    'reason': 'missing_or_invalid_candidate_capture_mapping'}]
        elif not (ranges := owned_ranges(row['start_ms'], row['end_ms'], row['label'], turns)):
            # Foreign exclusive evidence still invalidates the whole candidate.
            query["reason"] = "not_contained_in_single_exclusive_turn"
        else:
            query['ownership'] = {'version': 'positive-exclusive-ownership-v2',
                'original_range': [row['start_ms'], row['end_ms']],
                'selected_ranges': [list(r) for r in ranges],
                'omitted_ranges': omitted_ranges(row['start_ms'], row['end_ms'], ranges)}
            plan = {'windows': [], 'exclusions': []}
            for lo, hi in ranges:
                owned = dict(row, start_ms=lo, end_ms=hi)
                piece = plan_source_windows(owned, row['label'], row['speaker_track_id'],
                                            turns, tokens, [], captures, 'product-self-v1')
                if piece['exclusions'] and all(e['reason'] == 'below_minimum_useful_duration' for e in piece['exclusions']):
                    # The existing capture-tail retry remains inside this
                    # positively owned span; it never fills ownership gaps.
                    complete = plan_source_windows(owned, row['label'], row['speaker_track_id'],
                                                   turns, [], [], captures, 'product-self-v1')
                    if not complete['exclusions']:
                        query['replanning'] = {'reason': 'token_boundary_short_tail',
                                              'original_exclusions': piece['exclusions']}
                        piece = complete
                for window in piece['windows']:
                    window['provenance']['original_utterance_range'] = [row['start_ms'], row['end_ms']]
                    window['provenance']['selected_owned_range'] = [lo, hi]
                plan['windows'].extend(piece['windows'])
                plan['exclusions'].extend(piece['exclusions'])
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
