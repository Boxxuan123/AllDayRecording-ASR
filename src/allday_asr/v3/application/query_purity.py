"""Identity-agnostic window consistency, shadow use only.

No identity IDs are read. Wrong-primary can be internally pure and is explicitly
outside this probe's capability. Missing diarization/VAD signals stay missing.
"""

import numpy as np

from .purity_shadow import unit

FEATURES = {
    "mean_pairwise_cosine": "lower",
    "min_pairwise_cosine": "lower",
    "max_centroid_distance": "upper",
    "embedding_dispersion": "upper",
}


def query_features(windows):
    count = len(windows)
    result = {
        "number_of_windows": count,
        "total_range_duration_s": sum(
            (w["end_ms"] - w["start_ms"]) / 1000 for w in windows
        ),
        "total_speech_duration_s": None,
        "duration_basis": "audio range; VAD unavailable",
        "overlap_probability": None,
        "diarization_confidence": None,
    }
    for name in FEATURES:
        result[name] = None
    result.update(
        {
            "pairwise_cosines": [],
            "centroid_similarities": [],
            "leave_one_out_similarities": [],
            "mean_centroid_distance": None,
            "centroid_distance_variance": None,
            "temporal_gaps_ms": [],
            "speaker_track_consistent": None,
            "cluster_consistent": None,
        }
    )
    for field, output in (
        ("speaker_track_id", "speaker_track_consistent"),
        ("cluster_id", "cluster_consistent"),
    ):
        values = [w.get(field) for w in windows]
        if count and all(values):
            result[output] = len(set(values)) == 1
    grouped = {}
    for window in windows:
        grouped.setdefault(window.get("media_id"), []).append(window)
    for group in grouped.values():
        ordered = sorted(group, key=lambda w: w["start_ms"])
        result["temporal_gaps_ms"].extend(
            b["start_ms"] - a["end_ms"]
            for a, b in zip(ordered, ordered[1:], strict=False)
        )
    if not count:
        return result
    vectors = np.stack([unit(w["embedding"]) for w in windows])
    mean = vectors.mean(axis=0)
    if np.linalg.norm(mean) < 1e-12:
        result["invalid_centroid"] = True
        return result
    center = unit(mean)
    similarity = np.clip(vectors @ center, -1, 1)
    distances = 1 - similarity
    result.update(
        {
            "centroid_similarities": similarity.tolist(),
            "max_centroid_distance": float(distances.max()),
            "mean_centroid_distance": float(distances.mean()),
            "centroid_distance_variance": float(distances.var()),
            "embedding_dispersion": float(
                np.mean(np.sum((vectors - mean) ** 2, axis=1))
            ),
        }
    )
    if count >= 2:
        pairwise = np.clip(vectors @ vectors.T, -1, 1)[np.triu_indices(count, 1)]
        loo = []
        for vector in vectors:
            other = vectors.sum(axis=0) - vector
            loo.append(
                float(vector @ unit(other)) if np.linalg.norm(other) > 1e-12 else None
            )
        result.update(
            {
                "pairwise_cosines": pairwise.tolist(),
                "mean_pairwise_cosine": float(pairwise.mean()),
                "min_pairwise_cosine": float(pairwise.min()),
                "leave_one_out_similarities": loo,
            }
        )
    return result


def fit_clean_thresholds(rows, *, minimum_duration_s=6):
    """Predeclared 95% clean retention target, not optimized on held-out errors."""
    result = {}
    for feature, direction in FEATURES.items():
        values = [
            r["features"][feature]
            for r in rows
            if r["gold"] == "clean_single"
            and r["features"]["number_of_windows"] >= 2
            and r["features"]["total_range_duration_s"] >= minimum_duration_s
            and r["features"][feature] is not None
        ]
        if values:
            result[feature] = float(
                np.quantile(values, 0.05 if direction == "lower" else 0.95)
            )
    return result


def probe(features, thresholds, *, rule="min_pairwise_cosine", minimum_duration_s=6):
    if (
        features["number_of_windows"] < 2
        or features["total_range_duration_s"] < minimum_duration_s
        or features.get("invalid_centroid")
    ):
        return {"status": "INSUFFICIENT", "reason": "insufficient_window_evidence"}
    names = (
        [rule]
        if rule != "combination"
        else ["min_pairwise_cosine", "max_centroid_distance"]
    )
    if any(name not in thresholds or features.get(name) is None for name in names):
        return {"status": "INSUFFICIENT", "reason": "uncalibrated_feature"}
    violations = [
        name
        for name in names
        if (
            features[name] < thresholds[name]
            if FEATURES[name] == "lower"
            else features[name] > thresholds[name]
        )
    ]
    return {
        "status": "SUSPICIOUS" if violations else "PASS",
        "reason": "window_inconsistency" if violations else "internally_consistent",
        "violations": violations,
        "shadow_only": True,
    }


def grouped_validation(rows, *, minimum_duration_s=6):
    """Leave one source/session component out; each source is counted once."""
    groups = sorted({r["group"] for r in rows})
    rules = list(FEATURES) + ["combination"]
    outcomes = {rule: [] for rule in rules}
    folds = []
    for group in groups:
        train = [r for r in rows if r["group"] != group]
        test = [r for r in rows if r["group"] == group]
        thresholds = fit_clean_thresholds(train, minimum_duration_s=minimum_duration_s)
        folds.append(
            {
                "held_out_group": group,
                "train_groups": sorted({r["group"] for r in train}),
                "training_clean": sum(r["gold"] == "clean_single" for r in train),
                "test_sources": len(test),
                "thresholds": thresholds,
            }
        )
        for rule in rules:
            outcomes[rule].extend(
                {
                    "source_key": r["source_key"],
                    "gold": r["gold"],
                    "group": group,
                    **probe(
                        r["features"],
                        thresholds,
                        rule=rule,
                        minimum_duration_s=minimum_duration_s,
                    ),
                }
                for r in test
            )
    return {
        "method": "leave-one-session/media-component-out",
        "groups": len(groups),
        "folds": folds,
        "outcomes": outcomes,
        "primary_rule": "min_pairwise_cosine",
        "clean_retention_target": 0.95,
        "note": "No rule selection on held-out errors. Full-fit deployment remains shadow only.",
    }
