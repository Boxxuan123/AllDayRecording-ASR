"""Frozen G/P scoring and conservative connected components, independent of truth."""

import hashlib
import json
import math
from collections import Counter



def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def unit(vector):
    value = tuple(float(v) for v in vector)
    norm = math.sqrt(sum(v*v for v in value))
    if not value or not all(math.isfinite(v) for v in value) or norm == 0:
        raise ValueError("invalid embedding")
    return tuple(v / norm for v in value)


def score(vector, duration_s, refs, frozen):
    query = unit(vector)
    ordered = sorted(((person, max(sum(a*b for a,b in zip(query, unit(v), strict=True)) for v in vectors))
                      for person, vectors in refs.items()), key=lambda r: (-r[1], r[0]))
    if len(ordered) < 2:
        raise ValueError("frozen matcher requires at least two target profiles")
    best, best_score = ordered[0]
    second, second_score = ordered[1]
    margin = best_score - second_score
    decisions = {}
    for rule in ("G", "P"):
        config = frozen[rule]
        threshold = config["threshold"] if rule == "G" else config["per_person"].get(best, config["global_fallback"])
        accepted = duration_s >= frozen["minimum_speech_seconds"] and best_score >= threshold and margin >= config["margin"]
        decisions[rule] = best if accepted else None
    return {"best_person": best, "best_score": best_score, "second_person": second,
            "second_score": second_score, "margin": margin, "decisions": decisions}


def related(a, b):
    reasons = []
    if any(x["sha256"] == y["sha256"] and x["start_ms"] < y["end_ms"] and y["start_ms"] < x["end_ms"]
           for x in a["windows"] for y in b["windows"]):
        reasons.append("original_media_time_overlap")
    same_track = a["speaker_track_id"] == b["speaker_track_id"]
    same_cluster = bool(a.get("cluster_id")) and a.get("cluster_id") == b.get("cluster_id")
    gap = max(a["session_start_ms"], b["session_start_ms"]) - min(a["session_end_ms"], b["session_end_ms"])
    if a["session_id"] == b["session_id"] and (same_track or same_cluster) and gap <= (5000 if same_track else 2000):
        reasons.append("track_temporal_proximity" if same_track else "cluster_temporal_proximity")
    return reasons


def components(queries):
    parent = list(range(len(queries)))
    edges = []

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, a in enumerate(queries):
        for j in range(i):
            reasons = related(a, queries[j])
            if reasons:
                parent[root(i)] = root(j)
                edges.append({"a": a["query_id"], "b": queries[j]["query_id"], "reasons": reasons})
    groups = {}
    for i, query in enumerate(queries):
        groups.setdefault(root(i), []).append(query)
    return [(rows, [e for e in edges if e["a"] in {q["query_id"] for q in rows}]) for rows in groups.values()]


def wilson(success, total):
    if not total:
        return {"numerator": success, "denominator": total, "rate": None, "wilson95": None}
    z = 1.959963984540054
    p = success / total
    center = (p + z * z / (2 * total)) / (1 + z * z / total)
    delta = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / (1 + z * z / total)
    return {"numerator": success, "denominator": total, "rate": p, "wilson95": [center - delta, center + delta]}


def metrics(rows, known, self_id):
    result = {}
    for arm in ("legacy", "clean"):
        for rule in ("G", "P"):
            counts = Counter({k: 0 for k in ("known", "known_correct", "known_reject", "wrong_known", "unknown",
                                            "unknown_correct_reject", "unknown_false_accept", "self", "self_to_other")})
            for row in rows:
                truth = row["truth"]["primary_person_id"]
                predicted = row["prediction"][arm]["decisions"][rule]
                if truth in known:
                    counts["known"] += 1
                    counts["known_correct" if predicted == truth else "known_reject" if predicted is None else "wrong_known"] += 1
                else:
                    counts["unknown"] += 1
                    counts["unknown_false_accept" if predicted is not None else "unknown_correct_reject"] += 1
                if self_id is not None and truth == self_id:
                    counts["self"] += 1
                    counts["self_to_other"] += int(predicted is not None)
            result[f"{arm}_{rule}"] = dict(counts) | {
                "known_recall": wilson(counts["known_correct"], counts["known"]),
                "wrong_known_rate": wilson(counts["wrong_known"], counts["known"]),
                "unknown_FA": wilson(counts["unknown_false_accept"], counts["unknown"]),
                "self_to_other_rate": wilson(counts["self_to_other"], counts["self"]),
            }
    return result
