from __future__ import annotations
import sqlite3
from .annotation_fact_repository import AnnotationFactMixin
from allday_asr.v3.domain.processing import (
    SpeakerTrack,
    Utterance,
)
from .repository_clock import Clock

from .processing_repository_codec import _utterance, _datetime, _json


class SqliteEvidenceProjectionRepository(AnnotationFactMixin):
    def __init__(self, connection: sqlite3.Connection, *, now: Clock) -> None:
        self.connection = connection
        self.now = now

    def add_speaker_track(self, track: SpeakerTrack) -> bool:
        cursor = self.connection.execute(
            """
            INSERT INTO speaker_tracks (
                speaker_track_id, session_id, run_id, label, source_artifact_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING
            """,
            (
                track.speaker_track_id,
                track.session_id,
                track.run_id,
                track.label,
                track.source_artifact_id,
                _datetime(track.created_at),
            ),
        )
        return cursor.rowcount == 1

    def add_utterance(self, utterance: Utterance) -> bool:
        utterance = self._restore_annotation(utterance)
        cursor = self.connection.execute(
            """
            INSERT INTO utterances (
                utterance_id, session_id, run_id, source_artifact_id,
                speaker_track_id, original_speaker_track_id, ordinal,
                start_ms, end_ms, start_at, end_at, text, original_text,
                identity, original_identity, identity_evidence_json,
                evidence_json, revision, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                utterance.utterance_id,
                utterance.session_id,
                utterance.run_id,
                utterance.source_artifact_id,
                utterance.speaker_track_id,
                utterance.original_speaker_track_id,
                utterance.ordinal,
                utterance.start_ms,
                utterance.end_ms,
                _datetime(utterance.start_at),
                _datetime(utterance.end_at),
                utterance.text,
                utterance.original_text,
                utterance.identity.value,
                utterance.original_identity.value,
                _json(utterance.identity_evidence),
                _json(utterance.evidence),
                utterance.revision,
                utterance.status,
                _datetime(utterance.created_at),
                _datetime(utterance.updated_at),
            ),
        )
        return cursor.rowcount == 1

    def audio_evidence(self, utterance: Utterance) -> list[dict]:
        rows = self.connection.execute("""SELECT a.media_id, s.session_start_ms,
            s.session_end_ms, s.source_start_ms FROM capture_segments s
            JOIN audio_assets a ON a.asset_id = s.asset_id WHERE s.session_id = ?
            AND s.session_start_ms < ? AND s.session_end_ms > ? ORDER BY s.sequence, s.segment_id""",
            (utterance.session_id, utterance.end_ms, utterance.start_ms)).fetchall()
        return [{"media_id": r["media_id"],
                 "start_ms": r["source_start_ms"] + max(utterance.start_ms, r["session_start_ms"]) - r["session_start_ms"],
                 "end_ms": r["source_start_ms"] + min(utterance.end_ms, r["session_end_ms"]) - r["session_start_ms"]}
                for r in rows]

    def get_utterance(self, utterance_id: str) -> Utterance:
        row = self.connection.execute(
            "SELECT * FROM utterances WHERE utterance_id = ?", (utterance_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"utterance does not exist: {utterance_id}")
        return _utterance(row)

    def speaker_label(self, speaker_track_id: str | None) -> str | None:
        if speaker_track_id is None:
            return None
        row = self.connection.execute(
            """SELECT CASE WHEN st.label LIKE 'manual:%' THEN COALESCE(p.display_name, st.label) ELSE st.label END AS label FROM speaker_tracks st
            LEFT JOIN speaker_cluster_memberships m ON m.speaker_track_id = st.speaker_track_id AND m.state = 'active'
            LEFT JOIN person_cluster_links l ON l.cluster_id = m.cluster_id AND l.status = 'active'
            LEFT JOIN persons p ON p.person_id = l.person_id
            WHERE st.speaker_track_id = ? LIMIT 1""",
            (speaker_track_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"speaker track does not exist: {speaker_track_id}")
        return str(row["label"])

    def revise_utterance(
        self,
        utterance_id: str,
        expected_revision: int,
        text: str,
        speaker_track_id: str | None,
        identity: str,
        evidence: dict | None = None,
    ) -> Utterance:
        before = self.get_utterance(utterance_id)
        if speaker_track_id is not None:
            row = self.connection.execute(
                "SELECT session_id FROM speaker_tracks WHERE speaker_track_id = ?",
                (speaker_track_id,),
            ).fetchone()
            if row is None or str(row["session_id"]) != before.session_id:
                raise ValueError("speaker track does not belong to utterance session")
        now = self.now()
        cursor = self.connection.execute(
            """
            UPDATE utterances SET text = ?, speaker_track_id = ?, identity = ?, evidence_json = ?,
                revision = revision + 1,
                status = 'active', updated_at = ?
            WHERE utterance_id = ? AND revision = ?
            """,
            (text, speaker_track_id, identity, _json(before.evidence if evidence is None else evidence),
             now, utterance_id, expected_revision),
        )
        if cursor.rowcount != 1:
            raise ValueError("utterance revision conflict")
        return self.get_utterance(utterance_id)
