"""Support ordered, active utterance pages for one processing run."""

SQL = """
CREATE INDEX idx_utterances_active_run_time
ON utterances(run_id, start_ms, end_ms, utterance_id)
WHERE status = 'active';
"""
