"""Indexes for page reads that filter active utterances by speaker track."""

SQL = """
CREATE INDEX idx_utterances_active_speaker_track
ON utterances(speaker_track_id)
WHERE status = 'active';
"""
