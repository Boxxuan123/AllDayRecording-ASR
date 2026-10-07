"""Short coherent reads; detached bounded inputs and interval-indexed planning."""

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
    dataset_role: str = "learning"


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

    reservation = connection.execute(
        'SELECT revision,(SELECT dataset_role FROM session_dataset_roles WHERE session_id=?) '
        'FROM annotation_input_revision WHERE singleton=1', (session_id,)
    ).fetchone()
    revision = reservation[0]
    # Include all facts touching media used by this session, even another session's facts.
    scope = """SELECT DISTINCT a.media_id FROM annotation_fact_audio a
        JOIN annotation_facts f ON f.fact_id=a.fact_id
        JOIN utterances u ON u.utterance_id=f.source_utterance_id WHERE u.session_id=?"""
    facts = read(
        f"""SELECT f.fact_id,f.dimension,f.state,f.value_json,f.payload_json,u.session_id
        FROM annotation_facts f LEFT JOIN utterances u ON u.utterance_id=f.source_utterance_id
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
    return SampleSnapshot(revision, session_id, facts, audio, projections, links, replicas,
                          reservation[1] or "unassigned")
