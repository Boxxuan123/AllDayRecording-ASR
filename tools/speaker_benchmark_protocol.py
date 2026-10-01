"""Frozen future benchmark: development-only fit, independent evaluation once.

This module never changes production policies/assets. A model must prove companion
invariance on development controls before it can touch evaluation audio.
"""

import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Protocol

import numpy as np

from short_self_dataset import digest, read, write
from allday_asr.v3.adapters.models.funasr import (
    FunASRBackend,
    SPEAKER_INFERENCE_PROTOCOL,
    _cached_model_or_id,
)

PROTOCOL_VERSION = "speaker-benchmark-v1"
THRESHOLD_METHOD = "development-max-negative-plus-0.02-v1"
FORBIDDEN_FLAGS = (
    "was_enrollment",
    "was_calibration",
    "was_profile_learning",
    "was_development",
    "was_blind",
    "was_previous_diagnostic",
)


class SpeakerEmbeddingBackend(Protocol):
    def embed(self, audio: np.ndarray, valid_length: int) -> np.ndarray: ...
    def embed_batch(
        self, inputs: list[tuple[np.ndarray, int]], *, batch_size: int
    ) -> np.ndarray: ...
    def metadata(self) -> dict: ...


class CAMPlusBenchmarkBackend:
    def __init__(self, device="cpu"):
        self.backend = FunASRBackend(device=device)

    def metadata(self):
        model = Path(_cached_model_or_id("cam++"))
        if not model.is_dir():
            raise ValueError("NO NEW MODEL DOWNLOAD: CAM++ cache required")
        hashes = {
            p.name: digest(p)
            for p in model.iterdir()
            if p.suffix in (".bin", ".json", ".yaml")
        }
        return {
            "model": "FunASR/CAM++",
            "version": self.backend.package_version,
            "dimension": 192,
            "model_files": hashes,
            "model_hash": json_hash(hashes),
            "inference_protocol": SPEAKER_INFERENCE_PROTOCOL,
            "preprocessing": "mono float32 16000 Hz; existing provider clip decoding",
        }

    def embed(self, audio, valid_length):
        return self.embed_batch([(audio, valid_length)], batch_size=1)[0]

    def embed_batch(self, inputs, *, batch_size):
        self.metadata()  # Refuse any implicit model download.
        waves = [valid_audio(a, n) for a, n in inputs]
        vectors = self.backend.extract_speaker_embeddings(waves, batch_size=batch_size)
        return normalize(vectors)


def json_hash(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def valid_audio(audio, length):
    audio = np.asarray(audio, dtype=np.float32)
    if (
        audio.ndim != 1
        or not 0 < length <= len(audio)
        or not np.isfinite(audio[:length]).all()
    ):
        raise ValueError("invalid mono waveform/effective length")
    return audio[:length]


def normalize(vectors):
    values = np.atleast_2d(np.asarray(vectors, dtype=np.float32))
    norms = np.linalg.norm(values, axis=1, keepdims=True)
    if not np.isfinite(values).all() or (norms <= 0).any():
        raise ValueError("invalid speaker embedding")
    return values / norms


def trusted_event(e, role):
    r = e.get("reservation") or {}
    try:
        stamps = [
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            for value in (
                r["reserved_at"],
                r["first_prediction_at"],
                e["truth_reviewed_at"],
            )
        ]
        if any(s.tzinfo is None for s in stamps) or not (
            stamps[0] < stamps[1] and stamps[0] < stamps[2]
        ):
            return False
    except (KeyError, TypeError, ValueError):
        return False
    windows = e.get("source_windows", [])
    if not windows or not e.get("event_id") or not e.get("truth_fact_ids"):
        return False
    if len(e.get("normalized_audio_sha256", "")) != 64 or any(
        not w.get("media_id")
        or len(w.get("sha256", "")) != 64
        or not isinstance(w.get("start_ms"), int)
        or not isinstance(w.get("end_ms"), int)
        or not 0 <= w["start_ms"] < w["end_ms"]
        for w in windows
    ):
        return False
    if sum(w["end_ms"] - w["start_ms"] for w in windows) != e.get("duration_ms"):
        return False
    flags = (
        FORBIDDEN_FLAGS
        if role == "independent_evaluation"
        else tuple(f for f in FORBIDDEN_FLAGS if f != "was_development")
    )
    return (
        e.get("session_role") == role
        and r.get("research_role") == role
        and e.get("date") == r.get("capture_date_utc")
        and e.get("truth") in ("self", "non-self")
        and e.get("purity") == "clean_single"
        and e.get("mapping_complete") is True
        and e.get("has_overlap") is False
        and all(e.get(f) is False for f in flags)
        and e.get("exclusions") == []
    )


def independent_gate(events):
    eligible = [e for e in events if trusted_event(e, "independent_evaluation")]
    short = [e for e in eligible if 2000 <= e["duration_ms"] < 4000]
    counts = Counter(e["truth"] for e in short)
    sessions = {e["session_id"] for e in short}
    dates = {e["date"] for e in short}
    reasons = []
    if len(eligible) != len(events):
        reasons.append("ineligible_or_contaminated_events")
    if len(short) < 30:
        reasons.append("minimum_30_short_events")
    if any(counts[t] < 10 for t in ("self", "non-self")):
        reasons.append("minimum_10_per_truth")
    if len(sessions) < 3 or len(dates) < 3:
        reasons.append("minimum_3_sessions_and_dates")
    for t in ("self", "non-self"):
        selected = [e for e in short if e["truth"] == t]
        if (
            len({e["session_id"] for e in selected}) < 2
            or len({e["date"] for e in selected}) < 2
        ):
            reasons.append("per_truth_session_date_diversity:" + t)
        if any(
            not any(a <= e["duration_ms"] < b for e in selected)
            for a, b in ((2000, 3000), (3000, 4000))
        ):
            reasons.append("missing_short_bin:" + t)
        if not any(
            e["truth"] == t and 4000 <= e["duration_ms"] < 6000 for e in eligible
        ) or not any(e["truth"] == t and e["duration_ms"] >= 6000 for e in eligible):
            reasons.append("missing_control_bin:" + t)
    if short and max(Counter(e["session_id"] for e in short).values()) > len(short) / 2:
        reasons.append("session_concentration_exceeds_half")
    if short and max(counts.values()) > len(short) * 0.8:
        reasons.append("truth_imbalance_exceeds_80_percent")
    return {
        "passed": not reasons,
        "reasons": reasons,
        "short_events": len(short),
        "self": counts["self"],
        "negative": counts["non-self"],
        "sessions": len(sessions),
        "dates": len(dates),
        "meaning": "minimum study-start gate; never a production promotion gate",
    }


def score(vector, references, centroid):
    vector = normalize(vector)[0]
    refs = normalize(references)
    center = normalize(centroid)[0]
    similarities = refs @ vector
    return float(
        0.7 * np.dot(center, vector)
        + 0.3 * np.sort(similarities)[-min(3, len(refs)) :].mean()
    )


def invariance_gate(backend, controls, references, centroid, threshold):
    if len(controls) < 2:
        raise ValueError("development invariance controls required")
    records = []
    shortest = min(controls, key=lambda item: item[1])
    longest = max(controls, key=lambda item: item[1])
    for audio, length in controls:
        base = backend.embed(audio, length)
        base_score = score(base, references, centroid)
        for size, variant, companions in (
            (2, "short", [shortest]),
            (2, "long", [longest]),
            (4, "mixed", [shortest, longest, shortest]),
            (8, "mixed", [shortest, longest] * 3 + [shortest]),
        ):
            inputs = [(audio, length)] + companions
            current = backend.embed_batch(inputs, batch_size=size)[0]
            cosine = float(
                np.clip(np.dot(normalize(base)[0], normalize(current)[0]), -1, 1)
            )
            current_score = score(current, references, centroid)
            records.append(
                {
                    "batch_size": size,
                    "companions": variant,
                    "embedding_drift": 1 - cosine,
                    "score_delta": current_score - base_score,
                    "decision_flip": (current_score >= threshold)
                    != (base_score >= threshold),
                }
            )
    passed = all(
        r["embedding_drift"] <= 1e-5
        and abs(r["score_delta"]) <= 1e-5
        and not r["decision_flip"]
        for r in records
    )
    return {
        "passed": passed,
        "status": "VALID" if passed else "ENGINEERING INVALID FOR BENCHMARK",
        "records": records,
    }


def metrics(events, scores, threshold):
    rows = [(e, s) for e, s in zip(events, scores, strict=True)]

    def group(values):
        pos = [s for e, s in values if e["truth"] == "self"]
        neg = [s for e, s in values if e["truth"] == "non-self"]
        accepted = sum(s >= threshold for s in pos)
        false = sum(s >= threshold for s in neg)
        return {
            "self_events": len(pos),
            "negative_events": len(neg),
            "self_accepted": accepted,
            "self_unknown": len(pos) - accepted,
            "negative_self": false,
            "negative_rejected": len(neg) - false,
            "coverage": accepted / len(pos) if pos else None,
            "false_accept": false / len(neg) if neg else None,
            "false_reject": (len(pos) - accepted) / len(pos) if pos else None,
            "unknown_coverage": (len(pos) + len(neg) - accepted - false) / len(values)
            if values
            else None,
        }

    return {
        "all": group(rows),
        "bins": {
            name: group([(e, s) for e, s in rows if a <= e["duration_ms"] < b])
            for name, a, b in (
                ("2-3", 2000, 3000),
                ("3-4", 3000, 4000),
                ("4-6", 4000, 6000),
                ("6+", 6000, float("inf")),
            )
        },
    }


def checked_audio(e, load_audio):
    audio, length = load_audio(e)
    wave = valid_audio(audio, length)
    if abs(length / 16 - e["duration_ms"]) > 1:
        raise ValueError("effective audio length differs from frozen event")
    if hashlib.sha256(wave.tobytes()).hexdigest() != e["normalized_audio_sha256"]:
        raise ValueError("source/preprocessing differs from frozen manifest")
    return wave, length


def fit_and_freeze(root, development, evaluation, models, load_audio):
    root = Path(root)
    if (root / "plan.json").exists() or (root / "evaluation-started.json").exists():
        raise ValueError("study already frozen/evaluated")
    if not models:
        raise ValueError("a frozen model list is required")
    all_events = development + evaluation
    if len({e.get("event_id") for e in all_events}) != len(all_events):
        raise ValueError("duplicate event identity")
    by_source = {}
    for e in all_events:
        for w in e.get("source_windows", []):
            prior = by_source.setdefault(w["sha256"], [])
            if any(w["start_ms"] < end and w["end_ms"] > start for start, end in prior):
                raise ValueError("duplicate/overlapping source audio")
            prior.append((w["start_ms"], w["end_ms"]))
    gate = independent_gate(evaluation)
    if not gate["passed"]:
        raise ValueError("INDEPENDENT DATA GATE NOT MET: " + ",".join(gate["reasons"]))
    if not development or not all(trusted_event(e, "development") for e in development):
        raise ValueError("development-only calibration required")
    if {e["session_id"] for e in development} & {
        e["session_id"] for e in evaluation
    } or {e["date"] for e in development} & {e["date"] for e in evaluation}:
        raise ValueError("session/date overlap")
    root.mkdir(parents=True, exist_ok=True)
    model_plans = {}
    controls = [checked_audio(e, load_audio) for e in development]
    for name, (backend, references, centroid) in models.items():
        metadata = backend.metadata()
        dev_scores = [
            score(backend.embed(a, n), references, centroid) for a, n in controls
        ]
        negatives = [
            s
            for e, s in zip(development, dev_scores, strict=True)
            if e["truth"] == "non-self"
        ]
        if not negatives or not any(e["truth"] == "self" for e in development):
            raise ValueError("development truth classes incomplete")
        threshold = max(negatives) + 0.02
        prerequisite = invariance_gate(
            backend, controls, references, centroid, threshold
        )
        model_plans[name] = {
            "metadata": metadata,
            "threshold": threshold,
            "threshold_method": THRESHOLD_METHOD,
            "reference_hash": json_hash(
                [np.asarray(references).tolist(), np.asarray(centroid).tolist()]
            ),
            "development_metrics": metrics(development, dev_scores, threshold)
            if prerequisite["passed"]
            else None,
            "invariance": prerequisite,
        }
    plan = {
        "protocol": PROTOCOL_VERSION,
        "production_enabled": False,
        "development_hash": json_hash(development),
        "evaluation_hash": json_hash(evaluation),
        "gate": gate,
        "models": model_plans,
        "model_selection": "development only; model list frozen before evaluation; no evaluation ranking/winner selection",
    }
    write(root / "development-manifest.json", development)
    write(root / "evaluation-manifest.json", evaluation)
    write(root / "plan.json", plan)
    (root / "plan.sha256").write_text(digest(root / "plan.json"), encoding="ascii")
    return plan


def evaluate_once(root, models, load_audio):
    root = Path(root)
    if digest(root / "plan.json") != (root / "plan.sha256").read_text(encoding="ascii"):
        raise ValueError("frozen plan changed")
    plan = read(root / "plan.json")
    if plan["protocol"] != PROTOCOL_VERSION or plan["production_enabled"] is not False:
        raise ValueError("unsupported benchmark protocol")
    events = read(root / "evaluation-manifest.json")
    if (
        json_hash(events) != plan["evaluation_hash"]
        or json_hash(read(root / "development-manifest.json"))
        != plan["development_hash"]
    ):
        raise ValueError("frozen source/truth/split changed")
    if not independent_gate(events)["passed"]:
        raise ValueError("independent gate no longer satisfied")
    if set(models) != set(plan["models"]):
        raise ValueError("model list changed after freezing")
    for name, (backend, refs, center) in models.items():
        expected = plan["models"][name]
        if expected["threshold_method"] != THRESHOLD_METHOD:
            raise ValueError("threshold method changed")
        if (
            backend.metadata() != expected["metadata"]
            or json_hash([np.asarray(refs).tolist(), np.asarray(center).tolist()])
            != expected["reference_hash"]
        ):
            raise ValueError("model/reference snapshot changed")
        if not expected["invariance"]["passed"]:
            raise ValueError("ENGINEERING INVALID FOR BENCHMARK: " + name)
    # Create before reading evaluation audio: a failed run consumes this study.
    with (root / "evaluation-started.json").open("x", encoding="utf-8") as f:
        json.dump({"plan_sha256": digest(root / "plan.json")}, f)
    audio = [checked_audio(e, load_audio) for e in events]
    results = {
        name: metrics(
            events,
            [score(backend.embed(a, n), refs, center) for a, n in audio],
            plan["models"][name]["threshold"],
        )
        for name, (backend, refs, center) in models.items()
    }
    report = {
        "protocol": PROTOCOL_VERSION,
        "models": results,
        "production_enabled": False,
        "plan_sha256": digest(root / "plan.json"),
        "evaluation_counted_once": True,
    }
    write(root / "evaluation.json", report)
    return report
