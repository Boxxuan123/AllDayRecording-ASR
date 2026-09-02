from __future__ import annotations
import sqlite3
from allday_asr.v3.domain.processing import (
    SpeakerTrack,
    Utterance,
)
from .repository_clock import Clock

from .processing_repository_codec import _utterance, _datetime, _json


class SqliteEvidenceProjectionRepository:
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
            "SELECT label FROM speaker_tracks WHERE speaker_track_id = ?",
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
            UPDATE utterances SET text = ?, speaker_track_id = ?, identity = ?,
                revision = revision + 1,
                status = 'active', updated_at = ?
            WHERE utterance_id = ? AND revision = ?
            """,
            (text, speaker_track_id, identity, now, utterance_id, expected_revision),
        )
        if cursor.rowcount != 1:
            raise ValueError("utterance revision conflict")
        return self.get_utterance(utterance_id)
