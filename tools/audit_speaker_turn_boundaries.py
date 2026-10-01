"""Read-only boundary audit; all identifying evidence stays in ignored outputs/.

Run with .venv/Scripts/python.exe tools/audit_speaker_turn_boundaries.py.
No model inference, production service construction, database writes or new tasks.
Counts of predicted turns are never treated as acoustic ground truth.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from bisect import bisect_left, bisect_right
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from allday_asr.v3.adapters.models.native_projection import (  # noqa: E402
    _group_utterances,
    _speaker_for,
)
from allday_asr.v3.adapters.models.speech_gate import (  # noqa: E402
    _merge_speech_ranges,
)
from allday_asr.v3.adapters.sqlite.blind_queries import (  # noqa: E402
    automatic_queries,
)
from allday_asr.v3.interfaces.review_audio import audio_plan  # noqa: E402


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def open_readonly(path):
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("BEGIN")
    return connection


def intersects(span, start, end):
    return span["start_ms"] < end and span["end_ms"] > start


def attributed_tokens(tokens, turns, threshold):
    """Exact _speaker_for replay on intersecting exclusive turns only."""
    ordered = sorted(turns, key=lambda t: (t["start_ms"], t["end_ms"]))
    nonoverlap = all(
        a["end_ms"] <= b["start_ms"] for a, b in zip(ordered, ordered[1:], strict=False)
    )
    starts = [t["start_ms"] for t in ordered]
    ends = [t["end_ms"] for t in ordered]
    result = []
    for token in tokens:
        relevant = (
            ordered[
                bisect_right(ends, token["start_ms"]) : bisect_left(
                    starts, token["end_ms"]
                )
            ]
            if nonoverlap
            else ordered
        )
        result.append({**token, "speaker": _speaker_for(token, relevant, threshold)})
    return result


def union(ranges):
    result = []
    for start, end in sorted(ranges):
        if end <= start:
            continue
        if result and start <= result[-1][1]:
            result[-1][1] = max(end, result[-1][1])
        else:
            result.append([start, end])
    return result


def span_size(ranges):
    return sum(end - start for start, end in union(ranges))


def overlap_predictions(turns, start, end):
    """Different-label overlap in regular output, NOT verified simultaneous speech."""
    near = [turn for turn in turns if intersects(turn, start, end)]
    return union(
        (max(a["start_ms"], b["start_ms"], start), min(a["end_ms"], b["end_ms"], end))
        for i, a in enumerate(near)
        for b in near[:i]
        if a["speaker_label"] != b["speaker_label"]
    )


def aba_candidates(turns, utterances, labels, tokens, threshold):
    """Single-run adjacent exclusive A-B-A candidates with explicit denominators."""
    result = []
    ordered = sorted(turns, key=lambda t: (t["start_ms"], t["end_ms"]))
    for i in range(1, len(ordered) - 1):
        a, b, resumed = ordered[i - 1 : i + 2]
        duration = b["end_ms"] - b["start_ms"]
        if not (
            a["speaker_label"] == resumed["speaker_label"] != b["speaker_label"]
            and 0 < duration <= 2000
            and a["end_ms"] - a["start_ms"] >= 2000
            and 0 <= b["start_ms"] - a["end_ms"] <= 1200
            and 0 <= resumed["start_ms"] - b["end_ms"] <= 1200
        ):
            continue
        near_tokens = [t for t in tokens if intersects(t, b["start_ms"], b["end_ms"])]
        attributed = [
            {**t, "assigned_speaker": _speaker_for(t, ordered, threshold)}
            for t in near_tokens
        ]
        bridges = [
            u["utterance_id"]
            for u in utterances
            if u["start_ms"] < b["start_ms"]
            and u["end_ms"] > b["end_ms"]
            and labels.get(u["original_speaker_track_id"]) == a["speaker_label"]
        ]
        b_utterances = [
            u["utterance_id"]
            for u in utterances
            if intersects(u, b["start_ms"], b["end_ms"])
            and labels.get(u["original_speaker_track_id"]) == b["speaker_label"]
        ]
        a_utterances = [
            u["utterance_id"]
            for u in utterances
            if intersects(u, a["start_ms"], a["end_ms"])
            and labels.get(u["original_speaker_track_id"]) == a["speaker_label"]
        ]
        resumed_utterances = [
            u["utterance_id"]
            for u in utterances
            if intersects(u, resumed["start_ms"], resumed["end_ms"])
            and labels.get(u["original_speaker_track_id"]) == resumed["speaker_label"]
        ]
        outcome = (
            "downstream_remerged"
            if bridges
            else "b_label_preserved"
            if b_utterances
            else "b_not_projected_no_bridge"
        )
        result.append(
            {
                "a": a,
                "b": b,
                "a_resumes": resumed,
                "b_duration_ms": duration,
                "outcome": outcome,
                "bridging_utterance_ids": bridges,
                "b_utterance_ids": b_utterances,
                "a_utterance_ids": a_utterances,
                "a_resumes_utterance_ids": resumed_utterances,
                "three_projected_turns_preserved": bool(
                    a_utterances and b_utterances and resumed_utterances and not bridges
                ),
                "b_intersecting_tokens": attributed,
                "token_loss": "no_primary_token"
                if not near_tokens
                else "no_b_majority_token"
                if not any(
                    t["assigned_speaker"] == b["speaker_label"] for t in attributed
                )
                else "b_token_present",
                "acoustic_truth": "unverified_candidate",
                "laughter": "LAUGHTER_BACKCHANNEL_CANDIDATE"
                if any("哈" in t["text"] or "笑" in t["text"] for t in near_tokens)
                else "UNKNOWN",
            }
        )
    return result


def threshold_probes():
    results = []
    for duration in (300, 500, 1000):
        turns = [
            {"start_ms": 0, "end_ms": 4000, "speaker_label": "A"},
            {"start_ms": 4000, "end_ms": 4000 + duration, "speaker_label": "B"},
            {"start_ms": 4000 + duration, "end_ms": 9000, "speaker_label": "A"},
        ]
        a_tokens = [
            {
                "start_ms": 0,
                "end_ms": 4000,
                "text": "A",
                "speaker": "A",
                "source_refs": [],
            },
            {
                "start_ms": 4000 + duration,
                "end_ms": 9000,
                "text": "A",
                "speaker": "A",
                "source_refs": [],
            },
        ]
        present = [
            a_tokens[0],
            {
                "start_ms": 4000,
                "end_ms": 4000 + duration,
                "text": "B",
                "speaker": "B",
                "source_refs": [],
            },
            a_tokens[1],
        ]
        results.append(
            {
                "b_duration_ms": duration,
                "diarization_turns": turns,
                "b_token_absent_grouped_ranges": _group_utterances(a_tokens),
                "b_token_present_grouped_ranges": _group_utterances(present),
                "vad_merge_example": _merge_speech_ranges(
                    [(0, 4000), (4000 + duration, 9000)],
                    9000,
                    merge_gap_ms=600,
                    max_utterance_ms=30000,
                ),
                "enrollment_positive_gap_is_union_merged": len(
                    union([(0, 4000), (4000 + duration, 9000)])
                )
                == 1,
            }
        )
    return results


class Audit:
    def __init__(self, connection, db):
        self.c = connection
        self.state = db.parent
        self.runs = {}
        self.input_hashes = {}
        self.audio = {}
        self.traces = []
        self.cases = []
        self.boundaries = []
        self.tracks = {
            r["speaker_track_id"]: dict(r)
            for r in connection.execute(
                "SELECT * FROM speaker_tracks ORDER BY speaker_track_id"
            )
        }
        self.people = {
            r["person_id"]: r["display_name"]
            for r in connection.execute(
                "SELECT person_id,display_name FROM persons ORDER BY person_id"
            )
        }

    def rows(self, sql, args=()):
        return [dict(r) for r in self.c.execute(sql, args)]

    def run(self, run_id):
        if run_id in self.runs:
            return self.runs[run_id]
        artifacts, refs = {}, []
        for row in self.rows(
            "SELECT * FROM artifacts WHERE run_id=? ORDER BY artifact_id", (run_id,)
        ):
            if row["kind"] not in {
                "v3_asr_evidence",
                "v3_diarization_evidence",
                "v3_transcript_evidence",
            }:
                continue
            path = (self.state / "artifacts" / row["storage_ref"]).resolve()
            if not path.is_relative_to((self.state / "artifacts").resolve()):
                raise ValueError("artifact path escaped store")
            if not path.is_file():
                refs.append({"kind": row["kind"], "availability": "missing"})
                continue
            digest = sha(path)
            if digest != row["sha256"]:
                raise ValueError("artifact checksum mismatch")
            self.input_hashes[str(path)] = digest
            artifacts[row["kind"]] = json.loads(path.read_text(encoding="utf-8"))
            refs.append(
                {
                    "artifact_id": row["artifact_id"],
                    "kind": row["kind"],
                    "sha256": digest,
                    "status": row["status"],
                }
            )
        diar = artifacts.get("v3_diarization_evidence", {})
        asr = artifacts.get("v3_asr_evidence", {})
        utterances = self.rows(
            "SELECT utterance_id,start_ms,end_ms,original_text AS text,"
            "original_speaker_track_id,speaker_track_id,evidence_json "
            "FROM utterances WHERE run_id=? ORDER BY start_ms,end_ms,utterance_id",
            (run_id,),
        )
        labels = {
            key: value["label"]
            for key, value in self.tracks.items()
            if value["run_id"] == run_id
        }
        threshold = 0.5  # Persisted native artifacts do not save projection threshold.
        data = {
            "artifacts": refs,
            "asr": asr,
            "diarization": diar,
            "utterances": utterances,
            "labels": labels,
            "threshold": threshold,
            "threshold_basis": "local config; replay verified for Blind",
            "transcript": artifacts.get("v3_transcript_evidence", {}),
        }
        self.runs[run_id] = data
        return data

    def captures(self, session_id, media_id=None):
        return self.rows(
            "SELECT s.*,a.media_id,a.sha256,a.duration_ms,r.storage_key "
            "FROM capture_segments s JOIN audio_assets a USING(asset_id) "
            "LEFT JOIN audio_replicas r USING(replica_id) "
            "WHERE s.session_id=? AND (? IS NULL OR a.media_id=?) "
            "ORDER BY s.sequence,s.segment_id",
            (session_id, media_id, media_id),
        )

    def audio_source(self, media_id):
        if media_id not in self.audio:
            rows = self.rows(
                "SELECT a.*,r.storage_key,r.state FROM audio_assets a "
                "LEFT JOIN audio_replicas r USING(asset_id) WHERE media_id=? "
                "ORDER BY r.storage_key",
                (media_id,),
            )
            evidence = []
            for row in rows:
                key = row["storage_key"]
                path = (self.state / "audio" / key).resolve() if key else None
                if path and not path.is_relative_to((self.state / "audio").resolve()):
                    raise ValueError("audio path escaped store")
                available = bool(path and path.is_file())
                checksum = sha(path) if available else None
                if available:
                    self.input_hashes[str(path)] = checksum
                evidence.append(
                    {
                        **row,
                        "local_path": str(path) if path else None,
                        "file_exists": available,
                        "file_sha256": checksum,
                        "checksum_matches": checksum == row["sha256"]
                        if available
                        else None,
                    }
                )
            self.audio[media_id] = evidence
        return self.audio[media_id]

    def window_trace(self, case_id, session_id, run_id, window, label=None):
        data = self.run(run_id)
        start, end = window["session_start_ms"], window["session_end_ms"]
        lo, hi = max(0, start - 5000), end + 5000
        tokens = [
            t for t in data["asr"].get("primary_tokens", []) if intersects(t, lo, hi)
        ]
        exclusive = data["diarization"].get("exclusive_turns", [])
        regular = data["diarization"].get("regular_turns", [])
        assigned = [
            {**t, "assigned_speaker": _speaker_for(t, exclusive, data["threshold"])}
            for t in tokens
        ]
        utterances = [u for u in data["utterances"] if intersects(u, lo, hi)]
        gate = []
        for hypothesis in data["asr"].get("hypotheses", []):
            if hypothesis.get("role") != "primary":
                continue
            offset = hypothesis["analysis_start_ms"]
            raw = hypothesis.get("raw_response", {})
            for kind in (
                "candidate_ranges_ms",
                "silero_ranges_ms",
                "speech_ranges_ms",
                "inference_ranges_ms",
            ):
                for a, b in raw.get(kind, []):
                    if a + offset < hi and b + offset > lo:
                        gate.append(
                            {
                                "kind": kind,
                                "start_ms": a + offset,
                                "end_ms": b + offset,
                                "asr_core_start_ms": hypothesis["core_start_ms"],
                                "asr_core_end_ms": hypothesis["core_end_ms"],
                            }
                        )
        foreign = union(
            (max(t["start_ms"], start), min(t["end_ms"], end))
            for t in exclusive
            if label and t["speaker_label"] != label
        )
        internal_foreign = [
            t
            for t in exclusive
            if label
            and t["speaker_label"] != label
            and start < t["start_ms"] < t["end_ms"] < end
        ]
        overlaps = overlap_predictions(regular, start, end)
        uid = window.get("utterance_id")
        source_u = next(
            (u for u in data["utterances"] if u["utterance_id"] == uid), None
        )
        boundary = None
        if source_u and end < source_u["end_ms"]:
            capture_ends = {
                c["session_end_ms"]
                for c in self.captures(session_id, window["media_id"])
            }
            at_capture = end in capture_ends
            at_cap = end - start == 8000
            boundary = {
                "case_id": case_id,
                "window": window,
                "first_bad_layer": "FIXED_WINDOW_ONLY",
                "confidence": "HIGH",
                "source_utterance": source_u,
                "remaining_utterance_ms": source_u["end_ms"] - end,
                "causes": (["QUERY_8S_CAP"] if at_cap else [])
                + (["QUERY_CAPTURE_SEGMENT_END"] if at_capture else []),
                "token_straddles_end": any(
                    t["start_ms"] < end < t["end_ms"] for t in tokens
                ),
                "same_speaker_exclusive_turn_straddles_end": any(
                    t["speaker_label"] == label and t["start_ms"] < end < t["end_ms"]
                    for t in exclusive
                ),
                "vad_speech_straddles_end": any(
                    t["kind"] == "speech_ranges_ms"
                    and t["start_ms"] < end < t["end_ms"]
                    for t in gate
                ),
                "raw_speech_continuity": "not independently listened; ASR/diarization continuation evidence",
                "context_start_ms": lo,
                "context_end_ms": hi,
                "model_query_affected": True,
                "review_only_affected": False,
                "boundary_cut_semantically_confirmed": False,
            }
            self.boundaries.append(boundary)
        trace = {
            "case_id": case_id,
            "session_id": session_id,
            "run_id": run_id,
            "media_id": window["media_id"],
            "window": window,
            "coordinate_system": "all layer timestamps=session ms; window source timestamps=media ms",
            "capture_media": self.captures(session_id, window["media_id"]),
            "raw_audio": self.audio_source(window["media_id"]),
            "raw_speech_spans": None,
            "raw_speech_spans_reason": "no fine-grained acoustic truth",
            "artifact_provenance": data["artifacts"],
            "vad": gate,
            "asr_tokens": assigned,
            "asr_utterances": utterances,
            "diarization_exclusive": [t for t in exclusive if intersects(t, lo, hi)],
            "diarization_regular": [t for t in regular if intersects(t, lo, hi)],
            "foreign_exclusive_ms": span_size(foreign),
            "foreign_exclusive_ranges": foreign,
            "fully_internal_foreign_turns": internal_foreign,
            "regular_overlap_prediction_ms": span_size(overlaps),
            "regular_overlap_prediction_ranges": overlaps,
            "source_utterance_to_query": {
                "input": [source_u["start_ms"], source_u["end_ms"]]
                if source_u
                else None,
                "output": [start, end],
                "expands": False,
            },
            "boundary": boundary,
        }
        self.traces.append(trace)
        return trace

    def blind(self):
        tasks = self.rows(
            "SELECT t.*,q.query_json,q.experiment_id,e.session_id,e.query_ids_json,e.grouping_json,"
            "e.canonical_query_id FROM blind_review_tasks t JOIN blind_events e USING(event_id) "
            "JOIN blind_query_views q USING(query_id) WHERE e.superseded_by IS NULL "
            "ORDER BY t.created_at,t.task_id"
        )
        truth = {
            r["task_id"]: r
            for r in self.rows(
                "SELECT g.* FROM blind_ground_truth g WHERE "
                "NOT EXISTS(SELECT 1 FROM blind_ground_truth n WHERE n.task_id=g.task_id AND n.revision>g.revision) "
                "ORDER BY g.task_id"
            )
        }
        for index, task in enumerate(tasks, 1):
            case_id = f"blind-{index:02}"
            query = json.loads(task.pop("query_json"))
            task["query_ids"] = json.loads(task.pop("query_ids_json"))
            task["grouping"] = json.loads(task.pop("grouping_json"))
            track = self.tracks[query["speaker_track_id"]]
            windows = [
                {k: v for k, v in w.items() if k != "embedding"}
                for w in query["windows"]
            ]
            traces = [
                self.window_trace(
                    case_id, query["session_id"], query["run_id"], w, track["label"]
                )
                for w in windows
            ]
            plan = audio_plan(
                {"prototype_id": task["task_id"], "representative_clips": windows}
            )
            regenerated = automatic_queries(
                self.c, query["session_id"], task["experiment_id"], query["run_id"]
            )
            replay = next(
                (q for q in regenerated if q["query_id"] == query["query_id"]), None
            )

            def comparable(ws):
                return [{k: v for k, v in w.items() if k != "embedding"} for w in ws]

            ranges_replay = bool(
                replay and comparable(replay["windows"]) == comparable(query["windows"])
            )
            vectors = [w["embedding"] for w in query["windows"]]
            mean = [
                sum(v[i] for v in vectors) / len(vectors)
                for i in range(len(vectors[0]))
            ]
            norm = sum(v * v for v in mean) ** 0.5
            vector_error = max(
                abs(a / norm - b) for a, b in zip(mean, query["vector"], strict=True)
            )
            data = self.run(query["run_id"])
            if "projection_replay" not in data:
                projected = _group_utterances(
                    attributed_tokens(
                        data["asr"].get("primary_tokens", []),
                        data["diarization"].get("exclusive_turns", []),
                        data["threshold"],
                    )
                )
                data["projection_replay"] = projected == data["transcript"].get(
                    "utterances"
                )
            projection_replay = data["projection_replay"]
            human = truth.get(task["task_id"])
            mixed = bool(
                human
                and human["action"] == "submit"
                and human["purity"] == "mixed_overlap"
            )
            case = {
                "case_id": case_id,
                "cohort": "latest_blind",
                "review_task": task,
                "truth": human,
                "speaker_track": track,
                "track_definition": "identity grouping within run/session; not contiguous turn",
                "track_membership": [
                    {
                        "utterance_id": u["utterance_id"],
                        "start_ms": u["start_ms"],
                        "end_ms": u["end_ms"],
                    }
                    for u in data["utterances"]
                    if u["original_speaker_track_id"] == track["speaker_track_id"]
                ],
                "cluster_memberships": self.rows(
                    "SELECT * FROM speaker_cluster_memberships "
                    "WHERE speaker_track_id=? ORDER BY membership_id",
                    (track["speaker_track_id"],),
                ),
                "prototype_sources": self.rows(
                    "SELECT prototype_id,cluster_id,status,source_prototype_id,"
                    "representative_clips_json FROM voice_prototypes WHERE speaker_track_id=? ORDER BY prototype_id",
                    (track["speaker_track_id"],),
                ),
                "query_source_windows": windows,
                "model_query_composition": "unit mean of unit window embeddings",
                "model_mean_max_abs_error": vector_error,
                "builder_replay_matches": ranges_replay,
                "projection_replay_matches": projection_replay,
                "review_target_ranges": windows,
                "review_playback": plan,
                "review_response_range": [0, plan["total_ms"]],
                "review_padding_ms": 0,
                "review_composition": "listed windows concatenated without source gaps or inserted silence",
                "independent_event_audio_construction": False,
                "first_bad_layer": "UNKNOWN",
                "confidence": "LOW",
                "root_cause": "UNKNOWN_MIXED_SOURCE" if mixed else "UNKNOWN",
                "model_query_affected": mixed,
                "review_only_affected": False,
                "affected_basis": "human mixed verdict on same source windows as model; origin unlocalized",
                "evidence": "No fully internal foreign exclusive turn in selected windows; regular output predicts overlap. "
                "Coarse task truth cannot distinguish simultaneous speech, missed turns, or label changes.",
                "foreign_exclusive_ms": sum(t["foreign_exclusive_ms"] for t in traces),
                "regular_overlap_prediction_ms": sum(
                    t["regular_overlap_prediction_ms"] for t in traces
                ),
                "fully_internal_foreign_turn_count": sum(
                    len(t["fully_internal_foreign_turns"]) for t in traces
                ),
                "secondary_findings": {
                    "fixed_window_cuts": sum(t["boundary"] is not None for t in traces),
                    "regular_overlap_predicted": any(
                        t["regular_overlap_prediction_ms"] for t in traces
                    ),
                },
            }
            if not ranges_replay or not projection_replay or vector_error > 1e-12:
                raise ValueError("Blind builder/projection/vector replay mismatch")
            self.cases.append(case)
        return tasks

    def historical(self):
        rows = self.rows(
            "SELECT t.*,g.purity,g.primary_speaker_person_id,g.quality_flags_json,g.reviewed_at "
            "FROM speaker_profile_purity_tasks t JOIN speaker_profile_purity_reviews g USING(task_id) "
            "WHERE g.action='submit' AND NOT EXISTS(SELECT 1 FROM speaker_profile_purity_reviews n "
            "WHERE n.task_id=g.task_id AND n.revision>g.revision) ORDER BY t.task_id"
        )
        selected = {r["task_id"]: r for r in rows if r["purity"] == "boundary_cross"}
        # Select implicated identity groups by existing review evidence, never names.
        priority_people = sorted(
            {
                r["target_person_id"]
                for r in rows
                if r["purity"] == "clean_single"
                and r["primary_speaker_person_id"] != r["target_person_id"]
            }
        )
        for person in priority_people[:2]:
            candidates = [
                r
                for r in rows
                if r["target_person_id"] == person and r["purity"] == "mixed_overlap"
            ]
            candidates.sort(
                key=lambda r: (
                    not bool(
                        json.loads(r["source_snapshot_json"]).get("prototype_ids")
                    ),
                    r["task_id"],
                )
            )
            sessions = set()
            for row in candidates:
                if row["source_session_id"] not in sessions:
                    selected[row["task_id"]] = row
                    sessions.add(row["source_session_id"])
                if len(sessions) == 3:
                    break
            wrong = [
                r
                for r in rows
                if r["target_person_id"] == person
                and r["purity"] == "clean_single"
                and r["primary_speaker_person_id"] != person
            ]
            wrong.sort(
                key=lambda r: (
                    not bool(
                        json.loads(r["source_snapshot_json"]).get("prototype_ids")
                    ),
                    r["task_id"],
                )
            )
            selected.update({r["task_id"]: r for r in wrong[:1]})
        controls = [
            r
            for r in rows
            if r["purity"] == "clean_single"
            and r["target_person_id"] == r["primary_speaker_person_id"]
        ]
        controls.sort(
            key=lambda r: (
                not bool(json.loads(r["source_snapshot_json"]).get("person_facts")),
                r["task_id"],
            )
        )
        selected.update({r["task_id"]: r for r in controls[:3]})
        for index, row in enumerate(
            sorted(selected.values(), key=lambda r: r["task_id"]), 1
        ):
            case_id = f"historical-{index:02}"
            snapshot = json.loads(row["source_snapshot_json"])
            provenance = []
            for fact in snapshot.get("person_facts", []):
                provenance.extend(
                    self.rows(
                        "SELECT f.fact_id,f.actor,f.state,f.source_utterance_id,"
                        "f.value_json,f.payload_json,u.run_id,u.start_ms,u.end_ms,u.original_speaker_track_id "
                        "FROM annotation_facts f LEFT JOIN utterances u ON u.utterance_id=f.source_utterance_id "
                        "WHERE f.fact_id=?",
                        (fact,),
                    )
                )
            run_ids = {p["run_id"] for p in provenance if p["run_id"]}
            track = self.tracks.get(row["source_track_id"])
            if not run_ids and track:
                run_ids.add(track["run_id"])
            captures = self.captures(row["source_session_id"], row["source_media_id"])
            mappings = union(
                (
                    seg["session_start_ms"] + row["start_ms"] - seg["source_start_ms"],
                    seg["session_start_ms"] + row["end_ms"] - seg["source_start_ms"],
                )
                for seg in captures
                if seg["source_start_ms"] <= row["start_ms"]
                and seg["source_end_ms"] >= row["end_ms"]
            )
            case_traces = []
            if len(mappings) == 1:
                start, end = mappings[0]
                window = {
                    "media_id": row["source_media_id"],
                    "start_ms": row["start_ms"],
                    "end_ms": row["end_ms"],
                    "session_start_ms": start,
                    "session_end_ms": end,
                }
                for run_id in sorted(run_ids):
                    case_traces.append(
                        self.window_trace(
                            case_id,
                            row["source_session_id"],
                            run_id,
                            window,
                            track["label"]
                            if track
                            and not track["label"].startswith(("manual:", "sample:"))
                            else None,
                        )
                    )
            wrong = (
                row["purity"] == "clean_single"
                and row["primary_speaker_person_id"] != row["target_person_id"]
            )
            prototypes = []
            for pid in snapshot.get("prototype_ids", []):
                prototypes.extend(
                    self.rows(
                        "SELECT prototype_id,source_prototype_id,status,person_id,"
                        "speaker_track_id,cluster_id,representative_clips_json FROM voice_prototypes "
                        "WHERE prototype_id=? ORDER BY prototype_id",
                        (pid,),
                    )
                )
            bridges = []
            for trace in case_traces:
                data = self.run(trace["run_id"])
                start, end = (
                    trace["window"]["session_start_ms"],
                    trace["window"]["session_end_ms"],
                )
                for u in trace["asr_utterances"]:
                    label = data["labels"].get(u["original_speaker_track_id"])
                    for turn in trace["diarization_exclusive"]:
                        if (
                            label
                            and turn["speaker_label"] != label
                            and max(start, u["start_ms"])
                            < turn["start_ms"]
                            < turn["end_ms"]
                            < min(end, u["end_ms"])
                        ):
                            bridges.append(
                                {
                                    "run_id": trace["run_id"],
                                    "utterance_id": u["utterance_id"],
                                    "utterance_label": label,
                                    "foreign_turn": turn,
                                }
                            )
            source_samples = self.rows(
                "SELECT sample_key,person_id,session_id,prototype_id,windows_json,current "
                "FROM annotation_sample_sets WHERE session_id=? AND person_id=? ORDER BY sample_key",
                (row["source_session_id"], row["target_person_id"]),
            )
            self.cases.append(
                {
                    "case_id": case_id,
                    "cohort": "historical_purity_support",
                    "review_task": row,
                    "snapshot": snapshot,
                    "annotation_provenance": provenance,
                    "source_run_ids": sorted(run_ids),
                    "media_to_session_mappings": mappings,
                    "prototype_sources": prototypes,
                    "annotation_sample_sets": source_samples,
                    "structural_cross_speaker_hulls": bridges,
                    "first_bad_structural_layer": "ASR_SEGMENTATION"
                    if bridges
                    else "UNKNOWN",
                    "structural_confidence": "HIGH" if bridges else "LOW",
                    "speaker_track": track,
                    "cluster_memberships": self.rows(
                        "SELECT * FROM speaker_cluster_memberships "
                        "WHERE speaker_track_id=? ORDER BY membership_id",
                        (row["source_track_id"],),
                    ),
                    "first_bad_layer": "UNKNOWN",
                    "confidence": "LOW",
                    "root_cause": "WRONG_PRIMARY_SOURCE_AUTHORIZATION"
                    if wrong
                    else "UNKNOWN",
                    "model_query_affected": bool(
                        snapshot.get("prototype_ids")
                        and (wrong or row["purity"] != "clean_single")
                    ),
                    "review_only_affected": None,
                    "evidence": "Reviewed source/prototype provenance retained. Fine-grained turn truth unavailable; "
                    "purity review permits optional ±4s context, and actual context choice is not logged. "
                    "Wrong-primary clean source, when present, predates sample aggregation.",
                    "review_target": [row["start_ms"], row["end_ms"]],
                    "possible_context": [
                        max(0, row["start_ms"] - 4000),
                        min(row["end_ms"] + 4000, captures[0]["duration_ms"])
                        if captures
                        else None,
                    ],
                    "actual_review_playback": "UNKNOWN; target/context action not persisted",
                }
            )
        return {
            "latest_review_count": len(rows),
            "latest_purity_counts": dict(Counter(r["purity"] for r in rows)),
            "selected_support_count": len(selected),
            "not_in_blind_denominator": True,
        }

    def backchannels(self):
        run_ids = sorted(
            {
                c["speaker_track"]["run_id"]
                for c in self.cases
                if c["cohort"] == "latest_blind"
            }
        )
        patterns = []
        for run_id in run_ids:
            data = self.run(run_id)
            candidates = aba_candidates(
                data["diarization"].get("exclusive_turns", []),
                data["utterances"],
                data["labels"],
                data["asr"].get("primary_tokens", []),
                data["threshold"],
            )
            for candidate in candidates:
                b = candidate["b"]
                candidate["run_id"] = run_id
                candidate["selected_blind_window_hits"] = [
                    {"case_id": c["case_id"], "window_index": i}
                    for c in self.cases
                    if c["cohort"] == "latest_blind"
                    for i, w in enumerate(c["query_source_windows"], 1)
                    if c["speaker_track"]["run_id"] == run_id
                    and w["session_start_ms"] <= b["start_ms"]
                    and w["session_end_ms"] >= b["end_ms"]
                ]
                patterns.append(candidate)
        samples = [p for p in patterns if p["outcome"] == "downstream_remerged"][:5] + [
            p for p in patterns if p["three_projected_turns_preserved"]
        ][:2]
        for index, pattern in enumerate(samples, 1):
            case_id = f"aba-{index:02}"
            track = next(
                t
                for t in self.tracks.values()
                if t["run_id"] == pattern["run_id"]
                and t["label"] == pattern["a"]["speaker_label"]
            )
            start = max(pattern["a"]["start_ms"], pattern["b"]["start_ms"] - 5000)
            end = min(pattern["a_resumes"]["end_ms"], pattern["b"]["end_ms"] + 5000)
            for capture in self.captures(track["session_id"]):
                lo, hi = (
                    max(start, capture["session_start_ms"]),
                    min(end, capture["session_end_ms"]),
                )
                if hi <= lo:
                    continue
                source = capture["source_start_ms"] + lo - capture["session_start_ms"]
                self.window_trace(
                    case_id,
                    track["session_id"],
                    pattern["run_id"],
                    {
                        "media_id": capture["media_id"],
                        "start_ms": source,
                        "end_ms": source + hi - lo,
                        "session_start_ms": lo,
                        "session_end_ms": hi,
                        "diagnostic_reference_only": True,
                    },
                    track["label"],
                )
            self.cases.append(
                {
                    "case_id": case_id,
                    "cohort": "automatic_pattern_support",
                    "pattern": pattern,
                    "speaker_track": track,
                    "cluster_memberships": self.rows(
                        "SELECT * FROM speaker_cluster_memberships WHERE speaker_track_id=? ORDER BY membership_id",
                        (track["speaker_track_id"],),
                    ),
                    "first_bad_layer": "ASR_SEGMENTATION"
                    if pattern["outcome"] == "downstream_remerged"
                    else "UNKNOWN",
                    "confidence": "HIGH"
                    if pattern["outcome"] == "downstream_remerged"
                    else "LOW",
                    "confidence_scope": "saved-layer discrepancy; acoustic speaker truth unverified",
                    "root_cause": "BACKCHANNEL_REMERGED_DOWNSTREAM"
                    if pattern["outcome"] == "downstream_remerged"
                    else "PREDICTED_B_LABEL_PRESERVED",
                    "model_query_affected": False,
                    "review_only_affected": False,
                    "evidence": "Diarization predicts B; ASR token projection has no B utterance and A hull crosses B. "
                    "These sampled bridges are outside the selected Blind query windows.",
                    "downstream_effect_scope": "selected latest Blind queries only",
                }
            )
        return {
            "scope": "unique run(s) used by latest Blind, adjacent exclusive predicted turns only",
            "candidate_definition": "A duration>=2s, B<=2s, neighboring gaps 0..1200ms, A resumes with same label",
            "counts": dict(Counter(p["outcome"] for p in patterns)),
            "aba_total": len(patterns),
            "three_projected_turns_preserved_count": sum(
                p["three_projected_turns_preserved"] for p in patterns
            ),
            "diarization_missed": None,
            "diarization_missed_reason": "cannot discover missing B using diarization alone",
            "duration_distribution": dict(
                Counter(
                    "0..300"
                    if p["b_duration_ms"] <= 300
                    else "301..500"
                    if p["b_duration_ms"] <= 500
                    else "501..1000"
                    if p["b_duration_ms"] <= 1000
                    else "1001..2000"
                    for p in patterns
                )
            ),
            "laughter_candidates": sum(p["laughter"] != "UNKNOWN" for p in patterns),
            "remerged_token_loss": dict(
                Counter(
                    p["token_loss"]
                    for p in patterns
                    if p["outcome"] == "downstream_remerged"
                )
            ),
            "selected_blind_remerged_hits": sum(
                len(p["selected_blind_window_hits"])
                for p in patterns
                if p["outcome"] == "downstream_remerged"
            ),
            "patterns": patterns,
            "threshold_probes": threshold_probes(),
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "state/v3/core.sqlite3")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "outputs/speaker-turn-boundary-audit-20261001",
    )
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to((ROOT / "outputs").resolve()):
        raise ValueError("identifying audit output must stay under private outputs/")
    db = args.db.resolve()
    if not db.is_file():
        raise FileNotFoundError(db)
    db_files = [p for p in (db, Path(str(db) + "-wal")) if p.exists()]
    before = {str(p): sha(p) for p in db_files}
    source_before = {str(p): sha(p) for p in sorted((ROOT / "src").rglob("*.py"))}
    with open_readonly(db) as connection:
        audit = Audit(connection, db)
        tasks = audit.blind()
        historical = audit.historical()
        backchannels = audit.backchannels()
        blind = [c for c in audit.cases if c["cohort"] == "latest_blind"]
        count = len(blind)
        roots = Counter(c["root_cause"] for c in blind)
        root_table = [
            {
                "root_cause": name,
                "cases": roots.get(name, 0),
                "percent": 100 * roots.get(name, 0) / count if count else None,
            }
            for name in (
                "DIARIZATION_MISASSIGNMENT",
                "BACKCHANNEL_REMERGED_DOWNSTREAM",
                "FIXED_WINDOW_BOUNDARY",
                "REVIEW_PLAYBACK_CONTEXT_CONTAMINATION",
                "SIMULTANEOUS_OVERLAP",
                "UNKNOWN_MIXED_SOURCE",
            )
        ]
        cuts = Counter(cause for b in audit.boundaries for cause in b["causes"])
        summary = {
            "blind_task_count": count,
            "blind_source_window_count": sum(
                len(c["query_source_windows"]) for c in blind
            ),
            "blind_latest_verdicts": dict(
                Counter(
                    c["truth"]["purity"] if c["truth"] else "unreviewed" for c in blind
                )
            ),
            "primary_root_cause_table": root_table,
            "root_cause_denominator": "latest active Blind tasks only",
            "root_cause_zero_meaning": "zero confirmed attribution; NOT exclusion of that mechanism",
            "verified_true_overlap_count": None,
            "verified_aba_count": None,
            "verified_semantic_boundary_cut_count": None,
            "q4_percentages": None,
            "q4_reason": "mixed_overlap schema supplies no per-window subtype or turn-level human labels",
            "regular_overlap_predicted_task_count": sum(
                c["secondary_findings"]["regular_overlap_predicted"] for c in blind
            ),
            "exclusive_foreign_task_count": sum(
                c["foreign_exclusive_ms"] > 0 for c in blind
            ),
            "exclusive_foreign_total_ms": sum(c["foreign_exclusive_ms"] for c in blind),
            "fully_internal_foreign_turn_count": sum(
                c["fully_internal_foreign_turn_count"] for c in blind
            ),
            "boundary_window_count": len(audit.boundaries),
            "boundary_causes": dict(cuts),
            "boundary_task_count": len({b["case_id"] for b in audit.boundaries}),
            "boundary_same_speaker_and_vad_continuation_count": sum(
                b["same_speaker_exclusive_turn_straddles_end"]
                and b["vad_speech_straddles_end"]
                for b in audit.boundaries
            ),
            "boundary_inside_aligned_token_count": sum(
                b["token_straddles_end"] for b in audit.boundaries
            ),
            "boundary_vad_confirmed_count": None,
            "boundary_asr_confirmed_count": None,
            "boundary_review_additional_count": 0,
            "model_query_affected_count": sum(c["model_query_affected"] for c in blind),
            "review_only_affected_count": 0,
            "model_effect_basis": "same windows, human coarse mixed verdict; no accuracy-impact estimate",
            "historical_support": historical,
            "historical_structural_bridge_source_count": sum(
                bool(c["structural_cross_speaker_hulls"])
                for c in audit.cases
                if c["cohort"] == "historical_purity_support"
            ),
            "audit_case_count": len(audit.cases),
            "diarization_replacement": "NOT YET JUSTIFIED",
            "change_scope": "局部重构",
            "no_new_review_tasks": True,
            "questions": {
                "Q1": "UNKNOWN for latest mixed origin; no selected-query internal detected B turn, no envelope/padding expansion",
                "Q2": "18 downstream bridges among 105 predicted ABA candidates; diarization-missed unmeasurable; bridges absent from selected windows",
                "Q3": "query construction first clips 13 still-continuing utterances: 8 at 8s cap, 5 at capture edge; semantic/raw continuity unverified",
                "Q4": "true simultaneous, audible ABA and semantic-cut proportions unidentified; all 3 tasks have regular overlap predictions",
                "Q5": "3 mixed-verdict queries use identical model/review source windows; zero extra Blind padding; recognition harm not estimated",
                "Q6": "shared ASR-projection hull gap-fill demonstrated in enrollment support, but latest Blind mixed origin unproven; wrong-primary clean source is separate authorization chain",
                "Q7": "NOT YET JUSTIFIED",
                "Q8": "局部重构 of turn-preserving projection and source-window selection; no production changes this round",
            },
            "priorities": [
                "P0: preserve diarization turn boundaries through ASR attribution/grouping and clip selection",
                "P1: make 8s/capture clipping boundary aware and retain per-window review provenance",
                "P2: collect subtype/turn-level truth before diarization replacement experiment",
            ],
        }
        database_readonly = connection.execute("PRAGMA query_only").fetchone()[0] == 1
        connection.rollback()
    after = {str(p): sha(p) for p in db_files}
    source_after = {str(p): sha(p) for p in sorted((ROOT / "src").rglob("*.py"))}
    if before != after or source_before != source_after:
        raise RuntimeError(
            "database/source changed concurrently; retry audit against stable inputs"
        )
    verification = {
        "sqlite_uri_mode": "ro",
        "query_only": database_readonly,
        "database_before_sha256": before,
        "database_after_sha256": after,
        "database_unchanged": before == after,
        "production_source_unchanged": source_before == source_after,
        "source_hashes": source_before,
        "input_hashes": audit.input_hashes,
        "builder_replay_all_passed": all(c["builder_replay_matches"] for c in blind),
        "projection_replay_all_passed": all(
            c["projection_replay_matches"] for c in blind
        ),
        "model_mean_replay_all_passed": all(
            c["model_mean_max_abs_error"] <= 1e-12 for c in blind
        ),
        "audio_checksums_match": all(
            r["checksum_matches"] for rows in audit.audio.values() for r in rows
        ),
        "deterministic": "sorted snapshot reads; no current clock/randomness; rerun hashes checked separately",
        "limitations": [
            "no independent acoustic listening or fine-grained raw turn truth",
            "predicted regular overlap is not verified simultaneous speech",
            "historical context playback choice not stored",
            "run config_digest does not store original projection threshold; current 0.5 replay verified for Blind",
        ],
        "blind_task_ids": [t["task_id"] for t in tasks],
    }
    output.mkdir(parents=True, exist_ok=True)
    payloads = {
        "cases.json": audit.cases,
        "layer-traces.json": audit.traces,
        "backchannel-patterns.json": backchannels,
        "boundary-cases.json": audit.boundaries,
        "root-cause-summary.json": summary,
        "verification.json": verification,
    }
    for name, payload in payloads.items():
        (output / name).write_text(encode(payload), encoding="utf-8")
    lines = [
        "# Speaker Turn / Blind Query Boundary Audit — 私有报告",
        "",
        "严格只读。真实姓名、ASR、session/media 与时间线只保存在本目录。",
        "",
        "## 范围与主结论",
        "",
        f"最新 Blind {count} 个任务，全部追踪；支持案例不混入主分母。",
        "混合说话的首次声学错误层 UNKNOWN；固定范围裁切和 token 投影跨 B 是独立可证结构发现。",
        "真 overlap / diarization missed / 语义截断比例不可从 coarse verdict 推出。",
        "",
        "## 汇总",
        "",
        "```json",
        encode(summary).strip(),
        "```",
        "",
        "## A-B-A 候选",
        "",
        "```json",
        encode(
            {
                k: v
                for k, v in backchannels.items()
                if k not in {"patterns", "threshold_probes"}
            }
        ).strip(),
        "```",
        "",
        "## 逐例结论",
        "",
    ]
    for case in audit.cases:
        lines += [
            f"### {case['case_id']} ({case['cohort']})",
            "",
            f"first_bad_layer={case['first_bad_layer']}; confidence={case['confidence']}; "
            f"root_cause={case['root_cause']}",
            "",
            case["evidence"],
            "",
        ]
    lines += [
        "## Q1–Q8",
        "",
        "Q1: 最新三个 mixed 的首错层仍未知；非 query/event/padding 扩大时间包络。",
        "Q2: 已预测 B 的候选有下游重并；diarization missed 不可测，不能断言两者比例。",
        "Q3: 源 utterance 尚未结束时 query 的 8s/file-edge min 首次裁短；语义连续与 VAD 真值未确认。",
        "Q4: 真实 simultaneous/ABA/boundary cut 的比例不可识别；regular 模型 overlap 仅候选。",
        "Q5: 最新 mixed 三任务与模型使用相同窗口，因此源混合进入模型；手机 padding-only 无证据。",
        "Q6: 两条链共享 ASR projection 和 bounded embedding；enrollment 事实 union 对正 gap 不填充。",
        "wrong-primary 的单人错误授权另有独立 provenance，不能归因 ABA。",
        "Q7: NOT YET JUSTIFIED。Q8: 局部重构优先，现阶段只列建议。",
        "",
        "详细原音路径、token、VAD、regular/exclusive、人物授权链见 layer-traces.json 与 cases.json。",
    ]
    (output / "private-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        encode(
            {
                "output": str(output),
                "summary": summary,
                "backchannel_counts": backchannels["counts"],
            }
        )
    )


if __name__ == "__main__":
    main()
