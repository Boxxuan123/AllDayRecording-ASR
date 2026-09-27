"""Short coherent reads; detached bounded inputs and interval-indexed planning."""

import json
from collections import defaultdict
from dataclasses import dataclass

MAX_ROWS = 100_000
MAX_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class SampleSnapshot:
    revision: int
    session_id: str
    facts: tuple
    audio: tuple
    projections: tuple
    links: tuple
    replicas: tuple


def load_snapshot(connection, session_id):
    total = 0
    size = 0

    def read(sql, args=()):
        nonlocal total, size
        result = []
        for row in connection.execute(sql, args):
            value = tuple(row)
            total += 1
            size += sum(len(v.encode("utf-8")) for v in value if isinstance(v, str))
            if total > MAX_ROWS or size > MAX_BYTES:
                raise ValueError("annotation snapshot resource budget exceeded")
            result.append(value)
        return tuple(result)

    revision = connection.execute(
        "SELECT revision FROM annotation_input_revision WHERE singleton=1"
    ).fetchone()[0]
    # Include all facts touching media used by this session, even another session's facts.
    scope = """SELECT DISTINCT a.media_id FROM annotation_fact_audio a
        JOIN annotation_facts f ON f.fact_id=a.fact_id
        JOIN utterances u ON u.utterance_id=f.source_utterance_id WHERE u.session_id=?"""
    facts = read(
        f"""SELECT f.fact_id,f.dimension,f.state,f.value_json,f.payload_json,u.session_id
        FROM annotation_facts f JOIN utterances u ON u.utterance_id=f.source_utterance_id
        WHERE f.fact_id IN (SELECT fact_id FROM annotation_fact_audio WHERE media_id IN ({scope}))
        ORDER BY f.fact_id""",
        (session_id,),
    )
    audio = read(
        f"""SELECT fact_id,media_id,start_ms,end_ms FROM annotation_fact_audio
        WHERE media_id IN ({scope}) ORDER BY fact_id,ordinal""",
        (session_id,),
    )
    projections = read(
        "SELECT evidence_json FROM utterances WHERE session_id=? AND status='active'",
        (session_id,),
    )
    links = read(
        """SELECT m.speaker_track_id,l.person_id,m.cluster_id FROM speaker_cluster_memberships m
        JOIN person_cluster_links l ON l.cluster_id=m.cluster_id AND l.status='active'
        JOIN speaker_tracks t ON t.speaker_track_id=m.speaker_track_id
        WHERE m.state='active' AND t.session_id=? ORDER BY m.rowid,l.rowid""",
        (session_id,),
    )
    replicas = read(
        """SELECT a.media_id,s.source_start_ms,s.source_start_ms+s.session_end_ms-s.session_start_ms,r.storage_key
        FROM capture_segments s JOIN audio_assets a ON a.asset_id=s.asset_id
        JOIN audio_replicas r ON r.replica_id=s.replica_id
        WHERE s.session_id=? AND r.state='available' ORDER BY s.sequence,s.segment_id""",
        (session_id,),
    )
    return SampleSnapshot(
        revision, session_id, facts, audio, projections, links, replicas
    )


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
