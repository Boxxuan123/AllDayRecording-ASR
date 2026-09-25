"""Reminder source review is independent of user task lifecycle and scheduling."""


class ReminderSourceMixin:
    def invalidate_source_candidates(self, utterance_id: str, now: str) -> None:
        rows = self.connection.execute(
            """SELECT c.candidate_id,c.proposal_id
            FROM reminder_candidates c JOIN structured_change_proposals p ON p.proposal_id=c.proposal_id
            JOIN json_each(p.evidence_utterance_ids_json) evidence
            WHERE c.status='pending_confirmation' AND evidence.value=?""",
            (utterance_id,),
        ).fetchall()
        for row in rows:
            self.resolve_candidate(
                row["candidate_id"],
                "conflict",
                None,
                "source_semantics_changed",
                now,
                "source-review",
            )
            self.connection.execute(
                """UPDATE structured_change_proposals SET status='rejected',
                resolved_at=?,resolved_by='source-review',resolution_reason='source_semantics_changed'
                WHERE proposal_id=? AND status='pending' """,
                (now, row["proposal_id"]),
            )

    def has_user_task_for_evidence(self, utterance_ids) -> bool:
        # Exact references plus capture mapping cover reprojected utterance IDs.
        for uid in utterance_ids:
            found = self.connection.execute(
                """SELECT 1 FROM reminder_candidates c
                JOIN structured_change_proposals p ON p.proposal_id=c.proposal_id
                JOIN json_each(p.evidence_utterance_ids_json) source
                JOIN utterances old ON old.utterance_id=source.value
                JOIN utterances current ON current.utterance_id=?
                WHERE c.status IN ('confirmed','modified') AND (
                  old.utterance_id=current.utterance_id OR
                  (old.session_id=current.session_id AND old.start_ms<current.end_ms AND old.end_ms>current.start_ms))
                LIMIT 1""",
                (uid,),
            ).fetchone()
            if found:
                return True
        return False
