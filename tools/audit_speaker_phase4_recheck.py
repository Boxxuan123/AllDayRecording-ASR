"""Engineering replay only: frozen historical rules, no holdout fitting."""

import argparse
import os
import subprocess
import threading
import time
from pathlib import Path

import numpy as np

from audit_speaker_batch_invariance import CaptureBackend, frozen, legacy_extract
from short_self_dataset import digest, read, write
import short_self_evidence as evidence
from short_self_rules import evaluate, summarize
from short_self_regression import replay
from allday_asr.v3.adapters.files import ContentAddressedStore
from allday_asr.v3.adapters.models.funasr import FunASRBackend
from allday_asr.v3.adapters.speaker_embeddings import FunASRSpeakerEmbeddingProvider
from allday_asr.v3.ports.speaker_embeddings import SpeakerClipInput, SpeakerTrackInput


def recheck(state, previous, output, old_manifest, old_replay):
    frozen(output, state)
    target = output / "recheck"
    if target.exists():
        raise ValueError("engineering recheck already started; frozen once")
    target.mkdir()
    for name in (
        "split-manifest.json",
        "split-manifest.sha256",
        "candidate-rules.json",
    ):
        (target / name).write_bytes((previous / name).read_bytes())
    for name in ("fingerprints.json", "assets.json"):
        p = target / "before" / name
        p.parent.mkdir(exist_ok=True)
        p.write_bytes((output / "before" / name).read_bytes())
    (target / "dataset-summary.json").write_bytes(
        (previous / "dataset-summary.json").read_bytes()
    )
    # Real fixed production adapter, no audit-only batch=1 wrapper.
    original = evidence.SingleBatchBackend
    evidence.SingleBatchBackend = lambda backend: backend
    try:
        for partition in ("development", "holdout", "reference"):
            evidence.score_partition(state, target, partition)
            evaluate(target, partition)
        fixed = replay(
            state, target, old_manifest, old_replay, verify_prior_baseline=False
        )
        summarize(target)
    finally:
        evidence.SingleBatchBackend = original
    deltas = []
    for partition in ("development", "holdout", "reference"):
        old = {
            e["event_id"]: e
            for e in read(previous / f"{partition}-evidence.json")["events"]
        }
        for e in read(target / f"{partition}-evidence.json")["events"]:
            prior = old[e["event_id"]]
            deltas.append(
                {
                    "event_id": e["event_id"],
                    "truth": e["truth"],
                    "bin": e["bin"],
                    "prior_controlled_b1": prior["full"].get("score"),
                    "after_production_adapter": e["full"].get("score"),
                    "decision_flip": prior["full"]["decision"] != e["full"]["decision"],
                }
            )
    result = {
        "events": len(deltas),
        "comparison": deltas,
        "fixed_regression": fixed,
        "old_rules_sha256": digest(previous / "candidate-rules.json"),
        "new_rules_sha256": digest(target / "candidate-rules.json"),
        "max_score_delta": max(
            abs(e["prior_controlled_b1"] - e["after_production_adapter"])
            for e in deltas
        ),
        "decision_flip_count": sum(e["decision_flip"] for e in deltas),
        "scope": "historical engineering replay; not a new independent evaluation or refit",
    }
    write(output / "recheck-summary.json", result)
    frozen(output, state)
    return result["fixed_regression"]


class UtilizationMonitor:
    def __init__(self, gpu):
        import psutil

        self.psutil = psutil
        self.process = psutil.Process()
        self.gpu = gpu
        self.samples = []
        self.stop = threading.Event()

    def observe(self):
        self.process.cpu_percent()
        while not self.stop.wait(0.25):
            item = {
                "process_cpu_percent": self.process.cpu_percent(),
                "host_cpu_percent": self.psutil.cpu_percent(),
            }
            if self.gpu:
                try:
                    raw = subprocess.check_output(
                        [
                            "nvidia-smi",
                            "--query-gpu=utilization.gpu,utilization.memory,memory.used",
                            "--format=csv,noheader,nounits",
                        ],
                        text=True,
                        creationflags=subprocess.CREATE_NO_WINDOW
                        if os.name == "nt"
                        else 0,
                    ).splitlines()[0]
                    util, memory, used = [float(x.strip()) for x in raw.split(",")]
                    item.update(
                        gpu_util_percent=util,
                        gpu_memory_util_percent=memory,
                        gpu_memory_used_mb=used,
                    )
                except (OSError, subprocess.SubprocessError, ValueError):
                    item["gpu_monitor_unavailable"] = True
            self.samples.append(item)

    def __enter__(self):
        self.thread = threading.Thread(target=self.observe, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join(timeout=5)


def performance(state, previous, output):
    if (output / "performance.json").exists():
        raise ValueError("performance already recorded")
    frozen(output, state)
    events = read(previous / "split-manifest.json")["events"]
    capture = CaptureBackend()
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
        for i, e in enumerate(events)
    )
    FunASRSpeakerEmbeddingProvider(
        ContentAddressedStore(state / "audio"),
        backend_factory=lambda: capture,
        temp_root=output / "perf-clips",
    ).embed(tracks)
    waves = capture.samples
    import torch

    audio_seconds = sum(len(w) for w in waves) / 16000
    records = []
    for device in ["cpu", "cuda:0"] if torch.cuda.is_available() else ["cpu"]:
        backend = FunASRBackend(device=device)
        backend.ensure_speaker_loaded()
        backend.extract_speaker_embeddings(waves[:2])
        for label in ("legacy_b8", "fixed_b1"):
            if device.startswith("cuda"):
                torch.cuda.synchronize()
            with UtilizationMonitor(device.startswith("cuda")) as monitor:
                started = time.perf_counter()
                vectors = (
                    legacy_extract(backend, waves, 8)
                    if label == "legacy_b8"
                    else backend.extract_speaker_embeddings(waves, batch_size=8)
                )
                if device.startswith("cuda"):
                    torch.cuda.synchronize()
                elapsed = time.perf_counter() - started
            record = {
                "device": device,
                "protocol": label,
                "events": len(waves),
                "audio_seconds": audio_seconds,
                "elapsed_seconds": elapsed,
                "clips_per_second": len(waves) / elapsed,
                "audio_seconds_per_second": audio_seconds / elapsed,
                "real_time_factor": elapsed / audio_seconds,
                "latency_ms_per_clip_amortized": elapsed * 1000 / len(waves),
                "utilization_samples": monitor.samples,
            }
            records.append(record)
            np.savez(
                output / f"perf-{device.replace(':', '_')}-{label}.npz",
                embeddings=vectors,
            )
            print(
                {k: v for k, v in record.items() if k != "utilization_samples"},
                flush=True,
            )
        del backend
        if device.startswith("cuda"):
            torch.cuda.empty_cache()
    write(
        output / "performance.json",
        {
            "workload": "237 real historical verification clips; model warmed; includes unchanged fbank/model/normalization, excludes one-time audio decoding",
            "gpu_utilization_scope": "device-wide, may include other processes; process CPU percent can exceed 100 on multicore",
            "records": records,
        },
    )
    frozen(output, state)
    return {
        "events": len(waves),
        "audio_seconds": audio_seconds,
        "measurements": len(records),
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("phase", choices=("recheck", "performance"))
    p.add_argument("--state-dir", type=Path, required=True)
    p.add_argument("--previous", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--fixed-manifest", type=Path)
    p.add_argument("--fixed-replay", type=Path)
    a = p.parse_args()
    state, output = a.state_dir.resolve(), a.output.resolve()
    if output.is_relative_to(state):
        p.error("private output must be outside production state")
    result = (
        recheck(state, a.previous, output, a.fixed_manifest, a.fixed_replay)
        if a.phase == "recheck"
        else performance(state, a.previous, output)
    )
    print(result, flush=True)


if __name__ == "__main__":
    main()
