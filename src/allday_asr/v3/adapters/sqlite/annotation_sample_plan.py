"""Bounded original-audio selection from the existing effective fact ledger."""

import hashlib
import json
from dataclasses import dataclass

from allday_asr.v3.domain.sound_kind import sound_uses
from allday_asr.v3.ports.speaker_embeddings import SpeakerClipInput, SpeakerTrackInput
from .annotation_fact_repository import intersect

PREPROCESS_VERSION = "annotation-union-16k-v2-longest5x8s"


@dataclass(frozen=True)
class SamplePlan:
    key: str
    person_id: str
    cluster_id: str | None
    track: SpeakerTrackInput
    fact_ids: tuple[str, ...]
    windows: tuple[dict, ...]


def plans(connection, evidence, session_id, model, version):
    # Compatibility entry point; worker reads a snapshot before invoking compute_plans.
    from .annotation_sample_snapshot import load_snapshot

    return compute_plans(load_snapshot(connection, session_id), model, version)


def compute_plans(snapshot, model, version):
    from .annotation_sample_snapshot import PlanInputs

    if snapshot.dataset_role != "learning":
        return (), ["dataset_role_excluded"]

    inputs = PlanInputs(snapshot)
    session_id = snapshot.session_id
    groups, reasons = {}, set()
    for fact in inputs.source:
        if not fact["value"]:
            continue
        track_id = fact["payload"].get("speaker_track_id")
        cluster = inputs.links.get((track_id, fact["value"]))
        for anchor in fact["audio"]:
            overlaps = inputs.overlaps(anchor, "person")
            if any(
                f["state"] != "active" or f["value"] != fact["value"] for f in overlaps
            ):
                reasons.add("identity_conflict")
                continue
            sounds = inputs.overlaps(anchor, "sound")
            if any(f["state"] != "active" for f in sounds):
                reasons.add("sound_conflict")
                continue
            if any(
                not sound_uses({"sound_kind": f["value"]})["sample_candidate_allowed"]
                for f in sounds
            ):
                reasons.add("source_excluded")
                continue
            current = inputs.projections.get(fact["fact_id"], ())
            if current and any(
                not sound_uses(e)["sample_candidate_allowed"] for e in current
            ):
                reasons.add("source_excluded")
                continue
            replica = inputs.replica(anchor)
            if replica is None:
                reasons.add("missing_audio")
                continue
            groups.setdefault(fact["value"], []).append(
                {
                    **anchor,
                    "storage_key": replica,
                    "track_id": track_id,
                    "cluster_id": cluster,
                    "facts": {fact["fact_id"], *(s["fact_id"] for s in sounds)},
                }
            )
    result = []
    for person_id, anchors in sorted(groups.items()):
        union = []
        for a in sorted(
            anchors, key=lambda a: (a["media_id"], a["start_ms"], a["end_ms"])
        ):
            if (
                union
                and union[-1]["media_id"] == a["media_id"]
                and union[-1]["end_ms"] >= a["start_ms"]
            ):
                union[-1]["end_ms"] = max(union[-1]["end_ms"], a["end_ms"])
            else:
                union.append(
                    {k: a[k] for k in ("media_id", "start_ms", "end_ms", "storage_key")}
                )
        options = []
        for a in union:
            start = a["start_ms"]
            # At most five full chunks from any contiguous range can win.
            while start < min(a["end_ms"], a["start_ms"] + 40000):
                end = min(start + 8000, a["end_ms"])
                if end - start >= 800:
                    options.append({**a, "start_ms": start, "end_ms": end})
                start = end
        selected = sorted(
            sorted(
                options,
                key=lambda w: (
                    -(w["end_ms"] - w["start_ms"]),
                    w["media_id"],
                    w["start_ms"],
                    w["end_ms"],
                ),
            )[:5],
            key=lambda w: (w["media_id"], w["start_ms"], w["end_ms"]),
        )
        if not selected:
            reasons.add("insufficient_evidence")
            continue
        contributors = [a for a in anchors if any(intersect(a, w) for w in selected)]
        facts = tuple(sorted({fid for a in contributors for fid in a["facts"]}))
        windows = tuple(
            {k: w[k] for k in ("media_id", "start_ms", "end_ms")} for w in selected
        )
        key = hashlib.sha256(
            json.dumps(
                [
                    person_id,
                    session_id,
                    facts,
                    windows,
                    model,
                    version,
                    PREPROCESS_VERSION,
                ],
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        representative = contributors[0]
        track = SpeakerTrackInput(
            representative["track_id"],
            session_id,
            tuple(
                SpeakerClipInput(
                    w["media_id"], w["storage_key"], w["start_ms"], w["end_ms"], None
                )
                for w in selected
            ),
        )
        result.append(
            SamplePlan(
                key, person_id, representative["cluster_id"], track, facts, windows
            )
        )
    return tuple(result), sorted(reasons)
