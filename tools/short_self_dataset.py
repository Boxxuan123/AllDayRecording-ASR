"""Read-only source selection and provenance for private short-speech audits."""

import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from importlib.metadata import version
from pathlib import Path

import numpy as np

from audit_self_identity_regression import fingerprints
from allday_asr.v3.adapters.models.funasr import _cached_model_or_id
from allday_asr.v3.adapters.sqlite.blind_queries import _evidence
from allday_asr.v3.domain.blind_windows import plan_source_windows
from allday_asr.v3.domain.sound_kind import sound_uses
from allday_asr.v3.domain.speaker_turns import foreign_turns

BINS = ("1-2", "2-3", "3-4", "4-6", "6+")
FLAGS = (
    "was_enrollment",
    "was_calibration_positive",
    "was_calibration_negative",
    "was_profile_learning",
    "was_previous_diagnostic",
    "was_blind",
    "was_holdout",
)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        while block := f.read(4 * 1024 * 1024):
            h.update(block)
    return h.hexdigest()


def write(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def readonly(state):
    c = sqlite3.connect(
        (Path(state).resolve() / "core.sqlite3").as_uri() + "?mode=ro", uri=True
    )
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA query_only=ON")
    c.execute("BEGIN")
    return c


def duration_bin(ms):
    if ms < 1000:
        raise ValueError("event is shorter than the audit minimum")
    return (
        BINS[0]
        if ms < 2000
        else BINS[1]
        if ms < 3000
        else BINS[2]
        if ms < 4000
        else BINS[3]
        if ms < 6000
        else BINS[4]
    )


def intersects(a, b):
    return (
        a["media_id"] == b["media_id"]
        and a["start_ms"] < b["end_ms"]
        and b["start_ms"] < a["end_ms"]
    )


def subtract(ranges, lo, hi):
    return [
        (a, b)
        for start, end in ranges
        for a, b in [(start, min(end, lo)), (max(start, hi), end)]
        if b > a
    ]


def integrity(c):
    result = fingerprints(c)
    for table in (
        "utterances",
        "correction_operations",
        "person_profile_revisions",
        "persons",
        "speaker_clusters",
        "speaker_cluster_runs",
        "speaker_cluster_memberships",
    ):
        rows = sorted([tuple(r) for r in c.execute("SELECT * FROM " + table)], key=str)
        result[table] = {
            "rows": len(rows),
            "sha256": hashlib.sha256(
                json.dumps(rows, default=str).encode()
            ).hexdigest(),
        }
    return result


def assets(state):
    state = Path(state)
    policy_path = state / "identity/active-self-identity-policy.json"
    policy = read(policy_path)
    npz = Path(policy["voiceprint"])
    with np.load(npz, allow_pickle=False) as payload:
        metadata = json.loads(str(payload["metadata_json"]))
    calibration_path = (
        state
        / "identity"
        / f"identity-calibration-{policy['calibration_sha256'][:12]}.json"
    )
    model = Path(_cached_model_or_id("cam++"))
    return {
        "policy": digest(policy_path),
        "voiceprint": digest(npz),
        "threshold": policy["self_threshold"],
        "metadata_sha256": hashlib.sha256(
            json.dumps(metadata, sort_keys=True).encode()
        ).hexdigest(),
        "metadata": metadata,
        "calibration": digest(calibration_path),
        "calibration_path": str(calibration_path),
        "model": {
            p.name: digest(p)
            for p in model.iterdir()
            if p.is_file() and p.suffix in (".bin", ".json", ".yaml")
        },
        "funasr_version": version("funasr"),
        "enrollment_originals": {
            s["path"]: digest(s["path"]) for s in metadata["source_files"]
        },
    }


def calibration_ranges(calibration, legacy_db):
    """Link calibration by source content and session key, not human person names."""
    result = []
    if legacy_db is not None:
        legacy = sqlite3.connect(
            Path(legacy_db).resolve().as_uri() + "?mode=ro", uri=True
        )
        legacy.row_factory = sqlite3.Row
        for sample in calibration["samples"]:
            for cap in legacy.execute(
                """SELECT s.*,o.sha256 FROM session_sources s
                JOIN source_objects o ON o.id=s.source_object_id WHERE s.session_id=?""",
                (sample["session_id"],),
            ):
                lo = max(sample["start_ms"], cap["session_start_ms"])
                hi = min(sample["end_ms"], cap["session_end_ms"])
                if hi > lo:
                    offset = cap["source_start_ms"] - cap["session_start_ms"]
                    result.append(
                        {
                            "sha256": cap["sha256"],
                            "start_ms": lo + offset,
                            "end_ms": hi + offset,
                            "identity": sample["identity"],
                            "session_key": sample["session_key"],
                        }
                    )
        legacy.close()
    return result


def split_dates(events):
    dates = sorted({e["date"] for e in events})
    # Fixed chronological split, chosen without reading any embedding/score.
    count = max(1, (len(dates) + 2) // 3) if len(dates) > 1 else 0
    held = set(dates[-count:]) if count else set()
    return {d: "holdout" if d in held else "development" for d in dates}


def prepare(
    state,
    output,
    previous_manifest,
    *,
    legacy_db=None,
    previous_trace=None,
    max_per_bin=24,
):
    state, output = Path(state).resolve(), Path(output).resolve()
    if output.is_relative_to(state) or output == state.parent:
        raise ValueError("private audit output must be outside runtime state")
    frozen_path = output / "split-manifest.json"
    if frozen_path.exists():
        raise ValueError("split is already frozen; do not reselect or overwrite it")
    output.mkdir(parents=True, exist_ok=True)
    frozen_assets = assets(state)
    c = readonly(state)
    write(output / "before/fingerprints.json", integrity(c))
    write(output / "before/assets.json", frozen_assets)
    calibration = read(frozen_assets["calibration_path"])
    calib_sources = calibration_ranges(calibration, legacy_db)
    previous = read(previous_manifest)["events"]
    previous_ids = {e["utterance_id"] for e in previous}
    previous_sessions = {e["session_id"] for e in previous}
    enrollment_hashes = {s["sha256"] for s in frozen_assets["metadata"]["source_files"]}
    calibration_keys = {s["session_key"] for s in calibration["samples"]}
    prototype_clips = []
    for r in c.execute(
        "SELECT representative_clips_json FROM voice_prototypes WHERE human_confirmed=1"
    ):
        prototype_clips.extend(json.loads(r[0]))
    for r in c.execute("SELECT windows_json FROM annotation_sample_sets"):
        prototype_clips.extend(json.loads(r[0]))
    profile_sessions = {
        r[0] for r in c.execute("SELECT session_id FROM annotation_sample_sets")
    }
    self_id = c.execute("SELECT person_id FROM persons WHERE kind='self'").fetchone()[0]
    roles = {
        r[0]: r[1]
        for r in c.execute("SELECT session_id,dataset_role FROM session_dataset_roles")
    }
    media_truth = defaultdict(list)
    for r in c.execute("""SELECT f.value_json,f.actor,a.* FROM annotation_facts f JOIN annotation_fact_audio a USING(fact_id)
        WHERE f.dimension='person' AND f.state='active' AND f.actor NOT LIKE 'system:%' """):
        media_truth[r["media_id"]].append(
            dict(r) | {"person": json.loads(r["value_json"])}
        )
    purity = defaultdict(list)
    for r in c.execute("""SELECT s.*,e.* FROM speaker_purity_sources s JOIN speaker_purity_current cur USING(source_key)
        JOIN speaker_source_purity_evidence e USING(evidence_id)"""):
        purity[r["source_media_id"]].append(dict(r))
    data_cache = {}
    capture_cache = {}
    excluded = Counter()
    inventory = []
    rows = c.execute("""SELECT u.*,t.label AS original_label,s.captured_start,s.legacy_ref,
        f.fact_id,f.value_json,f.actor,f.payload_json FROM annotation_facts f
        JOIN utterances u ON u.utterance_id=f.source_utterance_id
        JOIN speaker_tracks t ON t.speaker_track_id=u.original_speaker_track_id
        JOIN recording_sessions s ON s.session_id=u.session_id
        WHERE f.dimension='person' AND f.state='active' AND f.actor NOT LIKE 'system:%'
        AND u.status='active' ORDER BY u.session_id,u.start_ms,u.utterance_id""").fetchall()
    seen_u = set()

    def flags(e):
        clip = {
            "media_id": e["media_id"],
            "start_ms": e["source_start_ms"],
            "end_ms": e["source_end_ms"],
        }
        linked = [
            s
            for s in calib_sources
            if s["sha256"] == e["sha256"]
            and s["start_ms"] < clip["end_ms"]
            and clip["start_ms"] < s["end_ms"]
        ]
        same_calib_session = any(
            e.get("legacy_ref", "").endswith(key) for key in calibration_keys
        )
        return {
            "was_enrollment": e["sha256"] in enrollment_hashes,
            "was_calibration_positive": any(s["identity"] == "self" for s in linked),
            "was_calibration_negative": any(
                s["identity"] == "not_self" for s in linked
            ),
            "was_profile_learning": any(intersects(clip, p) for p in prototype_clips),
            "was_previous_diagnostic": e["utterance_id"] in previous_ids,
            "was_blind": roles.get(e["session_id"]) == "blind",
            "was_holdout": roles.get(e["session_id"]) == "holdout",
            "calibration_session_exposure": same_calib_session,
            "profile_session_exposure": e["session_id"] in profile_sessions,
            "previous_diagnostic_session_exposure": e["session_id"]
            in previous_sessions,
            "calibration_linkage_available": legacy_db is not None,
        }

    for u in rows:
        uid, sid = u["utterance_id"], u["session_id"]
        if uid in seen_u:
            continue
        seen_u.add(uid)
        if not sound_uses(json.loads(u["evidence_json"]))["sample_candidate_allowed"]:
            excluded["excluded_sound"] += 1
            continue
        if roles.get(sid) in ("blind", "holdout"):
            excluded["frozen_blind_or_holdout"] += 1
            continue
        if u["run_id"] not in data_cache:
            try:
                data_cache[u["run_id"]] = _evidence(c, u["run_id"], state / "artifacts")
            except (ValueError, OSError, KeyError) as ex:
                data_cache[u["run_id"]] = {"error": str(ex)}
        data = data_cache[u["run_id"]]
        if "error" in data:
            excluded["automatic_evidence_unavailable"] += 1
            continue
        if sid not in capture_cache:
            capture_cache[sid] = [
                dict(r)
                for r in c.execute(
                    """SELECT s.*,a.media_id,a.sha256,a.duration_ms,r.storage_key
                FROM capture_segments s JOIN audio_assets a USING(asset_id) JOIN audio_replicas r USING(replica_id)
                WHERE s.session_id=? AND r.state='available' ORDER BY s.sequence""",
                    (sid,),
                )
            ]
        facts = [
            dict(r)
            for r in c.execute(
                "SELECT * FROM annotation_fact_audio WHERE fact_id=?", (u["fact_id"],)
            )
        ]
        person = json.loads(u["value_json"])
        diar = data["v3_diarization_evidence"]
        turns = diar["exclusive_turns"]
        # Never silently turn mixed events into single-speaker training examples.
        foreign = foreign_turns(
            u["start_ms"],
            u["end_ms"],
            u["original_label"],
            diar.get("regular_turns", []),
        )
        foreign_exclusive = foreign_turns(
            u["start_ms"], u["end_ms"], u["original_label"], turns
        )
        if foreign or foreign_exclusive:
            excluded["foreign_or_overlap"] += 1
            continue
        owned = [
            (max(u["start_ms"], t["start_ms"]), min(u["end_ms"], t["end_ms"]))
            for t in turns
            if t["speaker_label"] == u["original_label"]
            and t["start_ms"] < u["end_ms"]
            and t["end_ms"] > u["start_ms"]
        ]
        options = []
        for lo, hi in owned:
            if hi - lo < 1000:
                continue
            plan = plan_source_windows(
                dict(u) | {"start_ms": lo, "end_ms": hi},
                u["original_label"],
                u["original_speaker_track_id"],
                turns,
                [],
                [],
                capture_cache[sid],
                "short-self-audit-v1",
            )
            if plan["exclusions"]:
                excluded["incomplete_source_mapping"] += 1
                continue
            for window in plan["windows"]:
                clip = {
                    "media_id": window["media_id"],
                    "start_ms": window["start_ms"],
                    "end_ms": window["end_ms"],
                }
                if not any(
                    f["media_id"] == clip["media_id"]
                    and f["start_ms"] <= clip["start_ms"]
                    and f["end_ms"] >= clip["end_ms"]
                    for f in facts
                ):
                    continue
                if any(
                    t["person"] != person and intersects(clip, t)
                    for t in media_truth[clip["media_id"]]
                ):
                    excluded["conflicting_human_identity"] += 1
                    continue
                if any(
                    p["start_ms"] < clip["end_ms"]
                    and clip["start_ms"] < p["end_ms"]
                    and (
                        p["purity"] not in ("clean_single", "unreviewed")
                        or p["conflicting"]
                    )
                    for p in purity[clip["media_id"]]
                ):
                    excluded["adverse_human_composition_review"] += 1
                    continue
                if clip["end_ms"] - clip["start_ms"] >= 1000:
                    options.append(window)
        if not options:
            excluded["no_confirmed_safe_source_window"] += 1
            continue
        w = sorted(
            options,
            key=lambda x: (
                -(x["end_ms"] - x["start_ms"]),
                x["session_start_ms"],
                x["media_id"],
            ),
        )[0]
        e = {
            "event_id": uid,
            "utterance_id": uid,
            "session_id": sid,
            "date": u["captured_start"][:10],
            "truth": "self" if person == self_id else "non-self",
            "truth_person_id": person,
            "truth_actor": u["actor"],
            "fact_id": u["fact_id"],
            "human_audio_anchors": facts,
            "identity_anchor_limit": "human person audio anchor plus automatic acoustic ownership; composition not inferred from text",
            "media_id": w["media_id"],
            "sha256": w["sha256"],
            "storage_key": w["storage_key"],
            "source_start_ms": w["start_ms"],
            "source_end_ms": w["end_ms"],
            "session_start_ms": w["session_start_ms"],
            "session_end_ms": w["session_end_ms"],
            "duration_ms": w["end_ms"] - w["start_ms"],
            "original_utterance_range": [u["start_ms"], u["end_ms"]],
            "original_speaker_label": u["original_label"],
            "automatic_turns": w["provenance"]["exclusive_turn_coverage"],
            "safe_ownership": True,
            "dataset_role": roles.get(sid),
            "legacy_ref": u["legacy_ref"],
            "bin": duration_bin(w["end_ms"] - w["start_ms"]),
        }
        e.update(flags(e))
        if e["was_enrollment"]:
            excluded["enrollment_source"] += 1
            continue
        inventory.append(e)
    # Balanced deterministic selection across sessions, never scores.
    groups = defaultdict(lambda: defaultdict(list))
    for e in inventory:
        groups[e["truth"], e["bin"]][e["session_id"]].append(e)
    selected = []
    for _, sessions in sorted(groups.items()):
        ordered = [
            sorted(v, key=lambda e: (e["session_start_ms"], e["event_id"]))
            for _, v in sorted(sessions.items())
        ]
        used = defaultdict(list)
        n = 0
        for rank in range(max(map(len, ordered), default=0)):
            for rows_in_session in ordered:
                if rank >= len(rows_in_session) or n >= max_per_bin:
                    continue
                e = rows_in_session[rank]
                if any(
                    abs(e["session_start_ms"] - x) < 10000
                    for x in used[e["session_id"]]
                ):
                    continue
                selected.append(e)
                used[e["session_id"]].append(e["session_start_ms"])
                n += 1
    # Reference cohort is exact, including unsafe historical false accepts.
    historical = {}
    if previous_trace:
        historical = {
            e["utterance_id"]: e for e in read(previous_trace)["historical_events"]
        }
    selected = [e for e in selected if e["utterance_id"] not in previous_ids]
    for i, old in enumerate(previous):
        u = c.execute(
            "SELECT u.*,s.captured_start,s.legacy_ref FROM utterances u JOIN recording_sessions s USING(session_id) WHERE utterance_id=?",
            (old["utterance_id"],),
        ).fetchone()
        asset = c.execute(
            "SELECT sha256 FROM audio_assets WHERE media_id=?", (old["media_id"],)
        ).fetchone()
        e = dict(old) | {
            "event_id": "previous:" + old["utterance_id"],
            "date": old["captured_start"][:10],
            "duration_ms": old["source_end_ms"] - old["source_start_ms"],
            "sha256": asset[0],
            "legacy_ref": u["legacy_ref"],
            "bin": duration_bin(old["source_end_ms"] - old["source_start_ms"]),
            "safe_ownership": False,
            "reference_only": True,
            "previous_event_number": i + 1,
            "anchor": "anchor_1" if i == 1 else "anchor_5" if i == 6 else None,
            "historical_false_accept": historical.get(old["utterance_id"], {})
            .get("clean_query_decision", {})
            .get("decision")
            == "self"
            and old["truth"] == "non-self",
        }
        e.update(flags(e))
        data = data_cache.get(u["run_id"])
        if data is None:
            try:
                data = _evidence(c, u["run_id"], state / "artifacts")
            except (ValueError, OSError, KeyError):
                data = {"error": "automatic evidence unavailable"}
        label = c.execute(
            "SELECT label FROM speaker_tracks WHERE speaker_track_id=?",
            (u["original_speaker_track_id"],),
        ).fetchone()[0]
        if "error" not in data:
            diar = data["v3_diarization_evidence"]
            regular = foreign_turns(
                u["start_ms"], u["end_ms"], label, diar.get("regular_turns", [])
            )
            e.update(
                regular_foreign_crossing=regular,
                exclusive_turns=[
                    t
                    for t in diar["exclusive_turns"]
                    if t["start_ms"] < u["end_ms"] and t["end_ms"] > u["start_ms"]
                ],
            )
            e["safe_ownership"] = not regular and any(
                t["speaker_label"] == label
                and t["start_ms"] <= u["start_ms"]
                and t["end_ms"] >= u["end_ms"]
                for t in diar["exclusive_turns"]
            )
        selected.append(e)
    dates = split_dates(selected)
    for e in selected:
        contaminated = (
            any(e[f] for f in FLAGS)
            or e["calibration_session_exposure"]
            or not e["calibration_linkage_available"]
        )
        e["split"] = "reference" if contaminated else dates[e["date"]]
        e["strict_independent"] = (
            not contaminated
            and not e["previous_diagnostic_session_exposure"]
            and not e["profile_session_exposure"]
        )
    dev = {e["session_id"] for e in selected if e["split"] == "development"}
    held = {e["session_id"] for e in selected if e["split"] == "holdout"}
    assert not dev & held
    manifest = {
        "format": "short-self-offline-v1",
        "selection": "longest confirmed safe single-media window, <=8s; balanced bins/session round robin; >=10s event separation",
        "date_split": dates,
        "calibration_linkage": "legacy source SHA/range plus conservative entire calibration-session exclusion",
        "max_per_bin": max_per_bin,
        "independence_limit": "previous diagnostic/profile sessions excluded from strict independence even when new event ranges differ",
        "events": selected,
    }
    write(output / "inventory.json", {"events": inventory, "excluded": dict(excluded)})
    write(frozen_path, manifest)
    (output / "split-manifest.sha256").write_text(
        digest(frozen_path) + "\n", encoding="ascii"
    )
    summary = {
        "events": len(selected),
        "counts": dict(Counter(e["truth"] for e in selected)),
        "splits": {
            s: dict(Counter(e["truth"] for e in selected if e["split"] == s))
            for s in ("development", "holdout", "reference")
        },
        "independent_splits": {
            s: dict(
                Counter(
                    e["truth"]
                    for e in selected
                    if e["split"] == s and e["strict_independent"]
                )
            )
            for s in ("development", "holdout")
        },
        "sessions": len({e["session_id"] for e in selected}),
        "dates": len({e["date"] for e in selected}),
        "bins": {
            b: dict(Counter(e["truth"] for e in selected if e["bin"] == b))
            for b in BINS
        },
        "flags": {f: sum(e[f] for e in selected) for f in FLAGS},
        "excluded": dict(excluded),
        "manifest_sha256": digest(frozen_path),
        "calibration_source_ranges": len(calib_sources),
    }
    write(output / "dataset-summary.json", summary)
    assert integrity(c) == read(output / "before/fingerprints.json")
    c.close()
    return summary
