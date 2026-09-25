"""Queue leases and sample-set publication are committed with normal SQLite UOWs."""

import json
from .annotation_sample_plan import plans, PREPROCESS_VERSION
from .evidence_projection_repository import SqliteEvidenceProjectionRepository


class AnnotationSampleRepositoryMixin:
    def sample_plans(self, session_id, model, version):
        return plans(
            self.connection,
            SqliteEvidenceProjectionRepository(self.connection, now=lambda: ""),
            session_id,
            model,
            version,
        )

    def enqueue_samples(self, session_id):
        self.connection.execute(
            """INSERT INTO annotation_sample_queue(session_id) VALUES(?)
            ON CONFLICT(session_id) DO UPDATE SET generation=generation+1,status='queued',attempts=0,retry_at=0""",
            (session_id,),
        )

    def refresh_sample_model(self, model, version):
        key = json.dumps([model, version, PREPROCESS_VERSION])
        old = self.connection.execute(
            "SELECT model_key FROM annotation_sample_runtime WHERE singleton=1"
        ).fetchone()
        if old and old[0] == key:
            return
        self.connection.execute(
            "INSERT OR REPLACE INTO annotation_sample_runtime VALUES(1,?)", (key,)
        )
        for row in self.connection.execute("""SELECT DISTINCT u.session_id FROM annotation_facts f
            JOIN utterances u ON u.utterance_id=f.source_utterance_id""").fetchall():
            self.enqueue_samples(row[0])

    def claim_sample_job(self, now, token):
        self.connection.execute(
            """UPDATE annotation_sample_queue SET status='retryable',
            reason='worker_lease_expired',lease_until=0,token=NULL
            WHERE status='running' AND lease_until<? AND attempts>=3""",
            (now,),
        )
        row = self.connection.execute(
            """SELECT * FROM annotation_sample_queue
            WHERE ((status IN ('queued','retryable') AND retry_at<=? AND attempts<3)
            OR (status='running' AND lease_until<? AND attempts<3)) AND lease_until<?
            ORDER BY retry_at,session_id LIMIT 1""",
            (now, now, now),
        ).fetchone()
        if not row:
            return None
        self.connection.execute(
            """UPDATE annotation_sample_queue SET status='running',token=?,
            attempts=attempts+1,lease_until=? WHERE session_id=?""",
            (token, now + 120, row["session_id"]),
        )
        return dict(row)

    def finish_sample_job(self, session_id, generation, token, status, reason, now):
        self.connection.execute(
            """UPDATE annotation_sample_queue SET
            status=CASE WHEN generation=? THEN ? ELSE 'queued' END,
            reason=?,retry_at=?,lease_until=0,token=NULL WHERE session_id=? AND token=?""",
            (
                generation,
                status,
                reason[:1000],
                now + 30 if status == "retryable" else 0,
                session_id,
                token,
            ),
        )

    def sample_job(self, session_id):
        row = self.connection.execute(
            "SELECT * FROM annotation_sample_queue WHERE session_id=?", (session_id,)
        ).fetchone()
        return (
            dict(row)
            if row
            else {"status": "not_applicable", "reason": "no_person_fact"}
        )

    def sample_set_exists(self, key):
        return (
            self.connection.execute(
                "SELECT 1 FROM annotation_sample_sets WHERE sample_key=?", (key,)
            ).fetchone()
            is not None
        )

    def select_sample_set(self, plan):
        from .people_sample_eligibility import usable_voice_sample

        row = self.connection.execute(
            f"""SELECT s.sample_key FROM annotation_sample_sets s
            JOIN voice_prototypes p ON p.prototype_id=s.prototype_id
            WHERE s.sample_key=? AND {usable_voice_sample("p", require_current=False)}""",
            (plan.key,),
        ).fetchone()
        if row is None:
            return False
        # Selection never changes a review or copies its authorization. A
        # rejected/retracted result stays rejected/retracted, not pending.
        self.connection.execute(
            """UPDATE annotation_sample_sets SET current=(sample_key=?)
            WHERE session_id=? AND person_id=? AND model=? AND model_version=?""",
            (
                plan.key,
                plan.track.session_id,
                plan.person_id,
                *self.connection.execute(
                    "SELECT model,model_version FROM annotation_sample_sets WHERE sample_key=?",
                    (plan.key,),
                ).fetchone(),
            ),
        )
        return True

    def record_sample_set(self, plan, embedding, prototype_id, now):
        from allday_asr.v3.domain.ids import new_ulid
        from dataclasses import replace

        cluster_id = plan.cluster_id
        if cluster_id is None:
            # The source fact survives inactive ASR/manual tracks. A new review
            # carrier uses existing track/group tables without inventing facts.
            track_id, cluster_id = new_ulid(), new_ulid()
            self.connection.execute(
                """INSERT INTO speaker_tracks
                (speaker_track_id,session_id,run_id,label,source_artifact_id,created_at)
                SELECT ?,session_id,run_id,?,source_artifact_id,? FROM speaker_tracks
                WHERE speaker_track_id=?""",
                (track_id, "sample:" + plan.key, now, plan.track.speaker_track_id),
            )
            self.add_manual_group(
                cluster_id,
                (track_id,),
                "人工声音样本",
                "system:annotation-samples",
                new_ulid(),
                now,
            )
            self.label_cluster(
                cluster_id, plan.person_id, "system:annotation-samples", new_ulid(), now
            )
            embedding = replace(embedding, speaker_track_id=track_id)
        self._record_embedding_candidate(
            embedding, prototype_id, cluster_id, new_ulid(), now
        )
        self.connection.execute(
            """UPDATE annotation_sample_sets SET current=0
            WHERE session_id=? AND person_id=? AND model=? AND model_version=?""",
            (
                plan.track.session_id,
                plan.person_id,
                embedding.model,
                embedding.model_version,
            ),
        )
        self.connection.execute(
            "INSERT INTO annotation_sample_sets VALUES(?,?,?,?,?,?,?,?,1)",
            (
                plan.key,
                plan.track.session_id,
                plan.person_id,
                embedding.model,
                embedding.model_version,
                prototype_id,
                json.dumps(plan.fact_ids),
                json.dumps(plan.windows),
            ),
        )
