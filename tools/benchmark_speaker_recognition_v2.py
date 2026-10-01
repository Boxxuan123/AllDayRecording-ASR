"""Read-only, retrospective CAM++ speaker benchmark.

Run: python tools/benchmark_speaker_recognition_v2.py --output outputs/v2-replay-new
The old manifest and embeddings are immutable inputs; test labels are never used
to choose thresholds or profiles. Output is deliberately kept under outputs/.
"""

from __future__ import annotations

import collections
import argparse
import csv
import hashlib
import importlib.metadata
import json
import platform
import sqlite3
import subprocess
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
OLD = ROOT / "outputs/annotation-feasibility-20260928"
OUT = ROOT / "outputs/speaker-recognition-benchmark-v2-20260928"
SEEDS = (7, 17, 27, 37, 47)
THRESHOLDS = np.round(np.arange(0.20, 0.951, 0.01), 2)
MARGINS = (0.0, 0.03, 0.05, 0.08, 0.10, 0.15)


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(name, obj):
    (OUT / name).write_text(
        json.dumps(obj, ensure_ascii=False, indent=2, default=float), encoding="utf-8"
    )


def freeze_setting(name, setting):
    path = OUT / "frozen-settings.json"
    current = read(path) if path.exists() else {}
    current[name] = setting
    write("frozen-settings.json", current)


def unit(v):
    a = np.asarray(v, dtype=np.float64)
    return a / max(float(np.linalg.norm(a)), 1e-12)


def interval_union(rows):
    merged = []
    for a, b in sorted(rows):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(b, merged[-1][1])
        else:
            merged.append([a, b])
    return sum(b - a for a, b in merged)


def database():
    c = sqlite3.connect(
        (ROOT / "state/v3/core.sqlite3").resolve().as_uri() + "?mode=ro", uri=True
    )
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA query_only=ON")
    c.execute("BEGIN")
    return c


def human_only_manifest(original, c):
    """Exclude windows whose label came only from system identity projection."""
    kept, excluded = [], []
    for w in original["windows"]:
        rows = list(
            c.execute(
                """SELECT f.actor,f.value_json,a.start_ms,a.end_ms
            FROM annotation_fact_audio a JOIN annotation_facts f USING(fact_id)
            WHERE f.state='active' AND f.dimension='person' AND a.media_id=?
            AND a.start_ms<? AND a.end_ms>?""",
                (w["media_id"], w["end_ms"], w["start_ms"]),
            )
        )
        good = []
        conflict = False
        for r in rows:
            if r["actor"].startswith("system:"):
                continue
            pid = json.loads(r["value_json"])
            if pid != w["person_id"]:
                conflict = True
            else:
                good.append(
                    (max(w["start_ms"], r["start_ms"]), min(w["end_ms"], r["end_ms"]))
                )
        full = interval_union(good) == w["end_ms"] - w["start_ms"]
        (kept if full and not conflict else excluded).append(w)
    good_ids = {w["id"] for w in kept}
    groups = []
    for g in original["groups"]:
        row = g | {
            "windows": [wid for wid in g["windows"] if wid in good_ids],
            "longest": [wid for wid in g["longest"] if wid in good_ids],
        }
        if row["windows"]:
            groups.append(row)
    result = original | {
        "windows": kept,
        "groups": groups,
        "v2_filter": "Only active non-system person facts fully cover each window; conflicting human labels excluded",
        "old_manifest_sha256": hashlib.sha256(
            (OLD / "manifest.json").read_bytes()
        ).hexdigest(),
    }
    return result, excluded


def audit(m, vectors, c):
    ws = {w["id"]: w for w in m["windows"]}
    splits = collections.defaultdict(
        lambda: {"dates": set(), "sessions": set(), "windows": 0}
    )
    by_hash = collections.defaultdict(set)
    intervals = collections.defaultdict(list)
    missing_audio = []
    for w in m["windows"]:
        split = w["split"]
        splits[split]["dates"].add(w["date"])
        splits[split]["sessions"].add(w["session_id"])
        splits[split]["windows"] += 1
        by_hash[w["sha256"]].add(split)
        intervals[(w["sha256"], split)].append(
            (w["start_ms"], w["end_ms"], w["person_id"])
        )
        if not (ROOT / "state/v3/audio" / w["storage_key"]).is_file():
            missing_audio.append(w["id"])
    cross_split_hashes = [h for h, ss in by_hash.items() if len(ss) > 1]
    cross_split_dates = {s: sorted(x["dates"]) for s, x in splits.items()}
    assert len(ws) == len(m["windows"]) and set(ws).issubset(vectors)
    assert not cross_split_hashes and not missing_audio
    assert len(set(d for ds in cross_split_dates.values() for d in ds)) == sum(
        map(len, cross_split_dates.values())
    )
    norms = [float(np.linalg.norm(v)) for v in vectors.values()]
    finite = all(np.isfinite(v).all() for v in vectors.values())
    assert finite and max(abs(n - 1) for n in norms) < 1e-4
    unknown_enrollment = [
        g["person_id"]
        for g in m["groups"]
        if g["split"] == "train" and g["person_id"] not in m["known"]
    ]
    assert not unknown_enrollment
    duplicate_groups = []
    for g in m["groups"]:
        ids = g["windows"]
        for i, a in enumerate(ids):
            for b in ids[i + 1 :]:
                wa, wb = ws[a], ws[b]
                if (
                    wa["sha256"] == wb["sha256"]
                    and wa["start_ms"] < wb["end_ms"]
                    and wb["start_ms"] < wa["end_ms"]
                ):
                    duplicate_groups.append((a, b))
    # The extra 10 longest-five train windows may overlap sampled training windows.
    old_metrics = read(OLD / "results.json")
    assert old_metrics["test"]["longest5_centroid"]["grouped"]["correct"] == 16
    assert (
        old_metrics["test"]["longest5_centroid"]["grouped"].get("false_accept", 0) == 0
    )
    current_revision = c.execute(
        "SELECT revision FROM annotation_input_revision"
    ).fetchone()[0]
    audit_obj = {
        "original_windows": 555,
        "v2_human_covered_windows": len(ws),
        "removed_system_only_or_conflicting_windows": 555 - len(ws),
        "selected_windows": sum(len(g["windows"]) for g in m["groups"]),
        "split_sizes": {
            s: {
                "dates": sorted(v["dates"]),
                "sessions": len(v["sessions"]),
                "windows": v["windows"],
            }
            for s, v in splits.items()
        },
        "cross_split_media_hashes": cross_split_hashes,
        "within_selected_group_overlaps": duplicate_groups,
        "missing_audio": missing_audio,
        "unknown_in_enrollment": unknown_enrollment,
        "finite": finite,
        "norm_min": min(norms),
        "norm_max": max(norms),
        "annotation_revision_frozen": m["annotation_revision"],
        "annotation_revision_current": current_revision,
        "old_protocol_hash_matches": hashlib.sha256(
            (OLD / "PROTOCOL.md").read_bytes()
        ).hexdigest()
        == m["protocol_sha256"],
        "historical_test_tuning_in_code": False,
        "retrospective_test_already_inspected": True,
        "label_provenance": "V2 requires full coverage by active non-system person facts; not independently listened",
        "historical_issue": "Old pilot.prepare did not filter system:speaker-cluster-identity facts; 29 test windows had no full human label.",
        "conclusion": "Freeze human-covered subset of old split and vectors; prospective acceptance still requires new dates and verified truth.",
    }
    write("split-audit.json", audit_obj)
    write(
        "manifest.json",
        m
        | {
            "vector_cache_sha256": hashlib.sha256(
                (OLD / "vectors.npz").read_bytes()
            ).hexdigest()
        },
    )
    return ws, audit_obj


def track_coverage(c, m):
    test_dates = set(m["dates"]["test"])
    sessions = {
        r["session_id"]: r["captured_start"][:10]
        for r in c.execute("SELECT session_id,captured_start FROM recording_sessions")
    }
    names = m["names"]
    tracks = []
    for p in c.execute(
        "SELECT * FROM voice_prototypes WHERE source_prototype_id IS NULL AND speaker_track_id IS NOT NULL ORDER BY prototype_id"
    ):
        sid = c.execute(
            "SELECT session_id FROM speaker_tracks WHERE speaker_track_id=?",
            (p["speaker_track_id"],),
        ).fetchone()
        if not sid or sessions.get(sid[0]) not in test_dates:
            continue
        clips = json.loads(p["representative_clips_json"] or "[]")
        if not clips:
            continue
        person_ms = collections.Counter()
        covered_ms = 0
        mixed_ms = 0
        clip_details = []
        for clip in clips:
            a, b = clip["start_ms"], clip["end_ms"]
            rows = list(
                c.execute(
                    """SELECT f.value_json, fa.start_ms,fa.end_ms FROM annotation_fact_audio fa
                JOIN annotation_facts f ON f.fact_id=fa.fact_id
                WHERE f.state='active' AND f.dimension='person' AND f.actor NOT LIKE 'system:%' AND fa.media_id=?
                AND fa.start_ms<? AND fa.end_ms>?""",
                    (clip["media_id"], b, a),
                )
            )
            per = collections.defaultdict(list)
            bounds = {a, b}
            for r in rows:
                pid = json.loads(r["value_json"])
                if pid:
                    left, right = max(a, r["start_ms"]), min(b, r["end_ms"])
                    per[pid].append((left, right))
                    bounds.update((left, right))
            for left, right in zip(
                sorted(bounds)[:-1], sorted(bounds)[1:], strict=True
            ):
                labels = [
                    pid
                    for pid, spans in per.items()
                    if any(x <= left and y >= right for x, y in spans)
                ]
                if labels:
                    covered_ms += right - left
                    if len(labels) > 1:
                        mixed_ms += right - left
                    for pid in labels:
                        person_ms[pid] += right - left
            clip_details.append(
                {
                    "media_id": clip["media_id"],
                    "start_ms": a,
                    "end_ms": b,
                    "labelled_ms": interval_union(
                        [span for spans in per.values() for span in spans]
                    ),
                }
            )
        total = sum(x["end_ms"] - x["start_ms"] for x in clips)
        if not person_ms:
            status = "unlabelled"
        elif len(person_ms) > 1 or mixed_ms:
            status = "multiple_person_labels"
        elif covered_ms < total:
            status = "incomplete_labels"
        else:
            status = "fully_labelled_single_person"
        truth = (
            next(iter(person_ms)) if status == "fully_labelled_single_person" else None
        )
        tracks.append(
            {
                "prototype_id": p["prototype_id"],
                "session_id": sid[0],
                "date": sessions[sid[0]],
                "status": status,
                "truth_id": truth,
                "truth_name": names.get(truth) if truth else None,
                "person_ms": dict(person_ms),
                "labelled_fraction": covered_ms / total,
                "duration_s": total / 1000,
                "clips": clip_details,
                "vector": unit(json.loads(p["vector_json"])),
                "quality": p["quality_score"],
            }
        )
    return tracks


def queries(m, ws, vec, split, kind="groups", duration=None):
    result = []
    for g in m["groups"]:
        if g["split"] != split:
            continue
        ids = g["windows"]
        chunks = (
            [[x] for x in ids]
            if kind == "clips"
            else [ids[i : i + 5] for i in range(0, len(ids), 5)]
        )
        for chunk in chunks:
            if duration is not None:
                selected, seconds = [], 0.0
                for wid in chunk:
                    selected.append(wid)
                    seconds += (ws[wid]["end_ms"] - ws[wid]["start_ms"]) / 1000
                    if seconds >= duration:
                        break
                chunk = selected
            sec = sum((ws[x]["end_ms"] - ws[x]["start_ms"]) / 1000 for x in chunk)
            arr = np.stack([vec[x] for x in chunk])
            consistency = float(np.mean(arr @ unit(np.mean(arr, axis=0))))
            result.append(
                {
                    "id": f"{g['session_id']}:{g['person_id']}:{ids.index(chunk[0])}:{kind}:{duration}",
                    "truth": g["person_id"],
                    "session": g["session_id"],
                    "date": g["date"],
                    "windows": chunk,
                    "duration_s": sec,
                    "quality": float(min(1, sec / 6) * max(0, consistency)),
                    "vector": unit(np.mean(arr, axis=0)),
                }
            )
    return result


def enrollment(m, ws, vec, budget=None, strategy="longest", seed=7):
    rows = collections.defaultdict(list)
    for g in m["groups"]:
        if g["split"] == "train":
            for wid in g["windows"]:
                if wid not in {x["id"] for x in rows[g["person_id"]]}:
                    rows[g["person_id"]].append(
                        {
                            "id": wid,
                            "session": g["session_id"],
                            "date": g["date"],
                            "seconds": (ws[wid]["end_ms"] - ws[wid]["start_ms"]) / 1000,
                        }
                    )
    selected = {}
    for pid, pool in rows.items():
        center = unit(np.mean([vec[x["id"]] for x in pool], axis=0))
        rng = np.random.default_rng(seed)
        if strategy == "random":
            order = list(rng.permutation(len(pool)))
        elif strategy == "longest":
            order = sorted(range(len(pool)), key=lambda i: -pool[i]["seconds"])
        elif strategy == "quality":
            order = sorted(
                range(len(pool)),
                key=lambda i: (
                    -(
                        0.5 * min(1, pool[i]["seconds"] / 8)
                        + 0.5 * float(vec[pool[i]["id"]] @ center)
                    )
                ),
            )
        elif strategy == "diverse":
            queues = collections.defaultdict(list)
            for i, item in enumerate(pool):
                queues[(item["date"], item["session"])].append(i)
            for q in queues.values():
                q.sort(key=lambda i: -pool[i]["seconds"])
            order = []
            while any(queues.values()):
                for key in sorted(queues):
                    if queues[key]:
                        order.append(queues[key].pop(0))
        else:
            raise ValueError(strategy)
        chosen, total = [], 0.0
        for i in order:
            chosen.append(pool[i])
            total += pool[i]["seconds"]
            if budget is not None and total >= budget:
                break
        selected[pid] = chosen
    return selected


def profile(selected, vec, representation="centroid"):
    result = {}
    for pid, items in selected.items():
        arr = np.stack([vec[x["id"]] for x in items])
        if representation == "centroid":
            result[pid] = [unit(arr.mean(axis=0))]
        elif representation == "weighted":
            weights = np.array([x["seconds"] for x in items])
            result[pid] = [unit(np.average(arr, axis=0, weights=weights))]
        elif representation == "median":
            result[pid] = [unit(np.median(arr, axis=0))]
        elif representation == "all":
            result[pid] = list(arr)
        elif representation in ("2centroids", "3centroids"):
            k = min(int(representation[0]), len(arr))
            centers = [unit(arr.mean(axis=0))]
            while len(centers) < k:
                idx = np.argmin([max(float(v @ z) for z in centers) for v in arr])
                centers.append(arr[idx])
            for _ in range(8):
                labels = np.argmax(arr @ np.stack(centers).T, axis=1)
                centers = [
                    unit(arr[labels == j].mean(axis=0))
                    if np.any(labels == j)
                    else centers[j]
                    for j in range(k)
                ]
            result[pid] = centers
        else:
            raise ValueError(representation)
    return result


def old_profile(m, vec):
    frozen = read(OLD / "frozen-settings.json")
    refs = collections.defaultdict(list)
    for item in frozen["selected_refs"]["longest5_centroid"]:
        refs[item["person_id"]].append(
            unit(np.mean([vec[k] for k in item["windows"]], axis=0))
        )
    return dict(refs)


def scores(qs, refs, cohort=None):
    people = sorted(refs)
    raw = np.array(
        [[max(float(q["vector"] @ r) for r in refs[p]) for p in people] for q in qs]
    )
    if cohort is None:
        return people, raw
    # Symmetric adaptive score normalization. Cohort is train embeddings only.
    all_cohort = np.stack([v for _pid, v in cohort])
    qco = np.stack([q["vector"] for q in qs]) @ all_cohort.T
    qtop = np.sort(qco, axis=1)[:, -min(10, len(cohort)) :]
    qm, qsd = qtop.mean(axis=1), np.maximum(qtop.std(axis=1), 0.05)
    normed = np.zeros_like(raw)
    for j, pid in enumerate(people):
        others = np.stack([v for owner, v in cohort if owner != pid])
        ref = unit(np.mean(refs[pid], axis=0))
        top = np.sort(others @ ref)[-min(10, len(others)) :]
        normed[:, j] = 0.5 * (
            (raw[:, j] - qm) / qsd
            + (raw[:, j] - top.mean()) / max(float(top.std()), 0.05)
        )
    return people, normed


def predict(
    qs, people, matrix, threshold, margin, per_person=None, gate=6, quality_rule=None
):
    out = []
    for q, row in zip(qs, matrix, strict=True):
        order = np.argsort(-row)
        best, second = int(order[0]), int(order[1])
        score, second_score = float(row[best]), float(row[second])
        t = per_person.get(people[best], threshold) if per_person else threshold
        if quality_rule == "dynamic":
            t += 0.12 * (1 - q["quality"])
        if quality_rule == "discount":
            score -= 0.12 * (1 - q["quality"])
        accepted = (
            q["duration_s"] >= gate and score >= t and score - second_score >= margin
        )
        if quality_rule == "abstain" and q["quality"] < 0.55:
            accepted = False
        out.append(
            {k: v for k, v in q.items() if k != "vector"}
            | {
                "best": people[best],
                "score": score,
                "second": people[second],
                "second_score": second_score,
                "margin": score - second_score,
                "prediction": people[best] if accepted else None,
            }
        )
    return out


def metrics(rows, known):
    count = collections.Counter()
    per = collections.defaultdict(collections.Counter)
    for r in rows:
        truth, pred = r["truth"], r["prediction"]
        tag = (
            "correct"
            if pred == truth and truth in known
            else "false_reject"
            if truth in known and pred is None
            else "wrong_person"
            if truth in known
            else "unknown_false_accept"
            if pred
            else "unknown_reject"
        )
        count[tag] += 1
        count["known" if truth in known else "unknown"] += 1
        per[truth][tag] += 1
        per[truth]["count"] += 1
    known_n, unknown_n = count["known"], count["unknown"]
    derived = {
        "known_recall": count["correct"] / known_n if known_n else None,
        "known_identification_accuracy": count["correct"] / known_n
        if known_n
        else None,
        "false_reject_rate": count["false_reject"] / known_n if known_n else None,
        "wrong_person_rate": count["wrong_person"] / known_n if known_n else None,
        "unknown_false_accept_rate": count["unknown_false_accept"] / unknown_n
        if unknown_n
        else None,
        "macro_per_person_recall": float(
            np.mean(
                [per[p]["correct"] / per[p]["count"] for p in known if per[p]["count"]]
            )
        )
        if known_n
        else None,
        "risk_cost_5": 5 * (count["wrong_person"] + count["unknown_false_accept"])
        + count["false_reject"],
        "risk_cost_10": 10 * (count["wrong_person"] + count["unknown_false_accept"])
        + count["false_reject"],
        "risk_cost_20": 20 * (count["wrong_person"] + count["unknown_false_accept"])
        + count["false_reject"],
    }
    for key, value in derived.items():
        count[key] = value
    count["per_person"] = {p: dict(v) for p, v in per.items()}
    return dict(count)


def tune(qs, people, mat, known, margins=(0.05,), grid=THRESHOLDS, gate=6):
    candidates = []
    for t in grid:
        for margin in margins:
            result = metrics(
                predict(qs, people, mat, float(t), margin, gate=gate), known
            )
            risk = result.get("wrong_person", 0) + result.get("unknown_false_accept", 0)
            candidates.append(
                (
                    risk,
                    -(result["macro_per_person_recall"] or 0),
                    -result.get("correct", 0),
                    -float(t),
                    -margin,
                    float(t),
                    margin,
                )
            )
    best = min(candidates)
    return {"threshold": best[5], "margin": best[6], "validation_risk": best[0]}


def calibrate_per_person(qs, people, mat, known, global_settings):
    # Shrink to the global gate; each target needs at least five validation
    # positives and five negatives before any person-specific movement.
    result, support = {}, {}
    for pid in people:
        positive = sum(q["truth"] == pid for q in qs)
        negative = sum(q["truth"] != pid for q in qs)
        support[pid] = {"positive": positive, "negative": negative}
        if positive < 5 or negative < 5:
            result[pid] = global_settings["threshold"]
            continue
        candidate = []
        for t in THRESHOLDS:
            rows = predict(
                qs,
                people,
                mat,
                float(t),
                global_settings["margin"],
                per_person={p: 1.1 for p in people if p != pid},
            )
            wrong = sum(r["prediction"] == pid and r["truth"] != pid for r in rows)
            correct = sum(r["prediction"] == pid and r["truth"] == pid for r in rows)
            if wrong == 0:
                candidate.append((correct, float(t)))
        target = max(candidate)[1] if candidate else global_settings["threshold"]
        weight = positive / (positive + 10)
        result[pid] = round(
            weight * target + (1 - weight) * global_settings["threshold"], 3
        )
    return result, support


def evaluate_case(
    name,
    val,
    test,
    refs,
    known,
    *,
    fixed=None,
    margins=(0.05,),
    cohort=None,
    gate=6,
    quality_rule=None,
    per_person=False,
):
    people, vs = scores(val, refs, cohort)
    grid = np.round(np.arange(-4, 4.01, 0.1), 2) if cohort else THRESHOLDS
    setting = fixed or tune(
        val, people, vs, known, margins=margins, grid=grid, gate=gate
    )
    person_gates, support = (
        calibrate_per_person(val, people, vs, known, setting)
        if per_person
        else ({}, {})
    )
    frozen = setting | (
        {"per_person": person_gates, "support": support} if per_person else {}
    )
    freeze_setting(name, frozen)
    vr = predict(
        val,
        people,
        vs,
        setting["threshold"],
        setting["margin"],
        person_gates,
        gate,
        quality_rule,
    )
    _, ts = scores(test, refs, cohort)
    tr = predict(
        test,
        people,
        ts,
        setting["threshold"],
        setting["margin"],
        person_gates,
        gate,
        quality_rule,
    )
    return {
        "name": name,
        "settings": frozen,
        "validation": metrics(vr, known),
        "test": metrics(tr, known),
        "test_rows": tr,
        "validation_rows": vr,
    }


def compact(case):
    return {k: v for k, v in case.items() if k not in ("test_rows", "validation_rows")}


def prepare_output(output):
    output = Path(output).resolve()
    if not output.is_relative_to((ROOT / "outputs").resolve()):
        raise ValueError("benchmark output must stay in private ignored outputs/")
    # Exclusive creation also rejects an empty existing directory. Failed runs
    # retain their evidence; retries need a different directory.
    output.mkdir(parents=True, exist_ok=False)
    return output


def main(argv=None):
    global OUT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    OUT = prepare_output(args.output)
    write("frozen-settings.json", {})
    protocol = """# Speaker Recognition Benchmark V2 protocol

Retrospective reanalysis of frozen 2026-09-28 CAM++ vectors. The old manifest
contained 29 test windows supported only by system-generated person labels;
V2 freezes a human-fact-covered subset. No new encoder or production writes.
Date/session/media SHA splits are audited.
Train builds all profiles/cohorts. Validation alone selects thresholds, margins,
quality rules and candidate methods. Test is scored only after settings are
saved; since the prior pilot's test results are public, this is not a new blind
acceptance test. Grouped 5 labelled clips are controlled identity units, not
automatic tracks. Risks: wrong known and unknown false accept each cost 5/10/20
times a false reject. Zero observed error is not proof of zero population risk.

Enrollment budgets use 20/40/60/90 seconds and all train windows. Random uses
five fixed seeds. Actual selected durations can slightly exceed budget due to
whole-window sampling. Quality score uses duration and within-group embedding
consistency; no unsupported SNR claim. GMM requires >5 independent scores per
group and is withheld if unavailable. Delayed decisions use prefixes of each
five-clip group; such clips are labelled same-person and can be separated in
time. Continual analysis is rolling retrospective, never used for selection.
Track truth is derived from complete active person-fact coverage of each original
candidate prototype's representative clips. It does not certify whole tracks.
"""
    (OUT / "PROTOCOL.md").write_text(protocol, encoding="utf-8")
    original = read(OLD / "manifest.json")
    all_vec = dict(np.load(OLD / "vectors.npz"))
    c = database()
    m, excluded = human_only_manifest(original, c)
    raw_vec = {w["id"]: all_vec[w["id"]] for w in m["windows"]}
    ws, audit_result = audit(m, raw_vec, c)
    vec = {key: unit(value) for key, value in raw_vec.items()}
    write(
        "excluded-label-windows.json",
        [
            {
                "id": w["id"],
                "split": w["split"],
                "person_id": w["person_id"],
                "session_id": w["session_id"],
                "media_id": w["media_id"],
                "start_ms": w["start_ms"],
                "end_ms": w["end_ms"],
            }
            for w in excluded
        ],
    )
    tracks = track_coverage(c, m)
    known = set(m["known"])
    val, test = queries(m, ws, vec, "validation"), queries(m, ws, vec, "test")
    baseline_refs = old_profile(m, vec)
    old_setting = read(OLD / "frozen-settings.json")["settings"]["longest5_centroid"]
    baseline = [
        evaluate_case(
            "A0 production suggestion 0.92",
            val,
            test,
            baseline_refs,
            known,
            fixed={"threshold": 0.92, "margin": 0.05},
        ),
        evaluate_case(
            "A0 production anonymous 0.82",
            val,
            test,
            baseline_refs,
            known,
            fixed={"threshold": 0.82, "margin": 0.05},
        ),
        evaluate_case(
            "historical calibrated centroid",
            val,
            test,
            baseline_refs,
            known,
            fixed=old_setting,
        ),
    ]
    calibration = [
        evaluate_case("A1 score only", val, test, baseline_refs, known, margins=(0.0,)),
        evaluate_case(
            "A1 score + fixed margin", val, test, baseline_refs, known, margins=(0.05,)
        ),
        evaluate_case(
            "A3 calibrated margin", val, test, baseline_refs, known, margins=MARGINS
        ),
        evaluate_case(
            "A2 per-person + margin",
            val,
            test,
            baseline_refs,
            known,
            fixed=old_setting,
            per_person=True,
        ),
    ]
    cohort = [
        (pid, vec[w["id"]])
        for pid, items in enrollment(m, ws, vec).items()
        for w in items
    ]
    calibration.append(
        evaluate_case(
            "A4 AS-Norm",
            val,
            test,
            baseline_refs,
            known,
            cohort=cohort,
            margins=MARGINS,
        )
    )

    # Group-level robust score aggregation, using the same baseline profiles.
    # Each group has <=5 component scores. Report GMM as unsupported.
    def aggregate(group_queries, mode):
        result = [
            q | {"component_vectors": [vec[wid] for wid in q["windows"]]}
            for q in group_queries
        ]

        def matrix(qs):
            people = sorted(baseline_refs)
            values = []
            for q in qs:
                clip_scores = np.array(
                    [
                        [
                            max(float(v @ ref) for ref in baseline_refs[p])
                            for p in people
                        ]
                        for v in q["component_vectors"]
                    ]
                )
                if mode == "mean":
                    value = clip_scores.mean(axis=0)
                elif mode == "median":
                    value = np.median(clip_scores, axis=0)
                elif mode == "trimmed":
                    value = (
                        np.sort(clip_scores, axis=0)[1:-1].mean(axis=0)
                        if len(clip_scores) > 2
                        else clip_scores.mean(axis=0)
                    )
                else:
                    value = np.sort(clip_scores, axis=0)[
                        -min(2, len(clip_scores)) :
                    ].mean(axis=0)
                values.append(value)
            return people, np.stack(values)

        return result, matrix(result)

    robust = []
    for mode in ("mean", "median", "trimmed", "top2"):
        vq, (people, vm) = aggregate(val, mode)
        setting = tune(vq, people, vm, known, MARGINS)
        freeze_setting("A5 " + mode, setting)
        vr = predict(vq, people, vm, setting["threshold"], setting["margin"])
        tq, (_, tm) = aggregate(test, mode)
        tr = predict(tq, people, tm, setting["threshold"], setting["margin"])
        robust.append(
            {
                "name": "A5 " + mode,
                "settings": setting,
                "validation": metrics(vr, known),
                "test": metrics(tr, known),
                "test_rows": tr,
            }
        )
    calibration.extend(robust)
    profile_cases = []
    for budget in (20, 40, 60, 90, None):
        for strategy in ("random", "longest", "diverse", "quality"):
            seeds = SEEDS if strategy == "random" else (SEEDS[0],)
            for seed in seeds:
                selected = enrollment(m, ws, vec, budget, strategy, seed)
                for representation in (
                    "centroid",
                    "weighted",
                    "median",
                    "2centroids",
                    "3centroids",
                    "all",
                ):
                    name = (
                        f"B {budget or 'all'}s {strategy} {representation} seed{seed}"
                    )
                    case = evaluate_case(
                        name,
                        val,
                        test,
                        profile(selected, vec, representation),
                        known,
                        margins=MARGINS,
                    )
                    case["enrollment"] = {
                        p: {
                            "seconds": round(sum(x["seconds"] for x in items), 3),
                            "windows": [x["id"] for x in items],
                        }
                        for p, items in selected.items()
                    }
                    profile_cases.append(case)
    quality = [
        evaluate_case("C0 equal", val, test, baseline_refs, known, fixed=old_setting)
    ]
    for rule in ("abstain", "discount", "dynamic"):
        quality.append(
            evaluate_case(
                "C " + rule,
                val,
                test,
                baseline_refs,
                known,
                fixed=old_setting,
                quality_rule=rule,
            )
        )
    val_clips = queries(m, ws, vec, "validation", "clips")
    test_clips = queries(m, ws, vec, "test", "clips")
    for case, rule in zip(
        quality, (None, "abstain", "discount", "dynamic"), strict=True
    ):
        clip_case = evaluate_case(
            case["name"] + " clips",
            val_clips,
            test_clips,
            baseline_refs,
            known,
            fixed=old_setting,
            gate=0,
            quality_rule=rule,
        )
        case["validation_clips"] = clip_case["validation"]
        case["test_clips"] = clip_case["test"]
    # Temporal units use fixed old 5-clip groups, truncated to a cumulative
    # speech-duration budget. Tune every prefix only on validation.
    temporal = []
    for budget in (0, 3, 5, 10, 20):
        kind = "clips" if budget == 0 else "groups"
        vq = queries(m, ws, vec, "validation", kind, budget or None)
        tq = queries(m, ws, vec, "test", kind, budget or None)
        case = evaluate_case(
            f"D {budget or 'utterance'}s",
            vq,
            tq,
            baseline_refs,
            known,
            margins=MARGINS,
            gate=0 if budget == 0 else min(6, budget),
        )
        temporal.append(case)
    # Fixed validation-selected setting: pending until a prefix has at least
    # 3 seconds of speech and passes both gates; accepted identity is committed.
    delayed_rows = []
    for final in test:
        chunk = []
        committed = None
        speech_s = 0.0
        first_commit_s = None
        for wid in final["windows"]:
            chunk.append(wid)
            speech_s += (ws[wid]["end_ms"] - ws[wid]["start_ms"]) / 1000
            if speech_s < 3:
                continue
            probe = final | {
                "windows": list(chunk),
                "duration_s": speech_s,
                "vector": unit(np.mean([vec[x] for x in chunk], axis=0)),
            }
            people, mat = scores([probe], baseline_refs)
            decision = predict(
                [probe],
                people,
                mat,
                old_setting["threshold"],
                old_setting["margin"],
                gate=3,
            )[0]
            if decision["prediction"] is not None:
                committed = decision["prediction"]
                first_commit_s = speech_s
                break
        delayed_rows.append(
            {k: v for k, v in final.items() if k != "vector"}
            | {
                "prediction": committed,
                "time_to_identification_s": first_commit_s,
                "pending_to_end": committed is None,
            }
        )
    delayed = {
        "policy": "first passing prefix after >=3s, fixed validation gate 0.42/0.10",
        "test": metrics(delayed_rows, known),
        "identified_time_s": [
            x["time_to_identification_s"]
            for x in delayed_rows
            if x["time_to_identification_s"] is not None
        ],
        "rows": delayed_rows,
    }
    # Rolling simulation: score each day's labelled groups against profiles
    # from prior dates only. Identity is added after that day's decisions.
    rolling = []
    by_date = collections.defaultdict(list)
    for g in m["groups"]:
        by_date[g["date"]].append(g)
    history = collections.defaultdict(list)
    for day in sorted(by_date):
        day_groups = by_date[day]
        if len(history) >= 2:
            day_queries = []
            for g in day_groups:
                for i in range(0, len(g["windows"]), 5):
                    chunk = g["windows"][i : i + 5]
                    sec = sum(
                        (ws[x]["end_ms"] - ws[x]["start_ms"]) / 1000 for x in chunk
                    )
                    day_queries.append(
                        {
                            "id": f"{day}:{g['person_id']}:{i}",
                            "truth": g["person_id"],
                            "session": g["session_id"],
                            "date": day,
                            "windows": chunk,
                            "duration_s": sec,
                            "quality": min(1, sec / 6),
                            "vector": unit(np.mean([vec[x] for x in chunk], axis=0)),
                        }
                    )
            for limit in (None, 60):
                refs = {
                    p: [
                        unit(
                            np.mean(
                                [
                                    vec[x]
                                    for x in (
                                        ids
                                        if limit is None
                                        else ids[: max(1, int(limit / 6))]
                                    )
                                ],
                                axis=0,
                            )
                        )
                    ]
                    for p, ids in history.items()
                }
                if len(refs) < 2:
                    continue
                people, mat = scores(day_queries, refs)
                rr = predict(
                    day_queries,
                    people,
                    mat,
                    old_setting["threshold"],
                    old_setting["margin"],
                )
                rolling.append(
                    {
                        "date": day,
                        "profile": "all_prior" if limit is None else "capped_60s",
                        "enrollment_counts": {p: len(v) for p, v in history.items()},
                        "metrics": metrics(rr, set(refs)),
                    }
                )
        for g in day_groups:
            if g["person_id"] in known:
                history[g["person_id"]].extend(g["windows"])
    # Every candidate setting was persisted before its held-out scoring.
    safe_profiles = [
        x
        for x in profile_cases
        if x["validation"].get("wrong_person", 0) == 0
        and x["validation"].get("unknown_false_accept", 0) == 0
    ]
    # Break validation ties by a predeclared simple, date-diverse centroid.
    safe_profiles.sort(
        key=lambda x: (
            -(x["validation"].get("macro_per_person_recall") or 0),
            -x["validation"].get("correct", 0),
            0 if "diverse centroid" in x["name"] else 1,
            x["name"],
        )
    )
    selected = [baseline[2], calibration[3], safe_profiles[0]]
    track_results = []
    scored_tracks = [t for t in tracks if t["status"] == "fully_labelled_single_person"]
    for case in selected:
        refs = (
            baseline_refs
            if case["name"]
            in {x["name"] for x in baseline + calibration + quality + temporal}
            else profile(
                {
                    p: [
                        {
                            "id": wid,
                            "seconds": (ws[wid]["end_ms"] - ws[wid]["start_ms"]) / 1000,
                        }
                        for wid in info["windows"]
                    ]
                    for p, info in case["enrollment"].items()
                },
                vec,
                case["name"].split()[3],
            )
        )
        qs = [
            {
                "id": t["prototype_id"],
                "truth": t["truth_id"],
                "session": t["session_id"],
                "date": t["date"],
                "windows": t["clips"],
                "duration_s": t["duration_s"],
                "quality": t["quality"],
                "vector": t["vector"],
            }
            for t in scored_tracks
        ]
        people, mat = scores(qs, refs)
        setting = case["settings"]
        rr = predict(
            qs,
            people,
            mat,
            setting["threshold"],
            setting["margin"],
            setting.get("per_person"),
        )
        track_results.append(
            {"method": case["name"], "metrics": metrics(rr, known), "rows": rr}
        )
    coverage_counts = dict(collections.Counter(t["status"] for t in tracks))
    train_hashes = {w["sha256"] for w in m["windows"] if w["split"] == "train"}
    media_hashes = {
        r["media_id"]: r["sha256"]
        for r in c.execute("SELECT media_id,sha256 FROM audio_assets")
    }
    track_train_leak = [
        t["prototype_id"]
        for t in tracks
        if any(
            media_hashes.get(clip["media_id"]) in train_hashes for clip in t["clips"]
        )
    ]
    assert not track_train_leak
    # Count correlated original audio among scored tracks.
    shared_pairs = 0
    parents = list(range(len(scored_tracks)))

    def root(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    for i, left in enumerate(scored_tracks):
        for j in range(i + 1, len(scored_tracks)):
            right = scored_tracks[j]
            if any(
                a["media_id"] == b["media_id"]
                and a["start_ms"] < b["end_ms"]
                and b["start_ms"] < a["end_ms"]
                for a in left["clips"]
                for b in right["clips"]
            ):
                shared_pairs += 1
                parents[root(i)] = root(j)
    components = collections.defaultdict(list)
    for i, track in enumerate(scored_tracks):
        components[root(i)].append(track["prototype_id"])
    component_summary = []
    for ids in components.values():
        item = {"prototype_ids": ids, "count": len(ids), "outcomes": {}}
        for method in track_results:
            relevant = [r for r in method["rows"] if r["id"] in ids]
            item["outcomes"][method["method"]] = {
                "any_false_assignment": any(
                    r["prediction"] is not None and r["prediction"] != r["truth"]
                    for r in relevant
                ),
                "all_correct_if_known": all(
                    r["prediction"] == r["truth"] for r in relevant
                )
                if relevant[0]["truth"] in known
                else None,
            }
        component_summary.append(item)
    write(
        "track-level-results.json",
        {
            "coverage": coverage_counts,
            "track_count": len(tracks),
            "scored_track_pairs_sharing_original_audio": shared_pairs,
            "train_media_hash_overlap": track_train_leak,
            "overlap_component_count": len(components),
            "overlap_components": component_summary,
            "tracks": [{k: v for k, v in t.items() if k != "vector"} for t in tracks],
            "scored": track_results,
            "limitation": "Original automatic candidate representative clips only; 24 labelled prototypes across 4 sessions, some source overlap; insufficient for production acceptance.",
        },
    )
    failures = []
    selected_names = {x["name"] for x in selected}
    for case in baseline + calibration + profile_cases + quality + temporal:
        for r in case["test_rows"]:
            risky = r["prediction"] is not None and r["prediction"] != r["truth"]
            representative_reject = (
                case["name"] in selected_names
                and r["truth"] in known
                and r["prediction"] is None
            )
            if risky or representative_reject:
                failures.append(
                    {
                        "method": case["name"],
                        "category": "false_assignment" if risky else "false_reject",
                        "enrollment": case.get("enrollment"),
                        **{k: v for k, v in r.items() if k != "component_vectors"},
                    }
                )
    for x in track_results:
        for r in x["rows"]:
            if r["prediction"] != r["truth"]:
                failures.append(
                    {"method": x["method"], "unit": "real_track_representative", **r}
                )
    write("failure-cases.json", failures)
    write("baseline-results.json", [compact(x) for x in baseline])
    write(
        "calibration-results.json",
        [compact(x) for x in calibration]
        + [
            {
                "name": "A5 2-component GMM",
                "status": "insufficient evidence",
                "reason": "At most 5 clips per controlled group; unstable 2-component fit.",
            }
        ],
    )
    write("profile-results.json", [compact(x) for x in profile_cases])
    write("quality-results.json", [compact(x) for x in quality])
    write(
        "temporal-results.json",
        {"prefix_cases": [compact(x) for x in temporal], "delayed_commitment": delayed},
    )
    write(
        "continual-learning-results.json",
        {
            "rolling": rolling,
            "limitation": "Retrospective daily sequence reuses old validation/test labels only after each day; descriptive, not independent acceptance.",
        },
    )
    rng = np.random.default_rng(20260928)
    boot = {}
    for case in selected:
        rows = case["test_rows"]
        sessions = sorted(set(r["session"] for r in rows))
        values = []
        for _ in range(2000):
            sampled = rng.choice(sessions, len(sessions), replace=True)
            subset = [r for sid in sampled for r in rows if r["session"] == sid]
            mm = metrics(subset, known)
            if mm.get("known", 0) and mm.get("unknown", 0):
                values.append((mm["known_recall"], mm["unknown_false_accept_rate"]))
        boot[case["name"]] = {
            "unit": "session",
            "resamples": len(values),
            "known_recall_95pct": list(
                np.quantile([v[0] for v in values], [0.025, 0.975])
            ),
            "unknown_far_95pct": list(
                np.quantile([v[1] for v in values], [0.025, 0.975])
            ),
        }
    write("confidence-results.json", boot)
    with (OUT / "summary.csv").open("w", newline="", encoding="utf-8-sig") as f:
        columns = [
            "method",
            "validation_correct",
            "validation_wrong",
            "validation_unknown_fa",
            "test_known",
            "test_correct",
            "test_recall",
            "test_wrong",
            "test_unknown_fa",
            "test_false_reject",
            "test_cost10",
        ]
        w = csv.DictWriter(f, columns)
        w.writeheader()
        for x in baseline + calibration + profile_cases + quality + temporal:
            a, b = x["validation"], x["test"]
            w.writerow(
                dict(
                    method=x["name"],
                    validation_correct=a.get("correct", 0),
                    validation_wrong=a.get("wrong_person", 0),
                    validation_unknown_fa=a.get("unknown_false_accept", 0),
                    test_known=b["known"],
                    test_correct=b.get("correct", 0),
                    test_recall=b["known_recall"],
                    test_wrong=b.get("wrong_person", 0),
                    test_unknown_fa=b.get("unknown_false_accept", 0),
                    test_false_reject=b.get("false_reject", 0),
                    test_cost10=b["risk_cost_10"],
                )
            )
    with (OUT / "per-person.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(
            f,
            [
                "method",
                "person_id",
                "name",
                "count",
                "correct",
                "false_reject",
                "wrong_person",
                "unknown_false_accept",
            ],
        )
        w.writeheader()
        for x in selected:
            for pid, p in x["test"]["per_person"].items():
                w.writerow(
                    dict(
                        method=x["name"],
                        person_id=pid,
                        name=m["names"].get(pid),
                        **{
                            key: p.get(key, 0)
                            for key in (
                                "count",
                                "correct",
                                "false_reject",
                                "wrong_person",
                                "unknown_false_accept",
                            )
                        },
                    )
                )
    git_commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    old_runtime = read(OLD / "runtime.json")
    model_hash_now = hashlib.sha256(
        Path(old_runtime["model_file"]).read_bytes()
    ).hexdigest()
    assert model_hash_now == old_runtime["model_sha256"]
    verification = {
        "python": platform.python_version(),
        "numpy": np.__version__,
        "funasr": importlib.metadata.version("funasr"),
        "torch": importlib.metadata.version("torch"),
        "model": "FunASR/CAM++ v1-local",
        "model_sha256": model_hash_now,
        "manifest_sha256": hashlib.sha256(
            (OUT / "manifest.json").read_bytes()
        ).hexdigest(),
        "random_seeds": SEEDS,
        "git_commit": git_commit,
        "database_read_only": True,
        "annotation_revision": audit_result["annotation_revision_current"],
        "test_not_used_for_selection": True,
        "historical_test_previously_seen": True,
        "selected_from_validation": [x["name"] for x in selected],
        "vector_count": len(vec),
        "track_coverage": coverage_counts,
    }
    write("verification.json", verification)
    print(
        json.dumps(
            {
                "baseline": [
                    {
                        "name": x["name"],
                        "test": {
                            k: x["test"].get(k, 0)
                            for k in (
                                "correct",
                                "wrong_person",
                                "unknown_false_accept",
                                "false_reject",
                            )
                        },
                    }
                    for x in baseline
                ],
                "selected": verification["selected_from_validation"],
                "tracks": coverage_counts,
                "output": str(OUT),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    c.close()


if __name__ == "__main__":
    main()
