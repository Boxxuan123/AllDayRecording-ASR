"""Within-event evidence via the existing frozen CAM++ provider and matcher."""

from dataclasses import asdict
from pathlib import Path

import numpy as np

from short_self_dataset import digest, integrity, read, readonly, write
from allday_asr.v3.adapters.files import ContentAddressedStore
from allday_asr.v3.adapters.models.funasr import FunASRBackend
from allday_asr.v3.adapters.self_identity import CalibratedSelfIdentityMatcher
from allday_asr.v3.adapters.speaker_embeddings import FunASRSpeakerEmbeddingProvider
from allday_asr.v3.adapters.sqlite.people_repository import SqlitePeopleRepository
from allday_asr.v3.ports.speaker_embeddings import SpeakerClipInput, SpeakerTrackInput


def crop_ranges(start, end):
    duration = end - start
    if start < 0 or duration < 1000:
        raise ValueError("invalid or malformed audit clip")
    length = 2000 if duration >= 2000 else max(1000, duration * 3 // 4)
    offsets = {0, (duration - length) // 2, duration - length}
    if 2000 <= duration < 4000:
        offsets.update(range(0, duration - length + 1, 500))
    return [(start + offset, start + offset + length) for offset in sorted(offsets)]


def statistics(values):
    if not values:
        return {"count": 0}
    a = np.asarray(values, dtype=float)
    return {
        "count": len(values),
        "min": float(a.min()),
        "max": float(a.max()),
        "mean": float(a.mean()),
        "median": float(np.median(a)),
        "std": float(a.std()),
        "range": float(a.max() - a.min()),
    }


def distribution(values):
    result = statistics(values)
    if len(values) < 10:
        result["raw_scores"] = values
    else:
        result.update(
            p10=float(np.quantile(values, 0.1)), p90=float(np.quantile(values, 0.9))
        )
    return result


class SingleBatchBackend:
    """Keep padding context fixed for stability; no model/preprocessor copy."""

    def __init__(self, backend):
        self.backend = backend

    def extract_speaker_embeddings(self, samples, *, batch_size=8):
        return self.backend.extract_speaker_embeddings(samples, batch_size=1)


def features(event, embeddings, tracks, matcher, references, other_vectors):
    decisions = []
    vectors = []
    for label, track in tracks:
        embedding = embeddings.get(track.speaker_track_id)
        duration = sum(c.source_end_ms - c.source_start_ms for c in track.clips)
        evidence = (
            matcher.match(embedding).evidence
            if embedding
            else {"decision": "unknown", "reason": "embedding_unavailable"}
        )
        record = {
            "label": label,
            "input": asdict(track),
            "duration_ms": duration,
            "quality_score": embedding.quality_score if embedding else None,
            **evidence,
        }
        if embedding:
            vector = np.array(embedding.vector, dtype=np.float32)
            vector /= np.linalg.norm(vector)
            if label == "full":
                scores = references @ vector
                record["reference_consensus"] = {
                    "scores": scores.tolist(),
                    "summary": distribution(scores.tolist()),
                    "support_fraction_at_frozen_threshold": float(
                        np.mean(scores >= matcher.status()["self_threshold"])
                    ),
                }
                other = sorted(
                    [
                        {"person_id": pid, "cosine": float(np.dot(v, vector))}
                        for pid, v in other_vectors
                    ],
                    key=lambda x: -x["cosine"],
                )
                record["nearest_other"] = other[0] if other else None
                record["self_minus_best_other"] = (
                    evidence.get("score", 0) - other[0]["cosine"] if other else None
                )
                record["margin_limit"] = (
                    "diagnostic weighted-self-score minus actual-library max cosine; not a calibrated production margin"
                )
            if label == "full" or label.startswith("crop:"):
                vectors.append(vector)
        decisions.append(record)
    full = next(d for d in decisions if d["label"] == "full")
    crops = [d for d in decisions if d["label"].startswith("crop:")]
    pairwise = [
        float(np.dot(a, b)) for i, a in enumerate(vectors) for b in vectors[i + 1 :]
    ]
    independent = [d for d in decisions if d["label"].startswith("independent:")]
    two = (
        "self"
        if len(independent) >= 2
        and all(
            d["duration_ms"] >= 2000 and d["decision"] == "self" for d in independent
        )
        else "unknown"
    )
    return dict(event) | {
        "full": full,
        "crops": crops,
        "crop_score_statistics": statistics(
            [d["score"] for d in crops if "score" in d]
        ),
        "pairwise_cosine": statistics(pairwise),
        "independent_windows": independent,
        "current_two_window": two,
        "crop_independence": "overlapping perturbations of ONE event, not independent evidence or test samples",
        "snr": "NOT AVAILABLE; quality_score is duration/12000, not measured acoustic purity",
    }


def validate_manifest(output):
    path = Path(output) / "split-manifest.json"
    expected = (
        (Path(output) / "split-manifest.sha256").read_text(encoding="ascii").strip()
    )
    if digest(path) != expected:
        raise ValueError("frozen split manifest changed")
    return read(path)


def score_partition(state, output, partition):
    output = Path(output)
    state = Path(state)
    if partition not in ("development", "holdout", "reference"):
        raise ValueError("unknown partition")
    target = output / f"{partition}-evidence.json"
    if target.exists():
        raise ValueError("partition was already evaluated; no repeat/overwrite")
    if partition == "holdout" and not (output / "candidate-rules.json").exists():
        raise ValueError("freeze development-only rules before inspecting holdout")
    manifest = validate_manifest(output)
    events = [e for e in manifest["events"] if e["split"] == partition]
    c = readonly(state)
    if integrity(c) != read(output / "before/fingerprints.json"):
        raise ValueError("production identity/Blind fingerprints changed since prepare")
    # Validate all source references before any embedding or output write.
    for e in events:
        asset = c.execute(
            """SELECT a.duration_ms,a.media_id,r.storage_key FROM audio_assets a
            JOIN audio_replicas r USING(asset_id) WHERE a.media_id=? AND r.state='available' AND r.storage_key=?""",
            (e["media_id"], e["storage_key"]),
        ).fetchone()
        if asset is None or not (
            0 <= e["source_start_ms"] < e["source_end_ms"] <= asset["duration_ms"]
        ):
            raise ValueError("malformed or unavailable audio clip")
    matcher = CalibratedSelfIdentityMatcher(state)
    loaded, reason = matcher._load()
    if loaded is None:
        raise ValueError("frozen matcher unavailable: " + reason)
    _, references, _ = loaded
    others = []
    for pid, vector in SqlitePeopleRepository(c).person_vectors(
        "FunASR/CAM++", "v1-local"
    ):
        v = np.asarray(vector, dtype=np.float32)
        if np.isfinite(v).all() and np.linalg.norm(v) > 0:
            others.append((pid, v / np.linalg.norm(v)))
    all_tracks = []
    event_tracks = {}
    for e in events:
        start, end = e["source_start_ms"], e["source_end_ms"]
        ranges = [("full", start, end)] + [
            (f"crop:{i}", a, b) for i, (a, b) in enumerate(crop_ranges(start, end))
        ]
        if end - start >= 4000:
            mid = (start + end) // 2
            ranges.extend([("independent:0", start, mid), ("independent:1", mid, end)])
        pairs = []
        for label, a, b in ranges:
            track = SpeakerTrackInput(
                e["event_id"] + "|" + label,
                e["session_id"],
                (
                    SpeakerClipInput(
                        e["media_id"], e["storage_key"], a, b, e["utterance_id"]
                    ),
                ),
            )
            pairs.append((label, track))
            all_tracks.append(track)
        event_tracks[e["event_id"]] = pairs
    write(
        output / f"{partition}-inputs.json",
        {
            "manifest_sha256": digest(output / "split-manifest.json"),
            "inference_protocol": "existing provider/preprocessor, existing CAM++; batch_size=1 to prevent cross-event padding from masquerading as crop instability",
            "tracks": [asdict(t) for t in all_tracks],
        },
    )
    backend = SingleBatchBackend(FunASRBackend(device="cpu"))
    provider = FunASRSpeakerEmbeddingProvider(
        ContentAddressedStore(state / "audio"),
        backend_factory=lambda: backend,
        temp_root=output / "clips",
    )
    print(
        f"SCORING {partition}: {len(events)} events, {len(all_tracks)} within-event windows",
        flush=True,
    )
    # Bounded chunks, same single-waveform model protocol in every partition.
    embeddings = {}
    for offset in range(0, len(all_tracks), 64):
        batch = provider.embed(tuple(all_tracks[offset : offset + 64]))
        embeddings.update({e.speaker_track_id: e for e in batch})
        print(
            f"{partition} windows {min(offset + 64, len(all_tracks))}/{len(all_tracks)}",
            flush=True,
        )
    result = [
        features(
            e, embeddings, event_tracks[e["event_id"]], matcher, references, others
        )
        for e in events
    ]
    assert integrity(c) == read(output / "before/fingerprints.json")
    c.close()
    report = {
        "manifest_sha256": digest(output / "split-manifest.json"),
        "partition": partition,
        "matcher": matcher.status(),
        "inference_batch_size": 1,
        "real_other_prototype_count": len(others),
        "event_count": len(result),
        "events": result,
    }
    write(target, report)
    return {
        "partition": partition,
        "events": len(result),
        "windows": len(all_tracks),
        "other_prototypes": len(others),
    }
