"""Read-only audit of human person evidence in the live V3 SQLite database.

Run: python tools/audit_speaker_data_coverage.py
All identified results are written only to the ignored outputs/ directory.
"""

from __future__ import annotations

import collections
import csv
import hashlib
import json
import platform
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "state/v3/core.sqlite3"
OUT = ROOT / "outputs/speaker-data-coverage-audit-20260928"
LOCAL = ZoneInfo("Asia/Singapore")
GAPS = (30, 120, 300, 900)
STRICT_GAP = 900
RELAXED_GAP = 120
MIN_USABLE_SECONDS = 6
MANUAL_SQL = "(f.actor='legacy-human' OR f.actor LIKE 'phone-operation:%' OR f.actor LIKE 'desktop-operation:%')"


def write_json(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def dt(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def union_ms(intervals):
    total = 0
    end = -1
    for start, stop in sorted(intervals):
        if stop <= start:
            continue
        total += max(0, stop - max(start, end))
        end = max(end, stop)
    return total


def gini(counts):
    values = sorted(counts)
    total = sum(values)
    return round(sum((2 * i - len(values) - 1) * x for i, x in enumerate(values, 1)) / (len(values) * total), 4) if total else 0


def share(counts, n):
    values = sorted(counts, reverse=True)
    return round(sum(values[:n]) / sum(values), 4) if sum(values) else 0


def open_db():
    connection = sqlite3.connect(DB.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("BEGIN")
    return connection


def schema_check(connection):
    required = {
        "annotation_facts": {"fact_id", "actor", "state", "value_json", "source_utterance_id", "created_at"},
        "annotation_fact_audio": {"fact_id", "media_id", "start_ms", "end_ms"},
        "utterances": {"utterance_id", "session_id", "start_ms", "end_ms", "speaker_track_id"},
        "recording_sessions": {"session_id", "captured_start", "timezone"},
        "speaker_tracks": {"speaker_track_id", "session_id"},
        "speaker_cluster_memberships": {"speaker_track_id", "cluster_id", "state"},
        "voice_prototypes": {"prototype_id", "speaker_track_id", "person_id", "representative_clips_json"},
        "annotation_sample_sets": {"session_id", "person_id", "prototype_id", "current"},
        "persons": {"person_id", "display_name"},
        "audio_assets": {"media_id", "sha256", "duration_ms"},
        "capture_segments": {"session_id", "asset_id", "source_start_ms", "source_end_ms"},
    }
    schema = {name: [row[1] for row in connection.execute(f"PRAGMA table_info({name})")] for name in required}
    missing = {name: sorted(columns - set(schema[name])) for name, columns in required.items() if columns - set(schema[name])}
    if missing:
        raise RuntimeError(f"Live database schema differs from audit contract: {missing}")
    return schema


def load(connection):
    persons = {r["person_id"]: dict(r) for r in connection.execute("SELECT * FROM persons")}
    sessions = {r["session_id"]: dict(r) for r in connection.execute("SELECT * FROM recording_sessions")}
    assets = {r["media_id"]: dict(r) for r in connection.execute("SELECT * FROM audio_assets WHERE media_id IS NOT NULL")}
    replica_media = {r[0] for r in connection.execute("""SELECT DISTINCT a.media_id FROM audio_assets a
        JOIN audio_replicas r USING(asset_id) WHERE r.state='available'""")}
    rows = connection.execute(f"""SELECT f.fact_id,f.value_json,f.actor,f.created_at,
        f.source_utterance_id,u.session_id,u.start_ms AS utterance_start_ms,
        u.end_ms AS utterance_end_ms,u.speaker_track_id,u.original_speaker_track_id,
        u.status AS utterance_status,
        a.media_id,a.start_ms AS audio_start_ms,a.end_ms AS audio_end_ms
        FROM annotation_facts f LEFT JOIN utterances u ON u.utterance_id=f.source_utterance_id
        LEFT JOIN annotation_fact_audio a ON a.fact_id=f.fact_id
        WHERE f.dimension='person' AND f.state='active' AND {MANUAL_SQL}
        ORDER BY f.fact_id,a.ordinal""")
    facts = {}
    for row in rows:
        r = dict(row)
        fid = r["fact_id"]
        if fid not in facts:
            try:
                pid = json.loads(r["value_json"])
            except (TypeError, json.JSONDecodeError):
                pid = None
            facts[fid] = {"fact_id": fid, "person_id": pid, "actor": r["actor"],
                          "created_at": r["created_at"], "utterance_id": r["source_utterance_id"],
                          "session_id": r["session_id"], "start_ms": r["utterance_start_ms"],
                          "end_ms": r["utterance_end_ms"], "track_id": r["speaker_track_id"],
                          "original_track_id": r["original_speaker_track_id"],
                          "utterance_status": r["utterance_status"], "audio": []}
        if r["media_id"] is not None:
            facts[fid]["audio"].append({"media_id": r["media_id"], "start_ms": r["audio_start_ms"], "end_ms": r["audio_end_ms"]})
    return persons, sessions, assets, replica_media, list(facts.values())


def event_partition(facts, assets, replica_media, sessions, gap):
    """Chain same-person/session utterances; collapse shared original-audio overlap."""
    grouped = collections.defaultdict(list)
    for fact in facts:
        if fact["session_id"] and fact["start_ms"] is not None and fact["end_ms"] is not None:
            grouped[(fact["person_id"], fact["session_id"])].append(fact)
    events = []
    for (pid, sid), rows in grouped.items():
        rows.sort(key=lambda f: (f["start_ms"], f["end_ms"], f["fact_id"]))
        current = None
        for fact in rows:
            if current is None or fact["start_ms"] > current["end_ms"] + gap * 1000:
                current = {"person_id": pid, "session_ids": [sid], "start_ms": fact["start_ms"],
                           "end_ms": fact["end_ms"], "facts": [], "audio": [], "track_ids": set()}
                events.append(current)
            current["end_ms"] = max(current["end_ms"], fact["end_ms"])
            current["facts"].append(fact["fact_id"])
            current["audio"].extend(fact["audio"])
            if fact["original_track_id"]:
                current["track_ids"].add(fact["original_track_id"])
    # A source asset reused in multiple sessions is one source event when windows overlap.
    parent = list(range(len(events)))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    by_source = collections.defaultdict(list)
    for i, event in enumerate(events):
        for clip in event["audio"]:
            asset = assets.get(clip["media_id"], {})
            key = asset.get("sha256") or clip["media_id"]
            by_source[(event["person_id"], key)].append((clip["start_ms"], clip["end_ms"], i))
    for spans in by_source.values():
        spans.sort()
        active = []
        for start, end, i in spans:
            active = [(b, j) for b, j in active if b > start]
            for _, j in active:
                parent[root(i)] = root(j)
            active.append((end, i))
    merged = {}
    for i, event in enumerate(events):
        key = root(i)
        if key not in merged:
            merged[key] = {"person_id": event["person_id"], "session_ids": set(), "facts": set(),
                           "audio": [], "track_ids": set(), "session_ranges": {}}
        target = merged[key]
        target["session_ids"].update(event["session_ids"])
        target["facts"].update(event["facts"])
        target["audio"].extend(event["audio"])
        target["track_ids"].update(event["track_ids"])
        sid = event["session_ids"][0]
        previous = target["session_ranges"].get(sid, [event["start_ms"], event["end_ms"]])
        target["session_ranges"][sid] = [min(previous[0], event["start_ms"]), max(previous[1], event["end_ms"])]
    result = []
    for event in merged.values():
        audio = collections.defaultdict(list)
        available_audio = collections.defaultdict(list)
        for clip in event["audio"]:
            source = assets.get(clip["media_id"], {}).get("sha256") or clip["media_id"]
            audio[source].append((clip["start_ms"], clip["end_ms"]))
            if clip["media_id"] in replica_media:
                available_audio[source].append((clip["start_ms"], clip["end_ms"]))
        seconds = sum(union_ms(spans) for spans in audio.values()) / 1000
        event["audio_seconds"] = round(seconds, 3)
        event["available_replica_audio_seconds"] = round(sum(union_ms(spans) for spans in available_audio.values()) / 1000, 3)
        event["source_count"] = len(audio)
        times = [(dt(sessions[sid]["captured_start"]) + timedelta(milliseconds=start),
                  dt(sessions[sid]["captured_start"]) + timedelta(milliseconds=end))
                 for sid, (start, end) in event["session_ranges"].items()]
        event["first_seen"] = min(start for start, _ in times).isoformat()
        event["last_seen"] = max(end for _, end in times).isoformat()
        event["dates"] = sorted({start.astimezone(LOCAL).date().isoformat() for start, _ in times}
                                | {end.astimezone(LOCAL).date().isoformat() for _, end in times})
        event["session_ids"] = sorted(event["session_ids"])
        event["facts"] = sorted(event["facts"])
        event["track_ids"] = sorted(event["track_ids"])
        event["id"] = hashlib.sha256("|".join(event["facts"]).encode()).hexdigest()[:16]
        del event["audio"]
        result.append(event)
    return sorted(result, key=lambda e: (e["person_id"], e["session_ids"], e["id"]))


def gap_distribution(facts):
    grouped = collections.defaultdict(list)
    for fact in facts:
        grouped[(fact["person_id"], fact["session_id"])].append((fact["start_ms"], fact["end_ms"]))
    result = collections.defaultdict(list)
    for (pid, _), spans in grouped.items():
        end = None
        for start, stop in sorted(spans):
            if end is not None:
                result[pid].append(max(0, start - end) / 1000)
            end = max(end or stop, stop)
    def describe(values):
        if not values:
            return {"count": 0}
        ordered = sorted(values)
        return {"count": len(values), "median_s": ordered[len(ordered) // 2],
                "p90_s": ordered[int(0.9 * (len(ordered) - 1))],
                "p99_s": ordered[int(0.99 * (len(ordered) - 1))],
                "over_30s": sum(v > 30 for v in values), "over_120s": sum(v > 120 for v in values),
                "over_300s": sum(v > 300 for v in values), "over_900s": sum(v > 900 for v in values)}
    return {pid: describe(values) for pid, values in result.items()}


def cross_person_overlap_pairs(facts, assets):
    by_source = collections.defaultdict(list)
    for fact in facts:
        for clip in fact["audio"]:
            source = assets.get(clip["media_id"], {}).get("sha256") or clip["media_id"]
            by_source[source].append((clip["start_ms"], clip["end_ms"], fact["person_id"], fact["fact_id"]))
    pairs = set()
    for spans in by_source.values():
        active = []
        for start, end, pid, fid in sorted(spans):
            active = [item for item in active if item[0] > start]
            for _, old_pid, old_fid in active:
                if old_pid != pid:
                    pairs.add(tuple(sorted((fid, old_fid))))
            active.append((end, pid, fid))
    return pairs


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "plots").mkdir(exist_ok=True)
    connection = open_db()
    schema = schema_check(connection)
    persons, sessions, assets, replica_media, all_facts = load(connection)
    revision = connection.execute("SELECT revision FROM annotation_input_revision WHERE singleton=1").fetchone()[0]
    prior_dir = ROOT / "outputs/speaker-identity-blind-validation-20260928"
    prior_file = prior_dir / "verification.json"
    frozen_file = prior_dir / "frozen-candidates.json"
    old_blind = json.loads(prior_file.read_text(encoding="utf-8")) if prior_file.exists() else {}
    frozen = json.loads(frozen_file.read_text(encoding="utf-8")) if frozen_file.exists() else {}
    cutoff = dt(frozen["freeze_utc"]) if frozen else None
    post_freeze_sessions = [sid for sid, s in sessions.items()
                            if cutoff and dt(s["captured_start"]) > cutoff and dt(s["created_at"]) > cutoff]
    blind_zero_verified = (old_blind.get("database_revision") == revision
                           and old_blind.get("blind_scored_independent_events") == 0
                           and not post_freeze_sessions)
    facts = [f for f in all_facts if f["person_id"] in persons and f["session_id"] in sessions and f["audio"]]
    excluded = len(all_facts) - len(facts)
    conflicting_pairs = cross_person_overlap_pairs(facts, assets)
    for fact in facts:
        session = sessions[fact["session_id"]]
        fact["capture_time"] = (dt(session["captured_start"]) + timedelta(milliseconds=fact["start_ms"])).isoformat()
        fact["date"] = dt(fact["capture_time"]).astimezone(LOCAL).date().isoformat()
    by_person = collections.defaultdict(list)
    for f in facts:
        by_person[f["person_id"]].append(f)
    partitions = {str(gap): event_partition(facts, assets, replica_media, sessions, gap) for gap in GAPS}
    gap_stats = gap_distribution(facts)
    strict = partitions[str(STRICT_GAP)]
    relaxed = partitions[str(RELAXED_GAP)]
    strict_by_person = collections.defaultdict(list)
    relaxed_by_person = collections.defaultdict(list)
    for e in strict:
        strict_by_person[e["person_id"]].append(e)
    for e in relaxed:
        relaxed_by_person[e["person_id"]].append(e)
    clusters_by_track = collections.defaultdict(set)
    for row in connection.execute("SELECT speaker_track_id,cluster_id FROM speaker_cluster_memberships WHERE state='active'"):
        clusters_by_track[row["speaker_track_id"]].add(row["cluster_id"])
    for event in strict + relaxed:
        event["cluster_ids"] = sorted(set().union(*(clusters_by_track[t] for t in event["track_ids"])))
    sample_sets = [dict(r) for r in connection.execute("SELECT session_id,person_id,prototype_id,current FROM annotation_sample_sets")]
    prototypes = [dict(r) for r in connection.execute("SELECT prototype_id,speaker_track_id,person_id,status,source_prototype_id FROM voice_prototypes")]
    samples_by_person = collections.Counter(r["person_id"] for r in sample_sets if r["current"])
    prototypes_by_track = collections.defaultdict(set)
    direct_prototypes = collections.defaultdict(set)
    for p in prototypes:
        if p["speaker_track_id"]:
            prototypes_by_track[p["speaker_track_id"]].add(p["prototype_id"])
        if p["person_id"]:
            direct_prototypes[p["person_id"]].add(p["prototype_id"])
    summary = []
    concentration = {}
    coverage = {}
    curves = {}
    growth = {}
    for pid, rows in sorted(by_person.items(), key=lambda x: -len(x[1])):
        name = persons[pid]["display_name"]
        sids = {f["session_id"] for f in rows}
        dates = {f["date"] for f in rows}
        tracks = {f["original_track_id"] for f in rows if f["original_track_id"]}
        annotation_tracks = {f["track_id"] for f in rows if f["track_id"]}
        media_spans = collections.defaultdict(list)
        for f in rows:
            for a in f["audio"]:
                source = assets.get(a["media_id"], {}).get("sha256") or a["media_id"]
                media_spans[source].append((a["start_ms"], a["end_ms"]))
        seconds = round(sum(union_ms(v) for v in media_spans.values()) / 1000, 3)
        ses_counts = collections.Counter(f["session_id"] for f in rows)
        date_counts = collections.Counter(f["date"] for f in rows)
        usable = sum(e["available_replica_audio_seconds"] >= MIN_USABLE_SECONDS for e in strict_by_person[pid])
        first, last = min(dt(f["capture_time"]) for f in rows), max(dt(f["capture_time"]) for f in rows)
        item = {"person_id": pid, "person": name, "annotation_count": len(rows),
                "annotated_utterance_count": len({f["utterance_id"] for f in rows}),
                "annotated_audio_seconds": seconds, "session_count": len(sids), "date_count": len(dates),
                "speaker_track_count": len(tracks), "annotation_track_count": len(annotation_tracks),
                "prototype_count": len(direct_prototypes[pid] | set().union(*(prototypes_by_track[t] for t in tracks | annotation_tracks))),
                "current_annotation_sample_sets": samples_by_person[pid],
                "independent_event_count": len(strict_by_person[pid]),
                "usable_independent_event_count": usable,
                "relaxed_event_count": len(relaxed_by_person[pid]),
                "blind_event_count": 0 if blind_zero_verified else None,
                "first_seen": first.isoformat(), "last_seen": last.isoformat(),
                "time_span_days": round((last - first).total_seconds() / 86400, 2),
                "annotations_per_event": round(len(rows) / len(strict_by_person[pid]), 2),
                "seconds_per_event": round(seconds / len(strict_by_person[pid]), 2),
                "events_per_date": round(len(strict_by_person[pid]) / len(dates), 2),
                "sessions_per_date": round(len(sids) / len(dates), 2)}
        summary.append(item)
        concentration[pid] = {"person": name, "top1_session": share(ses_counts.values(), 1),
                              "top3_sessions": share(ses_counts.values(), 3), "top5_sessions": share(ses_counts.values(), 5),
                              "top1_date": share(date_counts.values(), 1), "top3_dates": share(date_counts.values(), 3),
                              "session_gini": gini(ses_counts.values()),
                              "session_hhi": round(sum((n / len(rows)) ** 2 for n in ses_counts.values()), 4),
                              "sessions": dict(ses_counts), "dates": dict(date_counts)}
        event_for_fact = {fid: e["id"] for e in strict_by_person[pid] for fid in e["facts"]}
        ordered = sorted(rows, key=lambda f: (f["capture_time"], f["fact_id"]))
        seen_events, seen_sessions, seen_dates = set(), set(), set()
        points = []
        accumulated_seconds = 0.0
        for index, f in enumerate(ordered, 1):
            seen_events.add(event_for_fact[f["fact_id"]])
            seen_sessions.add(f["session_id"])
            seen_dates.add(f["date"])
            accumulated_seconds += sum((a["end_ms"] - a["start_ms"]) / 1000 for a in f["audio"])
            if index == 1 or index % 25 == 0 or index == len(rows) or f["date"] != ordered[index]["date"]:
                points.append({"annotation_count": index, "capture_date": f["date"],
                               "independent_event_count": len(seen_events), "session_count": len(seen_sessions),
                               "date_count": len(seen_dates), "gross_audio_seconds": round(accumulated_seconds, 1)})
        curves[pid] = points
        new_rows = sorted(rows, key=lambda f: (f["created_at"], f["fact_id"]))
        tail = new_rows[-min(500, len(new_rows)):]
        prior = new_rows[:-len(tail)]
        capture_tail = ordered[-min(500, len(ordered)):]
        capture_prior = ordered[:-len(capture_tail)]
        qualified_ids = {e["id"] for e in strict_by_person[pid] if e["available_replica_audio_seconds"] >= MIN_USABLE_SECONDS}
        growth[pid] = {"person": name, "tail_count": len(tail), "order": "fact.created_at, fact_id",
                       "new_events": len({event_for_fact[f["fact_id"]] for f in tail} - {event_for_fact[f["fact_id"]] for f in prior}),
                       "new_duration_qualified_events": len({event_for_fact[f["fact_id"]] for f in tail} - {event_for_fact[f["fact_id"]] for f in prior}
                                                           & qualified_ids),
                       "new_sessions": len({f["session_id"] for f in tail} - {f["session_id"] for f in prior}),
                       "new_dates": len({f["date"] for f in tail} - {f["date"] for f in prior}),
                       "capture_order_tail": {
                           "new_events": len({event_for_fact[f["fact_id"]] for f in capture_tail} - {event_for_fact[f["fact_id"]] for f in capture_prior}),
                           "new_duration_qualified_events": len(({event_for_fact[f["fact_id"]] for f in capture_tail} - {event_for_fact[f["fact_id"]] for f in capture_prior}) & qualified_ids),
                           "new_sessions": len({f["session_id"] for f in capture_tail} - {f["session_id"] for f in capture_prior}),
                           "new_dates": len({f["date"] for f in capture_tail} - {f["date"] for f in capture_prior})},
                       "legacy_import_caution": any(f["actor"] == "legacy-human" for f in tail)}
        event_lengths = [e["audio_seconds"] for e in strict_by_person[pid]]
        bins = {"<1s": 0, "1-3s": 0, "3-5s": 0, "5-10s": 0, "10-20s": 0, ">20s": 0}
        for length in event_lengths:
            key = "<1s" if length < 1 else "1-3s" if length < 3 else "3-5s" if length < 5 else "5-10s" if length < 10 else "10-20s" if length <= 20 else ">20s"
            bins[key] += 1
        hours = collections.Counter(dt(f["capture_time"]).astimezone(LOCAL).hour for f in rows)
        coverage[pid] = {"person": name, "date_count": len(dates), "sessions_per_date": item["sessions_per_date"],
                         "events_per_date": item["events_per_date"], "event_duration_bins": bins,
                         "annotation_hours_local": dict(sorted(hours.items())),
                         "device": "not currently observable", "environment": "not currently observable",
                         "distance": "not currently observable", "SNR": "not currently observable",
                         "overlap": "not currently observable", "blind_coverage": "NONE",
                         "hard_negative_support": "NOT_QUANTIFIED_IN_CURRENT_LEDGER",
                         "speech_volume": "HIGH" if seconds >= 300 else "MEDIUM" if seconds >= 60 else "LOW",
                         "event_diversity": "HIGH" if len(strict_by_person[pid]) >= 20 else "MEDIUM" if len(strict_by_person[pid]) >= 5 else "LOW",
                         "session_diversity": "HIGH" if len(sids) >= 8 else "MEDIUM" if len(sids) >= 3 else "LOW",
                         "date_diversity": "HIGH" if len(dates) >= 8 else "MEDIUM" if len(dates) >= 3 else "LOW"}
    write_json("persons-summary.json", summary)
    with (OUT / "persons-summary.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=summary[0].keys())
        writer.writeheader()
        writer.writerows(summary)
    write_json("independent-events.json", {"strict_gap_seconds": STRICT_GAP, "relaxed_gap_seconds": RELAXED_GAP,
                                            "strict": strict, "relaxed": relaxed})
    write_json("event-definition-sensitivity.json", {"gap_distribution": gap_stats,
        "event_counts_by_gap_seconds": {str(g): {pid: len([e for e in events if e["person_id"] == pid]) for pid in by_person} for g, events in ((g, partitions[str(g)]) for g in GAPS)}})
    write_json("annotation-concentration.json", concentration)
    write_json("coverage-dimensions.json", coverage)
    write_json("accumulation-curves.json", {"curves": curves, "recent_500": growth})
    queue = [dict(r) for r in connection.execute("SELECT status,count(*) AS n FROM annotation_sample_queue GROUP BY status")]
    write_json("learning-blind-flow-audit.json", {"code_path": [
        "speaker.assign / person correction -> active human annotation_fact + original-audio anchors",
        "mobile_sync applied speaker.assign -> process_annotation_samples -> annotation_sample_queue(session)",
        "direct DeviceAnnotationService.assign -> assign_utterances only; this path does not explicitly enqueue; retry_samples/analyze can enqueue",
        "AnnotationSampleWorker -> compute_plans -> embed -> annotation_sample_sets(current=1) + voice_prototypes(candidate)",
        "human confirmed prototype review -> accepted voice_prototype; person_vectors matching requires accepted/human_confirmed and current eligibility",
        "no blind partition in these paths"],
        "code_references": ["application/speaker_annotation.py", "application/mobile_sync.py",
                            "interfaces/device_annotations.py", "application/people_clusters.py",
                            "application/annotation_samples.py", "adapters/sqlite/annotation_sample_plan.py",
                            "adapters/sqlite/people_repository_prototypes.py", "adapters/sqlite/people_repository_analysis.py"],
        "sample_queue_status": queue, "current_sample_sets": sum(r["current"] for r in sample_sets),
        "all_sample_sets": len(sample_sets), "blind_reservation_implemented": False})
    write_json("blind-readiness.json", {"frozen_blind_events_from_previous_audit": old_blind.get("blind_scored_independent_events"),
                                        "new_sessions_from_previous_audit": old_blind.get("new_sessions"),
                                        "post_freeze_sessions_now": len(post_freeze_sessions),
                                        "blind_zero_still_verified": blind_zero_verified,
                                        "historical_annotated_sessions": len({f["session_id"] for f in facts}),
                                        "historical_annotated_dates": len({f["date"] for f in facts}),
                                        "historical_annotated_strict_events": len(strict),
                                        "reservation_policy_present": False,
                                        "recommendation": "Reserve whole new session before sample generation; isolate blind from profiles and calibration."})
    active_person_sources = collections.defaultdict(lambda: collections.Counter())
    for row in connection.execute("SELECT value_json,actor FROM annotation_facts WHERE dimension='person' AND state='active'"):
        pid = json.loads(row["value_json"])
        if pid:
            active_person_sources[pid]["manual" if row["actor"] == "legacy-human" or row["actor"].startswith(("phone-operation:", "desktop-operation:")) else "other"] += 1
    verification = {"database_mode_ro": True, "query_only": bool(connection.execute("PRAGMA query_only").fetchone()[0]),
                    "database_revision": revision, "database_path": str(DB), "python": platform.python_version(),
                    "schema": schema, "all_active_manual_person_facts": len(all_facts),
                    "cross_person_original_audio_overlap_pairs": len(conflicting_pairs),
                    "included_facts": len(facts), "excluded_missing_person_session_or_audio": excluded,
                    "strict_event_count": len(strict), "relaxed_event_count": len(relaxed),
                    "active_person_fact_source_counts": {k: dict(v) for k, v in active_person_sources.items()},
                    "blind_uses_previous_freeze": bool(old_blind),
                    "blind_zero_still_verified": blind_zero_verified,
                    "created_utc": datetime.now(timezone.utc).isoformat()}
    write_json("verification.json", verification)
    (OUT / "PROTOCOL.md").write_text("""# Private coverage audit protocol\n\nAll identified outputs in this directory are private. The production SQLite database is opened with `mode=ro`, `query_only=ON`, and a read transaction. Only active `legacy-human`, `phone-operation:*`, and `desktop-operation:*` person facts with valid original-audio anchors and source session are counted. System identity facts are excluded.\n\nStrict event: same person and session utterances chained with at most 15 minutes between ranges; relaxed: 2 minutes. Overlapping windows of the same source audio SHA merge even across sessions. Sensitivity: 30, 120, 300, 900 seconds. Event speech seconds are the union of annotated original-audio ranges by SHA. `usable` means >=6 seconds of labelled ranges with a replica marked available in SQLite, not independently listened, verified single-speaker or model-scoreable. Dates use Asia/Singapore. Annotation counts are distinct facts.\n\nAccumulation is ordered by capture time. Recent 500 uses fact creation time; legacy imports can make this order artificial. Device, environment, distance, SNR and overlap are unobserved; neither date nor hour proves them.\n""", encoding="utf-8")
    make_plots(summary, curves, concentration)
    connection.rollback()
    connection.close()
    print(json.dumps({"persons": len(summary), "facts": len(facts), "strict_events": len(strict), "relaxed_events": len(relaxed), "revision": revision}, ensure_ascii=False))


def make_plots(summary, curves, concentration):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    x = list(range(len(summary)))
    labels = [f"P{i + 1:02}" for i in x]
    def bar(name, keys, ylabel):
        fig, ax = plt.subplots(figsize=(max(7, len(x) * 0.9), 4))
        width = 0.8 / len(keys)
        for i, (key, label) in enumerate(keys):
            ax.bar([p - 0.4 + width * (i + 0.5) for p in x], [r[key] for r in summary], width, label=label)
        ax.set_xticks(x, labels)
        ax.set_ylabel(ylabel)
        ax.legend()
        fig.tight_layout()
        fig.savefig(OUT / "plots" / name, dpi=150)
        plt.close(fig)
    bar("person-event-counts.png", [("independent_event_count", "strict"), ("relaxed_event_count", "relaxed")], "events")
    bar("person-session-date-coverage.png", [("session_count", "sessions"), ("date_count", "dates")], "count")
    fig, ax = plt.subplots(figsize=(7, 4))
    ax.scatter([r["annotation_count"] for r in summary], [r["independent_event_count"] for r in summary])
    for i, r in enumerate(summary):
        ax.annotate(labels[i], (r["annotation_count"], r["independent_event_count"]))
    ax.set(xlabel="active human annotations", ylabel="strict independent events")
    fig.tight_layout()
    fig.savefig(OUT / "plots" / "annotations-vs-events.png", dpi=150)
    plt.close(fig)
    focus = max(summary, key=lambda r: r["annotation_count"], default=None)
    if focus:
        points = curves[focus["person_id"]]
        fig, ax = plt.subplots(figsize=(8, 4))
        axis = [p["annotation_count"] for p in points]
        ax.plot(axis, [p["independent_event_count"] for p in points], label="strict events")
        ax.plot(axis, [p["session_count"] for p in points], label="sessions")
        ax.plot(axis, [p["date_count"] for p in points], label="dates")
        ax.set(xlabel="annotations in capture order", ylabel="distinct count")
        other = ax.twinx()
        other.plot(axis, axis, color="0.6", alpha=0.5, label="annotations")
        other.set_ylabel("annotations")
        ax.legend(loc="upper left")
        fig.tight_layout()
        fig.savefig(OUT / "plots" / "zhangweiping-accumulation.png", dpi=150)
        plt.close(fig)
        by_day = {}
        for point in points:
            by_day[point["capture_date"]] = point
        days = sorted(by_day)
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.plot(days, [by_day[d]["annotation_count"] for d in days], "o-", label="annotations")
        ax.set(xlabel="capture date", ylabel="cumulative annotations")
        other = ax.twinx()
        other.plot(days, [by_day[d]["independent_event_count"] for d in days], "s-", color="tab:orange", label="strict events")
        other.set_ylabel("cumulative events")
        ax.tick_params(axis="x", rotation=35)
        fig.tight_layout()
        fig.savefig(OUT / "plots" / "zhangweiping-by-date.png", dpi=150)
        plt.close(fig)
        c = concentration[focus["person_id"]]
        fig, ax = plt.subplots(figsize=(6, 4))
        ax.bar(["top 1 session", "top 3 sessions", "top 1 date"], [c["top1_session"], c["top3_sessions"], c["top1_date"]])
        ax.set_ylim(0, 1)
        ax.set_ylabel("share of annotations")
        fig.tight_layout()
        fig.savefig(OUT / "plots" / "zhangweiping-concentration.png", dpi=150)
        plt.close(fig)


if __name__ == "__main__":
    main()
