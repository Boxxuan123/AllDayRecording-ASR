"""Read-only learning-session overlap audit. Never invokes a production write API."""

import argparse
import json
from collections import Counter
from pathlib import Path

import numpy as np
import soundfile as sf

from build_pure_self_eval_manifest import anonymous, strict_review
from historical_self_integrity import snapshot
from short_self_dataset import digest, read, readonly, write
from short_overlap_geometry import analyze, pattern_cells, sensitivity, transitions
from allday_asr.v3.adapters.audio.tools import AudioClip, assemble_audio_clips
from allday_asr.v3.adapters.sqlite.blind_queries import _evidence, latest_run


def allowed_session(connection, session_id):
    role = connection.execute(
        "SELECT dataset_role FROM session_dataset_roles WHERE session_id=?",
        (session_id,),
    ).fetchone()
    reserved = connection.execute(
        "SELECT research_role FROM session_speaker_reservations WHERE session_id=?",
        (session_id,),
    ).fetchone()
    predicted = connection.execute(
        "SELECT 1 FROM blind_query_views q JOIN blind_prediction_snapshots p USING(query_id) WHERE q.session_id=? LIMIT 1",
        (session_id,),
    ).fetchone()
    return bool(
        role
        and role[0] == "learning"
        and (not reserved or reserved[0] == "learning")
        and not predicted
    )


def nearby(turns, lo, hi):
    return [t for t in turns if t["start_ms"] < hi + 3000 and t["end_ms"] > lo - 3000]


def event_record(
    session, run, artifact, captures, lo, hi, speaker, diarization, provenance
):
    regular, exclusive = diarization["regular_turns"], diarization["exclusive_turns"]
    geometry = analyze(lo, hi, speaker, regular, exclusive)
    identifier = anonymous(
        f"{session['session_id']}:{run}:{lo}:{hi}:{speaker}:{provenance['source']}"
    )
    return {
        "event_id": identifier,
        "session_id": session["session_id"],
        "session": anonymous(session["session_id"]),
        "date": session["captured_start"][:10],
        "range_ms": [lo, hi],
        "target_label": speaker,
        "processing_run_id": run,
        "artifact": artifact,
        "truth_provenance": provenance,
        "regular_turns": nearby(regular, lo, hi),
        "exclusive_turns": nearby(exclusive, lo, hi),
        "captures": [
            c
            for c in captures
            if c["session_start_ms"] < hi + 3000 and c["session_end_ms"] > lo - 3000
        ],
        "geometry": geometry,
        "pattern_duration_ms": pattern_cells(lo, hi, speaker, regular, exclusive),
        "posterior": "not available",
        "speaker_activity_confidence": "not available",
        "classification": "INSUFFICIENT_EVIDENCE",
        "classification_confidence": "insufficient",
        "selection": "all eligible frozen-evidence events; not a random population sample",
    }


def clip(state, captures, start, end, destination):
    """Assemble exactly the requested available interval; no missing-audio fill."""
    cursor, pieces, used = start, [], []
    for cap in sorted(captures, key=lambda c: (c["session_start_ms"], c["sequence"])):
        lo, hi = max(cursor, cap["session_start_ms"]), min(end, cap["session_end_ms"])
        if lo >= hi:
            continue
        if (
            lo != cursor
            or cap["source_end_ms"] - cap["source_start_ms"]
            != cap["session_end_ms"] - cap["session_start_ms"]
        ):
            raise ValueError("missing or invalid capture mapping")
        source = state / "audio" / cap["storage_key"]
        if digest(source) != cap["sha256"]:
            raise ValueError("capture digest mismatch")
        offset = cap["source_start_ms"] - cap["session_start_ms"]
        pieces.append(AudioClip(source, lo + offset, hi + offset))
        used.append(
            {
                "capture_id": cap["segment_id"],
                "sha256": cap["sha256"],
                "session_range_ms": [lo, hi],
                "source_range_ms": [lo + offset, hi + offset],
            }
        )
        cursor = hi
        if cursor == end:
            break
    if cursor != end:
        raise ValueError("incomplete context audio")
    assemble_audio_clips(pieces, destination)
    return {
        "path": str(destination),
        "requested_range_ms": [start, end],
        "sha256": digest(destination),
        "captures": used,
    }


def acoustic_features(path):
    wave, rate = sf.read(path, dtype="float32")
    wave = wave if wave.ndim == 1 else wave.mean(axis=1)
    rms = float(np.sqrt(np.mean(wave**2)))
    spectrum = np.abs(np.fft.rfft(wave)) ** 2
    frequency = np.fft.rfftfreq(len(wave), 1 / rate)
    total = float(spectrum.sum())
    return {
        "sample_rate": rate,
        "samples": len(wave),
        "duration_ms": len(wave) * 1000 / rate,
        "rms": rms,
        "rms_dbfs": float(20 * np.log10(max(rms, 1e-12))),
        "peak": float(np.max(np.abs(wave))),
        "energy_300_3400Hz_fraction": float(
            spectrum[(frequency >= 300) & (frequency <= 3400)].sum() / total
        )
        if total
        else 0.0,
        "interpretation": "nonzero energy and spectral content do not establish speech, speaker identity, or simultaneous speakers",
    }


def export_audio(state, event, output):
    exported = []
    caps = event["captures"]
    minimum, maximum = (
        min(c["session_start_ms"] for c in caps),
        max(c["session_end_ms"] for c in caps),
    )
    for index, component in enumerate(event["geometry"]["intersections"]):
        lo, hi = component["start_ms"], component["end_ms"]
        directory = output / "clips" / event["event_id"] / str(index)
        directory.mkdir(parents=True, exist_ok=True)
        ranges = {
            "full-context": (max(minimum, lo - 3000), min(maximum, hi + 3000)),
            "target-boundary": (max(minimum, lo - 500), min(maximum, hi + 500)),
            "overlap-only": (lo, hi),
            "pre-overlap-context": (max(minimum, lo - 1000), lo),
            "post-overlap-context": (hi, min(maximum, hi + 1000)),
        }
        if event.get("anchor"):
            ranges.update(
                {
                    f"context-{ms}ms": (max(minimum, lo - ms), min(maximum, hi + ms))
                    for ms in (250, 1000)
                }
            )
        clips = {
            name: clip(state, caps, a, b, directory / f"{name}.wav")
            for name, (a, b) in ranges.items()
            if a < b
        }
        features = acoustic_features(directory / "overlap-only.wav")
        item = {
            "event_id": event["event_id"],
            "component_index": index,
            "component": component,
            "clips": clips,
            "overlap_features": features,
        }
        write(directory / "audio-provenance.json", item)
        exported.append(item)
    return exported


def describe(values):
    if not values:
        return {"n": 0}
    a = np.asarray(values)
    return {
        "n": len(values),
        "p25": float(np.quantile(a, 0.25)),
        "median": float(np.median(a)),
        "p75": float(np.quantile(a, 0.75)),
        "max": float(a.max()),
    }


def scan(state, output, anchor_root):
    state, output, anchor_root = (
        Path(state).resolve(),
        Path(output).resolve(),
        Path(anchor_root),
    )
    if output.is_relative_to(state) or (output / "manifest.json").exists():
        raise ValueError("new audit output outside production state required")
    output.mkdir(parents=True, exist_ok=True)
    before = snapshot(state)
    write(output / "scan-before.json", before)
    anchor_manifest = read(anchor_root / "manifest.json")
    if digest(anchor_root / "manifest.json") != (
        anchor_root / "manifest.sha256"
    ).read_text(encoding="ascii"):
        raise ValueError("prior fixed anchor manifest changed")
    anchor = next(c for c in anchor_manifest["cases"] if c["case"] == "B")
    c = readonly(state)
    self_id = c.execute("SELECT person_id FROM persons WHERE kind='self'").fetchone()[0]
    strict = c.execute("""SELECT s.*,e.* FROM speaker_purity_sources s
        JOIN speaker_purity_current cur USING(source_key)
        JOIN speaker_source_purity_evidence e USING(evidence_id) ORDER BY s.source_key""").fetchall()
    sessions = c.execute(
        "SELECT * FROM recording_sessions WHERE tombstoned_at IS NULL ORDER BY session_id"
    ).fetchall()
    candidates, reviewed, all_transitions, exclusions = [], [], [], Counter()
    pattern_events, pattern_ms = Counter(), Counter()
    seen_review, source_hashes, run_evidence = set(), {}, []
    for session in sessions:
        sid = session["session_id"]
        if not allowed_session(c, sid):
            exclusions["frozen_or_missing_learning_role_session"] += 1
            continue
        run = latest_run(c, sid)
        if run is None:
            exclusions["no_successful_run"] += 1
            continue
        artifacts = [
            dict(r)
            for r in c.execute(
                "SELECT * FROM artifacts WHERE run_id=? AND kind IN ('v3_asr_evidence','v3_diarization_evidence','v3_transcript_evidence')",
                (run,),
            )
        ]
        try:
            if (
                len(artifacts) != 3
                or len({a["kind"] for a in artifacts}) != 3
                or any(a["status"] != "active" for a in artifacts)
            ):
                raise ValueError("incompatible evidence")
            data = _evidence(c, run, state / "artifacts")
            if any(d.get("session_id", sid) != sid for d in data.values()):
                raise ValueError("wrong session")
            diar = data["v3_diarization_evidence"]
        except (OSError, ValueError, KeyError, TypeError):
            exclusions["unavailable_compatible_evidence_session"] += 1
            continue
        captures = [
            dict(r)
            for r in c.execute(
                """SELECT s.*,a.media_id,a.sha256,a.duration_ms,r.storage_key
            FROM capture_segments s JOIN audio_assets a USING(asset_id)
            JOIN audio_replicas r USING(replica_id) WHERE s.session_id=? AND r.state='available'
            ORDER BY s.sequence,s.segment_id""",
                (sid,),
            )
        ]
        artifact = next(a for a in artifacts if a["kind"] == "v3_diarization_evidence")
        run_evidence.append(
            {
                "session": anonymous(sid),
                "processing_run_id": run,
                "artifacts": artifacts,
                "diarization_metadata": diar.get("metadata"),
                "raw_response": diar.get("raw_response"),
            }
        )
        all_transitions.extend(
            dict(t, session=anonymous(sid), processing_run_id=run)
            for t in transitions(diar["regular_turns"], diar["exclusive_turns"])
        )
        utterances = c.execute(
            """SELECT u.*,t.label AS original_label FROM utterances u
            JOIN speaker_tracks t ON t.speaker_track_id=u.original_speaker_track_id
            WHERE u.run_id=? AND u.status='active' ORDER BY u.ordinal""",
            (run,),
        ).fetchall()
        for u in utterances:
            e = event_record(
                session,
                run,
                artifact,
                captures,
                u["start_ms"],
                u["end_ms"],
                u["original_label"],
                diar,
                {
                    "source": "automatic_original_utterance",
                    "utterance_id": u["utterance_id"],
                    "truth": "not ground truth",
                },
            )
            for key, ms in e["pattern_duration_ms"].items():
                pattern_events[key] += 1
                pattern_ms[key] += ms
            if e["geometry"]["b_pattern"]:
                candidates.append(e)
        media = {cap["media_id"]: cap for cap in captures}
        for row in strict:
            truth = strict_review(row, self_id)
            cap = media.get(row["source_media_id"])
            if truth is None or cap is None:
                continue
            identity = (row["source_key"], sid)
            if identity in seen_review:
                continue
            seen_review.add(identity)
            if (
                not cap["source_start_ms"]
                <= row["start_ms"]
                < row["end_ms"]
                <= cap["source_end_ms"]
            ):
                exclusions["strict_range_outside_capture"] += 1
                continue
            path = state / "audio" / cap["storage_key"]
            if cap["storage_key"] not in source_hashes:
                source_hashes[cap["storage_key"]] = digest(path)
            if source_hashes[cap["storage_key"]] != cap["sha256"]:
                raise ValueError("strict source hash mismatch")
            offset = cap["session_start_ms"] - cap["source_start_ms"]
            lo, hi = row["start_ms"] + offset, row["end_ms"] + offset
            intersecting = [
                u for u in utterances if u["start_ms"] < hi and u["end_ms"] > lo
            ]
            if not intersecting:
                exclusions["strict_no_original_track"] += 1
                continue
            u = max(
                intersecting,
                key=lambda u: min(hi, u["end_ms"]) - max(lo, u["start_ms"]),
            )
            provenance = {
                "source": "individual_human_clean_single_review",
                "truth": truth,
                "primary_person_id": row["review_primary_person_id"],
                "source_key": row["source_key"],
                "review_references": json.loads(row["review_references_json"]),
                "provenance": json.loads(row["provenance_json"]),
                "automatic_label_used_only_for_geometry": True,
                "not_proof_of_no_foreign_audio": True,
            }
            e = event_record(
                session,
                run,
                artifact,
                captures,
                lo,
                hi,
                u["original_label"],
                diar,
                provenance,
            )
            e["anchor"] = (
                sid == anchor["session_id"] and [lo, hi] == anchor["human_review"]
            )
            e["physical_source_key"] = [cap["sha256"], row["start_ms"], row["end_ms"]]
            reviewed.append(e)
    c.close()
    if sum(e["anchor"] for e in reviewed) != 1:
        raise ValueError("frozen Case B must remain exactly one strict anchor")
    # Stable physical boundaries are deduplicated. Nearby events remain correlated.
    unique = {}
    for e in sorted(reviewed, key=lambda e: (not e["anchor"], e["event_id"])):
        unique.setdefault(tuple(e["physical_source_key"]), e)
    reviewed = list(unique.values())
    focus = [
        e
        for e in reviewed
        if e["anchor"]
        or any(i["duration_ms"] <= 200 for i in e["geometry"]["intersections"])
    ]
    manifest = {
        "format": "short-regular-overlap-acoustic-audit-v1",
        "read_only": True,
        "source_anchor_manifest_sha256": digest(anchor_root / "manifest.json"),
        "scope": "current successful compatible evidence, learning sessions only; frozen evaluation sessions excluded",
        "predicted_b_patterns": candidates,
        "strict_human_events": reviewed,
        "focus_event_ids": [e["event_id"] for e in focus],
    }
    write(output / "manifest.json", manifest)
    (output / "manifest.sha256").write_text(
        digest(output / "manifest.json"), encoding="ascii"
    )
    write(output / "source-evidence.json", run_evidence)
    write(output / "transitions.json", all_transitions)
    components = [i for e in candidates for i in e["geometry"]["intersections"]]
    strict_components = [i for e in reviewed for i in e["geometry"]["intersections"]]
    summary = {
        "scanned_session_count": len(sessions),
        "compatible_learning_session_count": len(run_evidence),
        "candidate_count": len(candidates),
        "candidate_edge_count": sum(
            e["geometry"]["edge_b_pattern"] for e in candidates
        ),
        "strict_human_ground_truth_count": len(reviewed),
        "strict_truth_counts": dict(
            Counter(e["truth_provenance"]["truth"] for e in reviewed)
        ),
        "strict_b_pattern_count": sum(e["geometry"]["b_pattern"] for e in reviewed),
        "strict_b_pattern_truth_counts": dict(
            Counter(
                e["truth_provenance"]["truth"]
                for e in reviewed
                if e["geometry"]["b_pattern"]
            )
        ),
        "candidate_component_positions": dict(
            Counter(i["position"] for i in components)
        ),
        "candidate_duration_buckets": dict(Counter(i["bucket"] for i in components)),
        "strict_component_positions": dict(
            Counter(i["position"] for i in strict_components)
        ),
        "strict_duration_buckets": dict(
            Counter(i["bucket"] for i in strict_components)
        ),
        "candidate_intersection_duration": describe(
            [i["duration_ms"] for i in components]
        ),
        "strict_intersection_duration": describe(
            [i["duration_ms"] for i in strict_components]
        ),
        "transition_count": len(all_transitions),
        "foreign_tail_after_switch": describe(
            [t["foreign_tail_after_switch_ms"] for t in all_transitions]
        ),
        "target_lead_before_switch": describe(
            [t["target_lead_before_switch_ms"] for t in all_transitions]
        ),
        "foreign_tail_histogram_ms": dict(
            Counter(t["foreign_tail_after_switch_ms"] for t in all_transitions)
        ),
        "pattern_events": dict(pattern_events),
        "pattern_duration_ms": dict(pattern_ms),
        "exclusions": dict(exclusions),
        "focus_count": len(focus),
        "overlap_acoustic_reviewed_count": 0,
        "bucket_semantics": "(0,25],(25,50],(50,100],(100,200],(200,500],(500,infinity); ms",
        "independence_limit": "source boundaries deduplicated; neighboring events and transition directions are correlated",
    }
    write(output / "summary.json", summary)
    write(
        output / "sensitivity.json",
        [{"event_id": e["event_id"], "analysis": sensitivity(e)} for e in focus],
    )
    audio = [item for e in focus for item in export_audio(state, e, output)]
    write(output / "audio-index.json", audio)
    after = snapshot(state)
    write(output / "scan-after.json", after)
    if before != after:
        raise ValueError("production fingerprints changed during audit")
    write(
        output / "scan-integrity.json",
        {
            "all_production_tables_and_assets_unchanged": True,
            "manifest_sha256": digest(output / "manifest.json"),
            "audio_count": sum(len(i["clips"]) for i in audio),
        },
    )
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=Path("state/v3"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--anchor-root",
        type=Path,
        default=Path("outputs/self-query-boundary-audit-20261002"),
    )
    args = parser.parse_args()
    print(json.dumps(scan(args.state_dir, args.output, args.anchor_root)))
