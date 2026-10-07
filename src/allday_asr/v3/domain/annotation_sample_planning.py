"""Pure bounded selection of annotation evidence and audio windows."""

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass

from allday_asr.v3.domain.sound_kind import sound_uses
from allday_asr.v3.ports.speaker_embeddings import SpeakerClipInput, SpeakerTrackInput

PREPROCESS_VERSION = "annotation-union-16k-v2-longest5x8s"


def intersect(a, b):
    return (a["media_id"] == b["media_id"] and
            a["start_ms"] < b["end_ms"] and b["start_ms"] < a["end_ms"])


class Intervals:
    """Balanced interval tree. Query visits matching intervals and boundary paths."""

    def __init__(self, rows):
        self.center = 0
        self.left = self.right = None
        self.starts = self.ends = ()
        if not rows:
            return
        self.center = sorted((a + b) / 2 for a, b, _ in rows)[len(rows) // 2]
        left, right, middle = [], [], []
        for row in rows:
            (
                left
                if row[1] <= self.center
                else right
                if row[0] > self.center
                else middle
            ).append(row)
        self.starts = sorted(middle, key=lambda r: r[0])
        self.ends = sorted(middle, key=lambda r: r[1], reverse=True)
        self.left = Intervals(left) if left else None
        self.right = Intervals(right) if right else None

    def query(self, start, end):
        if end <= self.center:
            for a, _b, value in self.starts:
                if a >= end:
                    break
                yield value
            if self.left:
                yield from self.left.query(start, end)
        elif start >= self.center:
            for _a, b, value in self.ends:
                if b <= start:
                    break
                yield value
            if self.right:
                yield from self.right.query(start, end)
        else:
            for _a, _b, value in self.starts:
                yield value
            if self.left:
                yield from self.left.query(start, end)
            if self.right:
                yield from self.right.query(start, end)


class PlanInputs:
    def __init__(self, snapshot):
        self.facts = {}
        self.source = []
        self.projections = defaultdict(list)
        self.links = {}
        self.replicas = defaultdict(list)
        audio = defaultdict(list)
        for fid, media, start, end in snapshot.audio:
            audio[fid].append(dict(media_id=media, start_ms=start, end_ms=end))
        intervals = defaultdict(list)
        for fid, dimension, state, value, payload, session in snapshot.facts:
            fact = dict(
                fact_id=fid,
                dimension=dimension,
                state=state,
                value=json.loads(value),
                payload=json.loads(payload),
                audio=audio[fid],
            )
            self.facts[fid] = fact
            if (
                session == snapshot.session_id
                and dimension == "person"
                and state == "active"
            ):
                self.source.append(fact)
            if state in ("active", "conflict", "revoked"):
                for a in audio[fid]:
                    intervals[(a["media_id"], dimension)].append(
                        (a["start_ms"], a["end_ms"], fid)
                    )
        self.intervals = {key: Intervals(rows) for key, rows in intervals.items()}
        for (raw,) in snapshot.projections:
            evidence = json.loads(raw)
            if not evidence.get("annotation_outdated"):
                for fid in evidence.get("annotation_fact_ids", {}).get("person", []):
                    self.projections[fid].append(evidence)
        for track, person, cluster in snapshot.links:
            self.links.setdefault((track, person), cluster)
        for rank, (media, start, end, key) in enumerate(snapshot.replicas):
            self.replicas[media].append((start, end, (rank, start, end, key)))
        self.replica_intervals = {
            media: Intervals(rows) for media, rows in self.replicas.items()
        }

    def overlaps(self, anchor, dimension):
        tree = self.intervals.get((anchor["media_id"], dimension))
        return (
            [
                self.facts[fid]
                for fid in sorted(set(tree.query(anchor["start_ms"], anchor["end_ms"])))
            ]
            if tree
            else []
        )

    def replica(self, anchor):
        tree = self.replica_intervals.get(anchor["media_id"])
        candidates = tree.query(anchor["start_ms"], anchor["end_ms"]) if tree else ()
        selected = min(
            (
                row
                for row in candidates
                if row[1] <= anchor["start_ms"] and row[2] >= anchor["end_ms"]
            ),
            default=None,
        )
        return selected[3] if selected else None


@dataclass(frozen=True)
class SamplePlan:
    key: str
    person_id: str
    cluster_id: str | None
    track: SpeakerTrackInput
    fact_ids: tuple[str, ...]
    windows: tuple[dict, ...]



def compute_plans(snapshot, model, version):
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
