"""Frozen G/P validation on independent original-audio events.

Run: .venv/Scripts/python.exe tools/validate_speaker_identity_blind.py
All database reads are SQLite mode=ro/query_only. Private results stay in outputs/.
"""
from __future__ import annotations

import collections
import hashlib
import json
import platform
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
V2 = ROOT / "outputs/speaker-recognition-benchmark-v2-20260928"
PILOT = ROOT / "outputs/annotation-feasibility-20260928"
OUT = ROOT / "outputs/speaker-identity-blind-validation-20260928"
DB = ROOT / "state/v3/core.sqlite3"
MIN_SECONDS = 6.0


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=float), encoding="utf-8")


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def utc(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def unit(value):
    array = np.asarray(value, dtype=np.float64)
    length = float(np.linalg.norm(array))
    if not np.isfinite(array).all() or length < 1e-12:
        raise ValueError("Invalid speaker vector")
    return array / length


def open_db():
    connection = sqlite3.connect(DB.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("BEGIN")
    return connection


def frozen_sources():
    manifest = read(V2 / "manifest.json")
    settings = read(V2 / "frozen-settings.json")
    baseline = next(x for x in read(V2 / "baseline-results.json") if x["name"] == "historical calibrated centroid")
    person_case = next(x for x in read(V2 / "calibration-results.json") if x["name"] == "A2 per-person + margin")
    g = settings["historical calibrated centroid"]
    p = settings["A2 per-person + margin"]
    assert g["threshold"] == 0.42 and g["margin"] == 0.10
    assert p["threshold"] == 0.42 and p["margin"] == 0.10
    assert baseline["test"]["known"] == 16 and baseline["test"]["correct"] == 10
    assert baseline["test"].get("wrong_person", 0) == baseline["test"].get("unknown_false_accept", 0) == 0
    assert person_case["test"]["correct"] == 8 and person_case["test"].get("unknown_false_accept", 0) == 0
    assert set(p["per_person"]) == set(manifest["known"])
    files = ("manifest.json", "frozen-settings.json", "baseline-results.json", "calibration-results.json")
    hashes = {name: sha(V2 / name) for name in files}
    hashes["pilot-frozen-settings.json"] = sha(PILOT / "frozen-settings.json")
    hashes["pilot-vectors.npz"] = sha(PILOT / "vectors.npz")
    cutoff = max((V2 / name).stat().st_mtime for name in ("frozen-settings.json", "verification.json"))
    frozen = {"G": {"profile": "longest5_centroid; max session-centroid cosine", "threshold": g["threshold"], "margin": g["margin"]},
              "P": {"profile": "same as G", "global_fallback": p["threshold"],
                    "per_person": p["per_person"], "margin": p["margin"]},
              "known_ids": manifest["known"], "minimum_speech_seconds": MIN_SECONDS,
              "model": "FunASR/CAM++ v1-local", "model_sha256": read(V2 / "verification.json")["model_sha256"],
              "v2_annotation_revision": read(V2 / "verification.json")["annotation_revision"],
              "source_sha256": hashes,
              "freeze_utc": datetime.fromtimestamp(cutoff, timezone.utc).isoformat(),
              "freeze_basis": "local V2 frozen-settings/verification timestamp; require session capture, session creation, prototype and original audio creation after cutoff"}
    path = OUT / "frozen-candidates.json"
    if path.exists():
        if read(path) != frozen:
            raise RuntimeError("Frozen source or cutoff changed; preserve and audit the previous run")
    else:
        save("frozen-candidates.json", frozen)
    return manifest, frozen


def frozen_profile(manifest):
    vectors = np.load(PILOT / "vectors.npz")
    chosen = read(PILOT / "frozen-settings.json")["selected_refs"]["longest5_centroid"]
    refs = collections.defaultdict(list)
    for entry in chosen:
        refs[entry["person_id"]].append(unit(np.mean([unit(vectors[wid]) for wid in entry["windows"]], axis=0)))
    assert set(refs) == set(manifest["known"])
    return dict(refs)


def data_tables(connection):
    sessions = {r["session_id"]: dict(r) for r in connection.execute("SELECT * FROM recording_sessions")}
    assets = {r["media_id"]: dict(r) for r in connection.execute("SELECT * FROM audio_assets WHERE media_id IS NOT NULL")}
    tracks = {r["speaker_track_id"]: dict(r) for r in connection.execute("SELECT * FROM speaker_tracks")}
    replicas = collections.defaultdict(list)
    for row in connection.execute("SELECT a.media_id,r.storage_key FROM audio_replicas r JOIN audio_assets a USING(asset_id) WHERE r.state='available'"):
        replicas[row["media_id"]].append(row["storage_key"])
    captures = collections.defaultdict(list)
    for row in connection.execute("""SELECT s.session_id,a.media_id,s.session_start_ms,s.session_end_ms,
                                  s.source_start_ms,s.source_end_ms FROM capture_segments s
                                  JOIN audio_assets a USING(asset_id) ORDER BY s.sequence"""):
        captures[(row["session_id"], row["media_id"])].append(dict(row))
    return sessions, assets, tracks, replicas, captures


def candidate_prototypes(connection, sessions, assets, tracks, replicas, captures):
    result = []
    for row in connection.execute("""SELECT * FROM voice_prototypes WHERE speaker_track_id IS NOT NULL
                                     AND source_prototype_id IS NULL ORDER BY prototype_id"""):
        track = tracks.get(row["speaker_track_id"])
        if not track or track["session_id"] not in sessions:
            continue
        sid = track["session_id"]
        session = sessions[sid]
        clips = []
        for original in json.loads(row["representative_clips_json"] or "[]"):
            media = original["media_id"]
            asset = assets.get(media)
            if asset is None:
                raise RuntimeError(f"Missing source media {media}")
            start, end = int(original["start_ms"]), int(original["end_ms"])
            positions = [segment["session_start_ms"] + start - segment["source_start_ms"]
                         for segment in captures[(sid, media)]
                         if segment["source_start_ms"] <= start and segment["source_end_ms"] >= end]
            position = min(positions) if positions else None
            clips.append({"media_id": media, "sha256": asset["sha256"], "start_ms": start,
                          "end_ms": end, "session_start_ms": position,
                          "session_end_ms": position + end - start if position is not None else None,
                          "asset_created_at": asset["created_at"], "storage_keys": replicas.get(media, []),
                          "utterance_id": original.get("utterance_id")})
        if clips:
            result.append({"prototype_id": row["prototype_id"], "speaker_track_id": row["speaker_track_id"],
                           "cluster_id": row["cluster_id"], "session_id": sid,
                           "date": utc(session["captured_start"]).astimezone(ZoneInfo("Asia/Singapore")).date().isoformat(),
                           "session_captured_start": session["captured_start"],
                           "session_created_at": session["created_at"], "prototype_created_at": row["created_at"],
                           "duration_s": sum((clip["end_ms"] - clip["start_ms"]) / 1000 for clip in clips),
                           "quality_score": row["quality_score"], "clips": clips,
                           "vector": unit(json.loads(row["vector_json"]))})
    return result


def manual_actor(actor):
    return actor == "legacy-human" or actor.startswith(("phone-operation:", "desktop-operation:"))


def truth_coverage(connection, prototype, cache):
    people_ms = collections.Counter()
    covered_ms = 0
    conflicting_ms = 0
    flags = []
    facts = set()
    for clip in prototype["clips"]:
        media, start, end = clip["media_id"], clip["start_ms"], clip["end_ms"]
        if media not in cache:
            cache[media] = [dict(r) for r in connection.execute("""SELECT f.fact_id,f.actor,f.state,f.value_json,
                           a.start_ms,a.end_ms FROM annotation_fact_audio a JOIN annotation_facts f USING(fact_id)
                           WHERE f.dimension='person' AND a.media_id=?""", (media,))]
        candidates = [r for r in cache[media] if r["start_ms"] < end and r["end_ms"] > start and manual_actor(r["actor"])]
        boundaries = {start, end}
        active = []
        for fact in candidates:
            lo, hi = max(start, fact["start_ms"]), min(end, fact["end_ms"])
            boundaries.update((lo, hi))
            facts.add(fact["fact_id"])
            if fact["state"] == "conflict":
                flags.append("conflicting_manual_fact")
            if fact["state"] == "active":
                person = json.loads(fact["value_json"])
                if person:
                    active.append((lo, hi, person))
        points = sorted(boundaries)
        for lo, hi in zip(points[:-1], points[1:], strict=True):
            labels = {person for a, b, person in active if a <= lo and b >= hi}
            if labels:
                covered_ms += hi - lo
                conflicting_ms += (hi - lo) if len(labels) > 1 else 0
                for person in labels:
                    people_ms[person] += hi - lo
    total_ms = round(prototype["duration_s"] * 1000)
    if not people_ms and not flags:
        status = "unlabelled"
    elif len(people_ms) > 1 or conflicting_ms or flags:
        status = "multiple_person_labels"
    elif covered_ms < total_ms:
        status = "incomplete_labels"
    else:
        status = "fully_labelled_single_person"
    return {"truth_status": status, "truth_id": next(iter(people_ms)) if status == "fully_labelled_single_person" else None,
            "labelled_fraction": covered_ms / total_ms if total_ms else 0.0,
            "person_ms": dict(people_ms), "manual_fact_ids": sorted(facts), "review_flags": sorted(set(flags))}


def overlap(a, b):
    return any(x["sha256"] == y["sha256"] and x["start_ms"] < y["end_ms"] and y["start_ms"] < x["end_ms"]
               for x in a["clips"] for y in b["clips"])


def track_near(a, b):
    if a["session_id"] != b["session_id"]:
        return False
    same_track = a["speaker_track_id"] == b["speaker_track_id"]
    same_cluster = bool(a["cluster_id"] and a["cluster_id"] == b["cluster_id"])
    if not (same_track or same_cluster):
        return False
    gap = 5000 if same_track else 2000
    return any(x["session_start_ms"] is not None and y["session_start_ms"] is not None
               and x["session_start_ms"] <= y["session_end_ms"] + gap
               and y["session_start_ms"] <= x["session_end_ms"] + gap
               for x in a["clips"] for y in b["clips"])


def public_clip(clip):
    return {key: value for key, value in clip.items() if key not in ("storage_keys", "asset_created_at")}


def independent_events(prototypes, names):
    parents = list(range(len(prototypes)))

    def root(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    links = []
    for i, a in enumerate(prototypes):
        for j in range(i):
            b = prototypes[j]
            reason = "source_sha_and_time_overlap" if overlap(a, b) else "same_track_or_cluster_time_near" if track_near(a, b) else None
            if reason:
                parents[root(i)] = root(j)
                links.append({"a": a["prototype_id"], "b": b["prototype_id"], "reason": reason})
    groups = collections.defaultdict(list)
    for i, prototype in enumerate(prototypes):
        groups[root(i)].append(prototype)
    events = []
    for members in groups.values():
        ids = sorted(x["prototype_id"] for x in members)
        event_id = hashlib.sha256("|".join(ids).encode()).hexdigest()[:24]
        statuses = [x["truth_status"] for x in members]
        truths = {x["truth_id"] for x in members if x["truth_id"]}
        if len(truths) > 1 or "multiple_person_labels" in statuses:
            status = "multiple_person_labels"
        elif len(truths) == 1 and all(s == "fully_labelled_single_person" for s in statuses):
            status = "fully_labelled_single_person"
        elif all(s == "unlabelled" for s in statuses):
            status = "unlabelled"
        else:
            status = "incomplete_labels"
        ranges = [clip for prototype in members for clip in prototype["clips"]]
        by_source = collections.defaultdict(list)
        for clip in ranges:
            by_source[clip["sha256"]].append((clip["start_ms"], clip["end_ms"]))
        unique_ms = 0
        for spans in by_source.values():
            merged = []
            for lo, hi in sorted(spans):
                if merged and lo <= merged[-1][1]:
                    merged[-1][1] = max(hi, merged[-1][1])
                else:
                    merged.append([lo, hi])
            unique_ms += sum(hi - lo for lo, hi in merged)
        canonical = max(members, key=lambda x: (x["duration_s"], x["prototype_id"]))
        truth = next(iter(truths)) if status == "fully_labelled_single_person" else None
        events.append({"event_id": event_id, "prototype_ids": ids, "prototype_count": len(ids),
                       "session_ids": sorted({x["session_id"] for x in members}),
                       "dates": sorted({x["date"] for x in members}),
                       "speaker_track_ids": sorted({x["speaker_track_id"] for x in members}),
                       "cluster_ids": sorted({x["cluster_id"] for x in members if x["cluster_id"]}),
                       "truth_status": status, "truth_id": truth, "truth_name": names.get(truth),
                       "distinct_label_ids": sorted(truths), "covered_audio_s": unique_ms / 1000,
                       "canonical_prototype_id": canonical["prototype_id"],
                       "ranges": [public_clip(clip) for clip in ranges],
                       "review_flags": sorted({flag for x in members for flag in x["review_flags"]})})
    return sorted(events, key=lambda x: (x["dates"], x["event_id"])), links


def score_prototype(prototype, refs, frozen, names):
    vector = prototype["vector"]
    ranked = sorted(((pid, max(float(vector @ ref) for ref in vectors)) for pid, vectors in refs.items()),
                    key=lambda x: (-x[1], x[0]))
    best, score = ranked[0]
    second, second_score = ranked[1]
    margin = score - second_score
    enough = prototype["duration_s"] >= frozen["minimum_speech_seconds"]
    g = best if enough and score >= frozen["G"]["threshold"] and margin >= frozen["G"]["margin"] else None
    p_threshold = frozen["P"]["per_person"].get(best, frozen["P"]["global_fallback"])
    p = best if enough and score >= p_threshold and margin >= frozen["P"]["margin"] else None
    return {"prototype_id": prototype["prototype_id"], "truth_status": prototype["truth_status"],
            "truth_id": prototype["truth_id"], "truth_name": names.get(prototype["truth_id"]),
            "G_prediction": g, "P_prediction": p, "G_prediction_name": names.get(g), "P_prediction_name": names.get(p),
            "best_person": best, "best_name": names.get(best), "best_score": score,
            "second_person": second, "second_name": names.get(second), "second_score": second_score,
            "margin": margin, "duration_s": prototype["duration_s"], "session_id": prototype["session_id"],
            "date": prototype["date"], "ranges": [public_clip(clip) for clip in prototype["clips"]]}


def count_outcomes(rows, known, self_id):
    counts = collections.Counter()
    for row in rows:
        truth, prediction = row["truth_id"], row["prediction"]
        if truth in known:
            if prediction == truth:
                counts["known_correct"] += 1
            elif prediction is None:
                counts["known_reject"] += 1
            else:
                counts["wrong_known_identity"] += 1
        elif prediction is None:
            counts["unknown_correct_reject"] += 1
        else:
            counts["unknown_false_accept"] += 1
            if truth == self_id:
                counts["self_to_other"] += 1
    return {key: counts[key] for key in ("known_correct", "known_reject", "wrong_known_identity",
                                       "unknown_correct_reject", "unknown_false_accept", "self_to_other")}


def score_events(events, prototype_rows, known, self_id):
    by_id = {x["prototype_id"]: x for x in prototype_rows}
    results = []
    for event in events:
        if event["truth_status"] != "fully_labelled_single_person":
            continue
        members = [by_id[pid] for pid in event["prototype_ids"]]
        canonical = by_id[event["canonical_prototype_id"]]
        result = {"event_id": event["event_id"], "truth_id": event["truth_id"], "truth_name": event["truth_name"],
                  "prototype_count": event["prototype_count"], "prototype_ids": event["prototype_ids"],
                  "session_ids": event["session_ids"], "dates": event["dates"], "duration_s": event["covered_audio_s"],
                  "ranges": event["ranges"], "canonical_prototype_id": event["canonical_prototype_id"],
                  "best_person": canonical["best_person"], "best_score": canonical["best_score"],
                  "second_person": canonical["second_person"], "second_score": canonical["second_score"],
                  "margin": canonical["margin"]}
        for method in ("G", "P"):
            accepted = {row[method + "_prediction"] for row in members if row[method + "_prediction"]}
            # Conservative event-risk audit; this does not change G/P scoring.
            if not accepted:
                prediction = None
            elif event["truth_id"] not in known:
                prediction = sorted(accepted)[0]
            elif any(pid != event["truth_id"] for pid in accepted):
                prediction = sorted(pid for pid in accepted if pid != event["truth_id"])[0]
            else:
                prediction = event["truth_id"]
            result[method + "_prediction"] = prediction
            result[method + "_accepted_ids"] = sorted(accepted)
            result[method + "_mixed_prototypes"] = len(accepted) > 1 or bool(accepted and any(row[method + "_prediction"] is None for row in members))
        results.append(result)
    metrics = {method: count_outcomes([{"truth_id": x["truth_id"], "prediction": x[method + "_prediction"]}
                                      for x in results], known, self_id) for method in ("G", "P")}
    tri = collections.Counter()
    for row in results:
        zone = "auto_commit_candidate" if row["P_prediction"] else "suggestion" if row["G_prediction"] else "unknown"
        tri[("known" if row["truth_id"] in known else "unknown") + "_" + zone] += 1
        if zone == "auto_commit_candidate" and row["P_prediction"] != row["truth_id"]:
            tri["incorrect_auto_commit_candidate"] += 1
    return results, metrics, dict(tri)


def blind_exclusions(prototype, frozen, old_sessions, old_hashes, old_ids):
    cutoff = utc(frozen["freeze_utc"])
    reasons = []
    if prototype["session_id"] in old_sessions:
        reasons.append("session_in_v2")
    if prototype["prototype_id"] in old_ids:
        reasons.append("prototype_in_v2")
    for field in ("session_captured_start", "session_created_at", "prototype_created_at"):
        if utc(prototype[field]) <= cutoff:
            reasons.append(field + "_before_freeze")
    if any(utc(clip["asset_created_at"]) <= cutoff for clip in prototype["clips"]):
        reasons.append("source_audio_created_before_freeze")
    if any(clip["sha256"] in old_hashes for clip in prototype["clips"]):
        reasons.append("source_sha_used_in_v2")
    if any(not any((ROOT / "state/v3/audio" / key).is_file() for key in clip["storage_keys"])
           for clip in prototype["clips"]):
        reasons.append("local_audio_missing")
    return reasons


def review_clips(event, prototypes):
    from allday_asr.v3.adapters.audio.tools import extract_clip

    source = prototypes[event["risk_prototype_ids"][0]]
    folder = OUT / "review-clips"
    folder.mkdir(exist_ok=True)
    clips = []
    for index, clip in enumerate(source["clips"]):
        path = next((ROOT / "state/v3/audio" / key for key in clip["storage_keys"]
                     if (ROOT / "state/v3/audio" / key).is_file()), None)
        if path is None:
            continue
        destination = folder / f"{event['event_id']}-{index:02d}.wav"
        extract_clip(path, destination, clip["start_ms"], clip["end_ms"])
        clips.append({"path": str(destination), "media_id": clip["media_id"],
                      "start_ms": clip["start_ms"], "end_ms": clip["end_ms"]})
    return clips


def risk_truth_evidence(connection, event, prototypes):
    manual_ids = sorted({fid for pid in event["prototype_ids"] for fid in prototypes[pid]["manual_fact_ids"]})
    system_disagreements = []
    seen = set()
    for clip in event["ranges"]:
        for row in connection.execute("""SELECT f.fact_id,f.value_json,a.start_ms,a.end_ms FROM annotation_fact_audio a
                          JOIN annotation_facts f USING(fact_id) WHERE f.dimension='person' AND f.state='active'
                          AND f.actor LIKE 'system:%' AND a.media_id=? AND a.start_ms<? AND a.end_ms>?""",
                          (clip["media_id"], clip["end_ms"], clip["start_ms"])):
            person = json.loads(row["value_json"])
            key = (row["fact_id"], clip["media_id"])
            if person != event["truth_id"] and key not in seen:
                seen.add(key)
                system_disagreements.append({"fact_id": row["fact_id"], "predicted_person_id": person,
                                             "media_id": clip["media_id"], "start_ms": row["start_ms"],
                                             "end_ms": row["end_ms"]})
    return {"manual_fact_ids": manual_ids,
            "manual_truth_complete_on_all_representatives": True,
            "system_prediction_disagreements": system_disagreements,
            "acoustic_review_status": "not independently listened; overlap, mixing, boundary purity and track contamination unverified",
            "review_flags": ["acoustic_purity_unverified"] + (["system_projection_disagrees_with_manual_truth"] if system_disagreements else [])}


def track_timelines(candidates, refs, frozen, names):
    """Same-model clip embedding; descriptive cumulative scores, no commitment."""
    from allday_asr.v3.adapters.files import ContentAddressedStore
    from allday_asr.v3.adapters.models.funasr import FunASRBackend
    from allday_asr.v3.adapters.speaker_embeddings.funasr import FunASRSpeakerEmbeddingProvider
    from allday_asr.v3.ports.speaker_embeddings import SpeakerClipInput, SpeakerTrackInput
    import torch

    cache_path = OUT / "timeline-vectors.npz"
    cache = dict(np.load(cache_path)) if cache_path.exists() else {}
    unique = {}
    for prototype in candidates:
        for clip in prototype["clips"]:
            key = hashlib.sha256(f"{clip['sha256']}:{clip['start_ms']}:{clip['end_ms']}".encode()).hexdigest()[:24]
            clip["timeline_key"] = key
            unique[key] = clip
    todo = [(key, clip) for key, clip in unique.items() if key not in cache]
    if todo:
        torch.set_num_threads(4)
        backend = FunASRBackend(device="cuda:0")
        provider = FunASRSpeakerEmbeddingProvider(ContentAddressedStore(ROOT / "state/v3/audio"),
                                                  backend_factory=lambda: backend, temp_root=OUT / "temp")
        for start in range(0, len(todo), 16):
            batch = []
            for key, clip in todo[start:start + 16]:
                storage = next((item for item in clip["storage_keys"]
                                if (ROOT / "state/v3/audio" / item).is_file()), None)
                if storage:
                    batch.append(SpeakerTrackInput(key, "timeline", (SpeakerClipInput(
                        clip["media_id"], storage, clip["start_ms"], clip["end_ms"], None),)))
            for row in provider.embed(tuple(batch)):
                cache[row.speaker_track_id] = np.asarray(row.vector, dtype=np.float32)
            np.savez(cache_path, **cache)
    tracks = []
    for prototype in candidates:
        ordered = sorted(prototype["clips"], key=lambda x: (x["session_start_ms"] is None,
                         x["session_start_ms"] if x["session_start_ms"] is not None else x["start_ms"], x["media_id"]))
        accumulated = []
        speech_s = 0.0
        points = []
        for index, clip in enumerate(ordered):
            if clip["timeline_key"] not in cache:
                continue
            accumulated.append(unit(cache[clip["timeline_key"]]))
            speech_s += (clip["end_ms"] - clip["start_ms"]) / 1000
            query = prototype | {"vector": unit(np.mean(accumulated, axis=0)), "duration_s": speech_s}
            scored = score_prototype(query, refs, frozen, names)
            points.append({"speech_t_s": speech_s, "segment_index": index,
                           "source": {k: clip[k] for k in ("media_id", "start_ms", "end_ms", "session_start_ms")},
                           "scores": {pid: max(float(query["vector"] @ ref) for ref in values) for pid, values in refs.items()},
                           "best_person": scored["best_person"], "best_score": scored["best_score"],
                           "second_person": scored["second_person"], "second_score": scored["second_score"],
                           "margin": scored["margin"], "G_status": scored["G_prediction"], "P_status": scored["P_prediction"]})
        final_cosine = float(unit(np.mean(accumulated, axis=0)) @ prototype["vector"]) if accumulated else None
        tracks.append({"prototype_id": prototype["prototype_id"], "truth_id": prototype["truth_id"],
                       "session_id": prototype["session_id"], "date": prototype["date"], "points": points,
                       "final_embedding_cosine_to_stored_prototype": final_cosine})
    reaching = {str(t): sum(bool(row["points"]) and row["points"][-1]["speech_t_s"] >= t for row in tracks)
                for t in (1, 3, 5, 10, 20)}
    mismatch = [row["prototype_id"] for row in tracks if row["final_embedding_cosine_to_stored_prototype"] is not None
                and row["final_embedding_cosine_to_stored_prototype"] < 0.99]
    return {"scope": "historical real-track representative clips; cumulative speech seconds, not wall-clock latency",
            "tracks": tracks, "clip_vectors_cached": len(cache), "tracks_reaching_seconds": reaching,
            "final_vector_mismatch_below_0_99": mismatch,
            "temporal_commitment_conclusion": "insufficient evidence for temporal commitment"}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    protocol = """# Speaker Identity Blind Validation protocol

G/P and enrollment are loaded verbatim from V2. Session capture, session
creation, prototype creation and original audio creation must all postdate the
local V2 freeze marker. Old session/prototype IDs and original SHA are excluded.
Ground truth requires complete active manual-person coverage without conflicting
human facts. System identity facts and model predictions never supply truth.
Independent events are connected components of original SHA/time overlap, or
the same track/cluster near in session time (2 seconds, 5 for same track).
All member prototypes need one consistent complete manual truth to be scored.
Historical V2 audio is diagnostic only, never blind acceptance evidence.
For event-risk audit, any accepted wrong person is one error; otherwise any
correct acceptance is one correct event; all rejections are one rejection.
This is an analysis reduction, not a new production matcher. Timeline prefixes
use the same CAM++ and G/P gates and do not imply permanent first-pass commit.
"""
    (OUT / "PROTOCOL.md").write_text(protocol, encoding="utf-8")
    manifest, frozen = frozen_sources()
    runtime = read(PILOT / "runtime.json")
    if sha(Path(runtime["model_file"])) != frozen["model_sha256"]:
        raise RuntimeError("Current CAM++ weight differs from frozen V2 model")
    refs = frozen_profile(manifest)
    connection = open_db()
    sessions, assets, tracks, replicas, captures = data_tables(connection)
    prototypes = candidate_prototypes(connection, sessions, assets, tracks, replicas, captures)
    cache = {}
    for prototype in prototypes:
        prototype.update(truth_coverage(connection, prototype, cache))
    by_id = {p["prototype_id"]: p for p in prototypes}
    v2_tracks = read(V2 / "track-level-results.json")["tracks"]
    old_ids = {row["prototype_id"] for row in v2_tracks}
    old_sessions = {row["session_id"] for row in v2_tracks}
    old_sessions.update(row["session_id"] for row in manifest["groups"])
    old_hashes = {row["sha256"] for row in manifest["windows"]}
    for row in v2_tracks:
        for clip in row["clips"]:
            if clip["media_id"] in assets:
                old_hashes.add(assets[clip["media_id"]]["sha256"])
    historical = [by_id[pid] for pid in sorted(old_ids) if pid in by_id]
    assert len(historical) == len(old_ids) == 37
    historical_full = [p for p in historical if p["truth_status"] == "fully_labelled_single_person"]
    old_counts = collections.Counter(row["status"] for row in v2_tracks)
    current_counts = collections.Counter(row["truth_status"] for row in historical)
    changed = {key: {"v2": old_counts[key], "now": current_counts[key]}
               for key in set(old_counts) | set(current_counts) if old_counts[key] != current_counts[key]}
    cutoff = utc(frozen["freeze_utc"])
    new_sessions = [s for s in sessions.values() if utc(s["captured_start"]) > cutoff
                    and utc(s["created_at"]) > cutoff and s["session_id"] not in old_sessions]
    new_ids = {s["session_id"] for s in new_sessions}
    new_prototypes = [p for p in prototypes if p["session_id"] in new_ids]
    for prototype in new_prototypes:
        prototype["blind_exclusion_reasons"] = blind_exclusions(prototype, frozen, old_sessions, old_hashes, old_ids)
    fresh = [p for p in new_prototypes if not p["blind_exclusion_reasons"]]
    names = manifest["names"]
    known = set(frozen["known_ids"])
    self_id = next(pid for pid, name in names.items() if name == "我")
    old_events, old_links = independent_events(historical_full, names)
    old_all_events, _ = independent_events(historical, names)
    blind_events, blind_links = independent_events(fresh, names)
    blind_full = [e for e in blind_events if e["truth_status"] == "fully_labelled_single_person"]
    historical_scores = [score_prototype(p, refs, frozen, names) for p in historical]
    blind_scores = [score_prototype(p, refs, frozen, names) for p in fresh]
    old_event_rows, old_metrics, old_tri = score_events(old_events, historical_scores, known, self_id)
    blind_rows, blind_metrics, blind_tri = score_events(blind_full, blind_scores, known, self_id)
    scored_proto = [x for x in historical_scores if x["truth_status"] == "fully_labelled_single_person"]
    proto_metrics = {method: count_outcomes([{"truth_id": x["truth_id"], "prediction": x[method + "_prediction"]}
                                             for x in scored_proto], known, self_id) for method in ("G", "P")}
    if not changed:
        assert proto_metrics["G"]["known_correct"] == 4
        assert proto_metrics["G"]["unknown_false_accept"] == 3
        assert proto_metrics["P"]["unknown_false_accept"] == 0
    risk_cases = []
    for scope, event_rows, prototype_rows in (("historical", old_event_rows, historical_scores),
                                               ("blind", blind_rows, blind_scores)):
        for row in event_rows:
            if not any(row[method + "_prediction"] is not None and row[method + "_prediction"] != row["truth_id"]
                       for method in ("G", "P")):
                continue
            related = [x for x in prototype_rows if x["prototype_id"] in row["prototype_ids"]]
            bad_ids = [x["prototype_id"] for x in related if x["G_prediction"] is not None
                       and x["G_prediction"] != row["truth_id"]]
            case = row | {"scope": scope, "risk_prototype_ids": bad_ids, "prototype_scores": related}
            case["truth_evidence"] = risk_truth_evidence(connection, case, by_id)
            if row["truth_id"] == self_id and bad_ids:
                case["review_clips"] = review_clips(case, by_id)
            risk_cases.append(case)
    review = [{"prototype_id": p["prototype_id"], "session_id": p["session_id"], "date": p["date"],
               "status": p["truth_status"], "labelled_fraction": p["labelled_fraction"],
               "person_ms": p["person_ms"], "review_flags": p["review_flags"],
               "ranges": [public_clip(x) for x in p["clips"]]}
              for p in historical + fresh if p["truth_status"] != "fully_labelled_single_person"]
    save("needs-human-review.json", {"count": len(review), "prototypes": review})
    save("independent-events.json", {"historical_scored": old_events, "historical_all": old_all_events,
          "blind_discovered": blind_events, "merge_links": {"historical": old_links, "blind": blind_links}})
    composition = collections.Counter("known" if e["truth_id"] in known else "unknown" for e in blind_full)
    desired_ids = set(frozen["known_ids"])
    desired_ids.update(g["person_id"] for g in manifest["groups"] if g["split"] == "test")
    missing = [names[pid] for pid in sorted(desired_ids)
               if not any(e["truth_id"] == pid for e in blind_full)]
    if not any(e["truth_id"] not in desired_ids for e in blind_full):
        missing.append("new_stranger")
    blind_status = "READY" if blind_full else "NO INDEPENDENT BLIND ACCEPTANCE DATA YET"
    save("blind-manifest.json", {"status": blind_status, "freeze_utc": frozen["freeze_utc"],
          "new_session_ids": sorted(new_ids),
          "new_sessions": [{"session_id": x["session_id"], "captured_start": x["captured_start"],
                            "created_at": x["created_at"]} for x in new_sessions],
          "new_original_prototypes": len(new_prototypes), "fresh_prototypes": len(fresh),
          "eligible_events": len(blind_full),
          "eligible_sessions": len({sid for e in blind_full for sid in e["session_ids"]}),
          "eligible_dates": sorted({date for e in blind_full for date in e["dates"]}),
          "composition": dict(composition), "missing_coverage": missing,
          "excluded_new_prototypes": [{"prototype_id": p["prototype_id"],
                                      "reasons": p["blind_exclusion_reasons"]}
                                     for p in new_prototypes if p["blind_exclusion_reasons"]],
          "events": blind_events})
    revision = connection.execute("SELECT revision FROM annotation_input_revision").fetchone()[0]
    source_overlap = sorted({clip["sha256"] for p in fresh for clip in p["clips"] if clip["sha256"] in old_hashes})
    assert not source_overlap
    save("data-leakage-audit.json", {"v2_source_sha_count": len(old_hashes),
          "v2_session_count": len(old_sessions), "v2_original_prototype_count": len(old_ids),
          "freeze_utc": frozen["freeze_utc"], "v2_annotation_revision": frozen["v2_annotation_revision"],
          "current_annotation_revision": revision, "new_sessions": len(new_sessions),
          "fresh_prototypes": len(fresh), "fresh_source_sha_overlap": source_overlap,
          "historical_label_status_change": changed,
          "gate": "capture/session/prototype/audio created after freeze; old session/prototype/source SHA excluded"})
    save("prototype-level-diagnostics.json", {"label": "diagnostic only; not independent evidence",
          "historical_fully_labelled_metrics": proto_metrics,
          "historical_rows": historical_scores, "blind_rows": blind_scores})
    save("event-level-results.json", {"blind": {"status": blind_status, "events": blind_rows,
          "metrics": blind_metrics if blind_rows else None,
          "three_level_probe": blind_tri if blind_rows else None},
          "historical_diagnostic_only": {"events": old_event_rows, "metrics": old_metrics,
          "three_level_probe": old_tri},
          "event_reduction": "Any wrong accepted identity is one error; otherwise any correct acceptance is one correct event; all reject means reject. This is not a production matcher."})
    save("failure-cases.json", risk_cases)
    try:
        temporal = track_timelines(historical_full + [p for p in fresh if p["truth_status"] == "fully_labelled_single_person"],
                                   refs, frozen, names)
    except Exception as error:
        temporal = {"status": "insufficient evidence for temporal commitment",
                    "reason": f"Same-model clip embedding unavailable: {type(error).__name__}: {error}",
                    "track_count": len(historical_full), "thresholds_unchanged": True}
    save("timeline-results.json", temporal)
    connection.close()
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    save("verification.json", {"python": platform.python_version(), "numpy": np.__version__,
          "git_base_commit": commit, "database_opened_readonly": True, "database_revision": revision,
          "frozen_source_sha256": frozen["source_sha256"], "model": frozen["model"],
          "model_sha256": frozen["model_sha256"], "G": frozen["G"], "P": frozen["P"],
          "historical_prototypes_scored": len(historical_full),
          "historical_scored_independent_events": len(old_events),
          "blind_scored_independent_events": len(blind_full), "new_sessions": len(new_sessions),
          "thresholds_retuned": False, "historical_data_used_as_blind": False})
    print(json.dumps({"blind_status": blind_status, "new_sessions": len(new_sessions),
                      "blind_events": len(blind_full), "historical_events": len(old_events),
                      "historical_G": old_metrics["G"], "historical_P": old_metrics["P"],
                      "output": str(OUT)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
