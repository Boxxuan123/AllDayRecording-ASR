"""Read-only companion invariance and timing with frozen private waveforms.

Prepare before changing the adapter, run before/after once each. No enrollment,
calibration, policy or identity writes; normalized target bytes are reused verbatim.
"""

import argparse
import hashlib
import json
import time
from pathlib import Path

import numpy as np
import soundfile as sf

from short_self_dataset import assets, digest, integrity, read, readonly, write
from allday_asr.v3.adapters.audio.tools import extract_clip
from allday_asr.v3.adapters.files import ContentAddressedStore
from allday_asr.v3.adapters.models.funasr import FunASRBackend
from allday_asr.v3.adapters.self_identity import CalibratedSelfIdentityMatcher
from allday_asr.v3.adapters.speaker_embeddings import FunASRSpeakerEmbeddingProvider
from allday_asr.v3.adapters.sqlite.people_repository import SqlitePeopleRepository
from allday_asr.v3.domain.people import SpeakerEmbedding
from allday_asr.v3.ports.speaker_embeddings import SpeakerClipInput, SpeakerTrackInput


def waveform_sha(samples):
    return hashlib.sha256(np.asarray(samples, dtype=np.float32).tobytes()).hexdigest()


class CaptureBackend:
    def extract_speaker_embeddings(self, samples, *, batch_size=8):
        self.samples = samples
        vectors = np.ones((len(samples), 192), dtype=np.float32)
        return vectors


def prepare(state, previous, output):
    if (output / "manifest.json").exists():
        raise ValueError("invariance corpus already frozen")
    previous_events = read(previous / "split-manifest.json")["events"]
    evidence = [
        e
        for part in ("development", "holdout", "reference")
        for e in read(previous / f"{part}-evidence.json")["events"]
    ]
    selected = [
        e for e in evidence if e.get("anchor") or e.get("historical_false_accept")
    ]
    strong = max(
        (
            e
            for e in evidence
            if e["truth"] == "non-self" and 2000 <= e["duration_ms"] < 3000
        ),
        key=lambda e: e["full"].get("score", -1),
    )
    selected.append(strong | {"audit_label": "strong_short_negative"})
    for truth in ("self", "non-self"):
        for bin_name in ("2-3", "3-4", "4-6", "6+"):
            chosen = next(
                e
                for e in previous_events
                if e["truth"] == truth and e["bin"] == bin_name and e["safe_ownership"]
            )
            if chosen["event_id"] not in {e["event_id"] for e in selected}:
                selected.append(chosen | {"audit_label": f"{truth}:{bin_name}"})
    tracks = tuple(
        SpeakerTrackInput(
            str(i),
            e["session_id"],
            (
                SpeakerClipInput(
                    e["media_id"],
                    e["storage_key"],
                    e["source_start_ms"],
                    e["source_end_ms"],
                    e["utterance_id"],
                ),
            ),
        )
        for i, e in enumerate(selected)
    )
    capture = CaptureBackend()
    provider = FunASRSpeakerEmbeddingProvider(
        ContentAddressedStore(state / "audio"),
        backend_factory=lambda: capture,
        temp_root=output / "clips",
    )
    provider.embed(tracks)
    waves = list(capture.samples)
    snapshot = assets(state)
    # Source files are preserved. Exact original reference-window timestamps are
    # absent from the NPZ metadata, so these are explicitly source probes only.
    for i, source in enumerate(snapshot["metadata"]["source_files"]):
        destination = output / "source-probes" / f"{i}.wav"
        extract_clip(
            Path(source["path"]),
            destination,
            0,
            min(8000, int(source["duration_seconds"] * 1000)),
        )
        wave, rate = sf.read(destination, dtype="float32")
        if rate != 16000:
            raise ValueError("reference source probe sample rate changed")
        waves.append(wave)
        selected.append(
            {
                "audit_label": f"reference_source_probe:{i}",
                "truth": "self",
                "source_path": source["path"],
                "source_sha256": source["sha256"],
            }
        )
    output.mkdir(parents=True, exist_ok=True)
    np.savez(output / "waveforms.npz", **{str(i): w for i, w in enumerate(waves)})
    c = readonly(state)
    write(output / "before/fingerprints.json", integrity(c))
    c.close()
    write(output / "before/assets.json", snapshot)
    write(
        output / "protected-previous-files.json",
        {
            str(p): digest(p)
            for p in previous.glob("*.json")
            if any(s in p.name for s in ("manifest", "rules", "shadow", "evidence"))
        },
    )
    manifest = {
        "format": "speaker-batch-invariance-v1",
        "previous_split_sha256": digest(previous / "split-manifest.json"),
        "waveforms_sha256": digest(output / "waveforms.npz"),
        "events": [
            e
            | {
                "index": i,
                "waveform_sha256": waveform_sha(w),
                "samples": len(w),
                "duration_ms": len(w) / 16,
            }
            for i, (e, w) in enumerate(zip(selected, waves, strict=True))
        ],
    }
    write(output / "manifest.json", manifest)
    (output / "manifest.sha256").write_text(
        digest(output / "manifest.json"), encoding="ascii"
    )
    return {
        "targets": len(waves),
        "original_source_probes": len(snapshot["metadata"]["source_files"]),
    }


def frozen(output, state):
    if digest(output / "manifest.json") != (output / "manifest.sha256").read_text(
        encoding="ascii"
    ):
        raise ValueError("invariance manifest changed")
    manifest = read(output / "manifest.json")
    if digest(output / "waveforms.npz") != manifest["waveforms_sha256"]:
        raise ValueError("target audio bytes changed")
    if assets(state) != read(output / "before/assets.json"):
        raise ValueError("model/reference/policy/enrollment assets changed")
    if any(
        digest(p) != sha
        for p, sha in read(output / "protected-previous-files.json").items()
    ):
        raise ValueError("old frozen predictions/rules/evidence changed")
    with np.load(output / "waveforms.npz", allow_pickle=False) as payload:
        waves = [payload[str(i)].copy() for i in range(len(manifest["events"]))]
    return manifest, waves


def legacy_extract(backend, inputs, batch_size):
    """Replay the prior API composition offline; never changes the adapter."""
    backend.ensure_speaker_loaded()
    result = backend._speaker_model.generate(
        input=[np.asarray(x, dtype=np.float32) for x in inputs], batch_size=batch_size
    )
    rows = []
    for item in result:
        value = item["spk_embedding"]
        if hasattr(value, "detach"):
            value = value.detach().cpu().numpy()
        rows.append(np.atleast_2d(np.asarray(value, dtype=np.float32)))
    vectors = np.concatenate(rows)
    if len(vectors) != len(inputs):
        raise ValueError("legacy embedding count mismatch")
    return vectors


def run(state, output, phase, device):
    path = output / f"{phase}.json"
    if path.exists():
        raise ValueError("before/after already recorded")
    manifest, waves = frozen(output, state)
    c = readonly(state)
    others = [
        (pid, np.asarray(v) / np.linalg.norm(v))
        for pid, v in SqlitePeopleRepository(c).person_vectors(
            "FunASR/CAM++", "v1-local"
        )
    ]
    matcher = CalibratedSelfIdentityMatcher(state)
    backend = FunASRBackend(device=device)
    backend.ensure_speaker_loaded()
    import funasr.models.campplus.model as implementation

    original = implementation.extract_feature
    trace = []

    def observe(audio):
        features, lengths, times = original(audio)
        trace.append(
            {
                "valid_feature_lengths": [int(x) for x in lengths],
                "padded_feature_frames": int(features.shape[1]),
                "waveform_samples": [int(x) for x in times],
                "first_valid_feature_sha256": waveform_sha(
                    features[0, : lengths[0]].cpu().numpy()
                ),
            }
        )
        return features, lengths, times

    implementation.extract_feature = observe
    short = min(waves, key=len)[:16000]
    long = max(waves, key=len)
    records = []
    try:
        for e, target in zip(manifest["events"], waves, strict=True):
            variants = [
                ("b1", 1, [target]),
                ("b2_short", 2, [target, short]),
                ("b2_long", 2, [target, long]),
                ("b4_mixed", 4, [target, short, long, long[:48000]]),
                (
                    "b8_mixed",
                    8,
                    [
                        target,
                        short,
                        long,
                        long[:48000],
                        short,
                        long[:80000],
                        target,
                        long,
                    ],
                ),
            ]
            baseline = None
            baseline_score = None
            baseline_decision = None
            for label, size, inputs in variants:
                trace.clear()
                started = time.perf_counter()
                vectors = (
                    legacy_extract(backend, inputs, size)
                    if phase == "before-cuda"
                    else backend.extract_speaker_embeddings(inputs, batch_size=size)
                )
                elapsed = time.perf_counter() - started
                raw = vectors[0]
                unit = raw / np.linalg.norm(raw)
                embedding = SpeakerEmbedding(
                    str(e["index"]),
                    "FunASR/CAM++",
                    "v1-local",
                    tuple(float(x) for x in unit),
                    (),
                    min(1, len(target) / 192000),
                )
                decision = matcher.match(embedding).evidence
                if baseline is None:
                    baseline, baseline_score, baseline_decision = (
                        unit.copy(),
                        decision["score"],
                        decision["decision"],
                    )
                records.append(
                    {
                        "index": e["index"],
                        "label": label,
                        "requested_batch": size,
                        "target_sha256": waveform_sha(target),
                        "duration_ms": len(target) / 16,
                        "padded_duration_ms": max(trace[0]["waveform_samples"]) / 16,
                        "features": list(trace),
                        "embedding": raw.tolist(),
                        "embedding_norm": float(np.linalg.norm(raw)),
                        "cosine_to_b1": float(np.clip(np.dot(baseline, unit), -1, 1)),
                        "self_score": decision["score"],
                        "best_other_score": max(
                            (float(np.dot(v, unit)) for _, v in others), default=None
                        ),
                        "decision": decision["decision"],
                        "score_delta": decision["score"] - baseline_score,
                        "decision_flip": decision["decision"] != baseline_decision,
                        "self_threshold_flip": (
                            decision["score"] >= matcher.status()["self_threshold"]
                        )
                        != (baseline_score >= matcher.status()["self_threshold"]),
                        "elapsed_seconds": elapsed,
                    }
                )
            print(f"{phase} targets {e['index'] + 1}/{len(waves)}", flush=True)
    finally:
        implementation.extract_feature = original
    report = {
        "phase": phase,
        "device": backend.device,
        "manifest_sha256": digest(output / "manifest.json"),
        "metrics": {
            "max_embedding_drift": max(1 - r["cosine_to_b1"] for r in records),
            "max_abs_score_delta": max(abs(r["score_delta"]) for r in records),
            "decision_flip_count": sum(r["decision_flip"] for r in records),
            "cross_self_threshold_flips": sum(
                r["self_threshold_flip"] for r in records
            ),
        },
        "records": records,
        "production_writes": False,
    }
    assert integrity(c) == read(output / "before/fingerprints.json")
    c.close()
    frozen(output, state)
    write(path, report)
    return report["metrics"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "phase", choices=("prepare", "before", "after", "before-cuda", "after-cuda")
    )
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--previous", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--device", default="cpu")
    args = parser.parse_args()
    state, output = args.state_dir.resolve(), args.output.resolve()
    if output.is_relative_to(state):
        parser.error("audit output must be outside production state")
    result = (
        prepare(state, args.previous, output)
        if args.phase == "prepare"
        else run(state, output, args.phase, args.device)
    )
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
