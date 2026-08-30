from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from typing import Any

from .runs import RunRepository
from .sessions import SessionRepository
from .types import Clock, ConnectionFactory


class IdentityRepository:
    def __init__(
        self,
        connect: ConnectionFactory,
        *,
        runs: RunRepository,
        sessions: SessionRepository,
        now: Clock,
    ) -> None:
        self.connect = connect
        self._runs = runs
        self._sessions = sessions
        self._now = now

    def upsert_v2d1_candidate_review(
        self, run_id: int, values: dict[str, Any]
    ) -> sqlite3.Row:
        run = self._runs.get_processing_run(run_id)
        if str(run["run_kind"]) != "quality_diarization_v2d1":
            raise ValueError("可能语音审核只适用于 V2-D.1 run")
        if str(run["status"]) != "completed":
            raise ValueError("只能审核已完成的 V2-D.1 run")
        status = str(values["status"])
        if status not in {"confirmed_speech", "rejected", "uncertain"}:
            raise ValueError("可能语音审核状态无效")
        start_ms = int(values["session_start_ms"])
        end_ms = int(values["session_end_ms"])
        if start_ms < 0 or end_ms <= start_ms:
            raise ValueError("可能语音审核时间范围无效")
        now = self._now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO v2d1_candidate_reviews (
                    run_id, candidate_id, session_start_ms, session_end_ms,
                    status, note, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, candidate_id) DO UPDATE SET
                    status = excluded.status,
                    note = excluded.note,
                    updated_at = excluded.updated_at
                """,
                (
                    run_id,
                    str(values["candidate_id"]),
                    start_ms,
                    end_ms,
                    status,
                    values.get("note"),
                    now,
                    now,
                ),
            )
            return connection.execute(
                """
                SELECT * FROM v2d1_candidate_reviews
                WHERE run_id = ? AND candidate_id = ?
                """,
                (run_id, str(values["candidate_id"])),
            ).fetchone()

    def list_v2d1_candidate_reviews(self, run_id: int) -> list[sqlite3.Row]:
        run = self._runs.get_processing_run(run_id)
        if str(run["run_kind"]) != "quality_diarization_v2d1":
            raise ValueError("可能语音审核只适用于 V2-D.1 run")
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM v2d1_candidate_reviews
                    WHERE run_id = ? ORDER BY session_start_ms, id
                    """,
                    (run_id,),
                )
            )

    def upsert_v2d1_review_completion(
        self,
        run_id: int,
        *,
        candidate_count: int,
        reviewed_count: int,
    ) -> sqlite3.Row:
        run = self._runs.get_processing_run(run_id)
        if str(run["run_kind"]) != "quality_diarization_v2d1":
            raise ValueError("可能语音审核只适用于 V2-D.1 run")
        if str(run["status"]) != "completed":
            raise ValueError("只能完成已结束的 V2-D.1 run 审核")
        candidate_count = int(candidate_count)
        reviewed_count = int(reviewed_count)
        if candidate_count < 0 or reviewed_count != candidate_count:
            raise ValueError("仍有可能语音候选尚未审核")
        now = self._now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO v2d1_review_completions (
                    run_id, candidate_count, reviewed_count,
                    completed_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    candidate_count = excluded.candidate_count,
                    reviewed_count = excluded.reviewed_count,
                    completed_at = excluded.completed_at,
                    updated_at = excluded.updated_at
                """,
                (run_id, candidate_count, reviewed_count, now, now),
            )
            return connection.execute(
                "SELECT * FROM v2d1_review_completions WHERE run_id = ?",
                (run_id,),
            ).fetchone()

    def get_v2d1_review_completion(self, run_id: int) -> sqlite3.Row | None:
        run = self._runs.get_processing_run(run_id)
        if str(run["run_kind"]) != "quality_diarization_v2d1":
            raise ValueError("可能语音审核只适用于 V2-D.1 run")
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM v2d1_review_completions WHERE run_id = ?",
                (run_id,),
            ).fetchone()

    def upsert_v2d1_identity_label(
        self, run_id: int, candidate_id: str, identity_label: str
    ) -> sqlite3.Row:
        run = self._runs.get_processing_run(run_id)
        if str(run["run_kind"]) != "quality_diarization_v2d1":
            raise ValueError("人物标签只适用于 V2-D.1 可能语音候选")
        identity_label = identity_label.strip()
        if not identity_label:
            raise ValueError("人物标签不能为空")
        with self.connect() as connection:
            review = connection.execute(
                """
                SELECT status FROM v2d1_candidate_reviews
                WHERE run_id = ? AND candidate_id = ?
                """,
                (run_id, candidate_id),
            ).fetchone()
            if review is None:
                raise KeyError("可能语音候选还没有人工判断")
            if str(review["status"]) != "confirmed_speech":
                raise ValueError("只有确认是语音的候选才能标注人物")
            now = self._now()
            connection.execute(
                """
                INSERT INTO v2d1_identity_labels (
                    run_id, candidate_id, identity_label, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(run_id, candidate_id) DO UPDATE SET
                    identity_label = excluded.identity_label,
                    updated_at = excluded.updated_at
                """,
                (run_id, candidate_id, identity_label, now, now),
            )
            return connection.execute(
                """
                SELECT * FROM v2d1_identity_labels
                WHERE run_id = ? AND candidate_id = ?
                """,
                (run_id, candidate_id),
            ).fetchone()

    def delete_v2d1_identity_label(self, run_id: int, candidate_id: str) -> None:
        self._runs.get_processing_run(run_id)
        with self.connect() as connection:
            connection.execute(
                """
                DELETE FROM v2d1_identity_labels
                WHERE run_id = ? AND candidate_id = ?
                """,
                (run_id, candidate_id),
            )

    def list_v2d1_identity_labels(self, run_id: int) -> list[sqlite3.Row]:
        run = self._runs.get_processing_run(run_id)
        if str(run["run_kind"]) != "quality_diarization_v2d1":
            raise ValueError("人物标签只适用于 V2-D.1 可能语音候选")
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT labels.*, reviews.session_start_ms,
                           reviews.session_end_ms, reviews.status AS review_status
                    FROM v2d1_identity_labels AS labels
                    JOIN v2d1_candidate_reviews AS reviews
                      ON reviews.run_id = labels.run_id
                     AND reviews.candidate_id = labels.candidate_id
                    WHERE labels.run_id = ?
                    ORDER BY reviews.session_start_ms, labels.id
                    """,
                    (run_id,),
                )
            )

    def upsert_manual_identity_annotation(
        self, values: dict[str, Any]
    ) -> sqlite3.Row:
        session_id = int(values["session_id"])
        run_id = int(values["diarization_run_id"])
        run = self._runs.get_processing_run(run_id)
        if (
            str(run["run_kind"]) != "quality_diarization_v2d"
            or str(run["status"]) != "completed"
            or int(run["session_id"] or 0) != session_id
        ):
            raise ValueError("人物区间必须属于当前已完成的 V2-D run")
        start_ms = int(values["session_start_ms"])
        end_ms = int(values["session_end_ms"])
        session = self._sessions.get_recording_session(session_id)
        if start_ms < 0 or end_ms <= start_ms or end_ms > int(session["duration_ms"]):
            raise ValueError("人物标注时间范围无效")
        identity_label = str(values["identity_label"]).strip()
        anonymous_label = str(values["anonymous_speaker_label"]).strip()
        if not identity_label or not anonymous_label:
            raise ValueError("人物和匿名 speaker 标签不能为空")
        now = self._now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO manual_identity_annotations (
                    annotation_key, session_id, diarization_run_id,
                    session_start_ms, session_end_ms, identity_label,
                    anonymous_speaker_label, status, note,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
                ON CONFLICT(
                    session_id, diarization_run_id,
                    session_start_ms, session_end_ms
                ) DO UPDATE SET
                    identity_label = excluded.identity_label,
                    anonymous_speaker_label = excluded.anonymous_speaker_label,
                    status = 'active',
                    note = excluded.note,
                    updated_at = excluded.updated_at
                """,
                (
                    str(values["annotation_key"]),
                    session_id,
                    run_id,
                    start_ms,
                    end_ms,
                    identity_label,
                    anonymous_label,
                    values.get("note"),
                    now,
                    now,
                ),
            )
            return connection.execute(
                """
                SELECT * FROM manual_identity_annotations
                WHERE session_id = ? AND diarization_run_id = ?
                  AND session_start_ms = ? AND session_end_ms = ?
                """,
                (session_id, run_id, start_ms, end_ms),
            ).fetchone()

    def list_manual_identity_annotations(
        self,
        session_id: int,
        *,
        diarization_run_id: int | None = None,
        status: str | None = "active",
    ) -> list[sqlite3.Row]:
        self._sessions.get_recording_session(session_id)
        sql = "SELECT * FROM manual_identity_annotations WHERE session_id = ?"
        params: list[Any] = [session_id]
        if diarization_run_id is not None:
            sql += " AND diarization_run_id = ?"
            params.append(diarization_run_id)
        if status is not None:
            if status not in {"active", "retracted"}:
                raise ValueError("人物区间状态无效")
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY session_start_ms, id"
        with self.connect() as connection:
            return list(connection.execute(sql, params))

    def retract_manual_identity_annotation(
        self, annotation_id: int
    ) -> sqlite3.Row:
        now = self._now()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM manual_identity_annotations WHERE id = ?",
                (annotation_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"人工人物区间 {annotation_id} 不存在")
            connection.execute(
                """
                UPDATE manual_identity_annotations
                SET status = 'retracted', updated_at = ?
                WHERE id = ?
                """,
                (now, annotation_id),
            )
            return connection.execute(
                "SELECT * FROM manual_identity_annotations WHERE id = ?",
                (annotation_id,),
            ).fetchone()

    def upsert_identity_candidate_review(
        self, run_id: int, values: dict[str, Any]
    ) -> sqlite3.Row:
        run = self._runs.get_processing_run(run_id)
        if str(run["run_kind"]) != "quality_diarization_v2d3":
            raise ValueError("身份候选审核只适用于 V2-D.3 run")
        if str(run["status"]) != "completed":
            raise ValueError("只能审核已完成的 V2-D.3 run")
        status = str(values["status"])
        if status not in {"confirmed_target", "rejected", "uncertain"}:
            raise ValueError("身份候选审核状态无效")
        start_ms = int(values["session_start_ms"])
        end_ms = int(values["session_end_ms"])
        if start_ms < 0 or end_ms <= start_ms:
            raise ValueError("身份候选审核时间范围无效")
        now = self._now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO identity_candidate_reviews (
                    run_id, candidate_id, target_identity, session_start_ms,
                    session_end_ms, status, note, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, candidate_id) DO UPDATE SET
                    status = excluded.status,
                    note = excluded.note,
                    updated_at = excluded.updated_at
                """,
                (
                    run_id,
                    str(values["candidate_id"]),
                    str(values["target_identity"]),
                    start_ms,
                    end_ms,
                    status,
                    values.get("note"),
                    now,
                    now,
                ),
            )
            return connection.execute(
                """
                SELECT * FROM identity_candidate_reviews
                WHERE run_id = ? AND candidate_id = ?
                """,
                (run_id, str(values["candidate_id"])),
            ).fetchone()

    def list_identity_candidate_reviews(self, run_id: int) -> list[sqlite3.Row]:
        run = self._runs.get_processing_run(run_id)
        if str(run["run_kind"]) != "quality_diarization_v2d3":
            raise ValueError("身份候选审核只适用于 V2-D.3 run")
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM identity_candidate_reviews
                    WHERE run_id = ? ORDER BY session_start_ms, id
                    """,
                    (run_id,),
                )
            )

    def upsert_identity_reference_interval(
        self, values: dict[str, Any]
    ) -> sqlite3.Row:
        """Save one source-backed identity reference without copying source audio."""
        identity_label = str(values["identity_label"]).strip()
        if not identity_label:
            raise ValueError("人物参考身份不能为空")
        decision = str(values["decision"])
        if decision not in {"confirmed_target", "rejected", "uncertain"}:
            raise ValueError("人物参考结论无效")
        provenance_kind = str(values["provenance_kind"])
        if provenance_kind not in {"truth", "v2d3_review"}:
            raise ValueError("人物参考来源无效")
        session_id = int(values["session_id"])
        source_object_id = int(values["source_object_id"])
        session_start = int(values["session_start_ms"])
        session_end = int(values["session_end_ms"])
        source_start = int(values["source_start_ms"])
        source_end = int(values["source_end_ms"])
        source_instance_id = values.get("source_instance_id")
        if session_start < 0 or session_end <= session_start:
            raise ValueError("人物参考会话时间范围无效")
        if source_start < 0 or source_end <= source_start:
            raise ValueError("人物参考原音时间范围无效")
        now = self._now()
        with self.connect() as connection:
            source_matches = list(connection.execute(
                """
                SELECT so.sha256, ss.source_instance_id,
                       ss.session_start_ms, ss.session_end_ms,
                       ss.source_start_ms, ss.source_end_ms
                FROM source_objects so
                JOIN session_sources ss ON ss.source_object_id = so.id
                WHERE so.id = ? AND ss.session_id = ?
                  AND (? IS NULL OR ss.source_instance_id = ?)
                """,
                (source_object_id, session_id, source_instance_id, source_instance_id),
            ))
            if not source_matches:
                raise ValueError("人物参考原音不属于指定录音会话")
            if len(source_matches) > 1:
                raise ValueError("人物参考原音实例不明确")
            source = source_matches[0]
            source_instance_id = int(source["source_instance_id"])
            if str(source["sha256"]) != str(values["source_sha256"]):
                raise ValueError("人物参考原音 SHA-256 不一致")
            if (
                source_start < int(source["source_start_ms"])
                or source_end > int(source["source_end_ms"])
            ):
                raise ValueError("人物参考范围超出不可变原音映射")
            mapped_start = int(source["session_start_ms"]) + (
                source_start - int(source["source_start_ms"])
            )
            mapped_end = mapped_start + (source_end - source_start)
            if (mapped_start, mapped_end) != (session_start, session_end):
                raise ValueError("人物参考的会话时间与原音时间不对应")
            connection.execute(
                """
                INSERT INTO identity_reference_intervals (
                    reference_key, identity_label, decision, session_id,
                    session_start_ms, session_end_ms, source_object_id, source_instance_id,
                    source_sha256, source_start_ms, source_end_ms,
                    provenance_kind, provenance_id, metadata_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(
                    identity_label, session_id, source_instance_id,
                    source_start_ms, source_end_ms
                ) DO UPDATE SET
                    decision = excluded.decision,
                    provenance_kind = excluded.provenance_kind,
                    provenance_id = excluded.provenance_id,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    str(values["reference_key"]),
                    identity_label,
                    decision,
                    session_id,
                    session_start,
                    session_end,
                    source_object_id,
                    source_instance_id,
                    str(values["source_sha256"]),
                    source_start,
                    source_end,
                    provenance_kind,
                    int(values["provenance_id"]),
                    json.dumps(
                        values.get("metadata", {}),
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    now,
                    now,
                ),
            )
            return connection.execute(
                """
                SELECT * FROM identity_reference_intervals
                WHERE identity_label = ? AND session_id = ?
                  AND source_object_id = ? AND source_start_ms = ?
                  AND source_end_ms = ?
                """,
                (
                    identity_label,
                    session_id,
                    source_object_id,
                    source_start,
                    source_end,
                ),
            ).fetchone()

    def list_identity_reference_intervals(
        self, identity_label: str | None = None
    ) -> list[sqlite3.Row]:
        with self.connect() as connection:
            if identity_label is None:
                return list(
                    connection.execute(
                        """
                        SELECT * FROM identity_reference_intervals
                        ORDER BY identity_label, session_id, session_start_ms, id
                        """
                    )
                )
            return list(
                connection.execute(
                    """
                    SELECT * FROM identity_reference_intervals
                    WHERE identity_label = ?
                    ORDER BY session_id, session_start_ms, id
                    """,
                    (identity_label,),
                )
            )

    def get_segment(self, segment_id: int) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM speech_segments WHERE id = ?", (segment_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"片段 {segment_id} 不存在")
        return row

    def upsert_self_profile(
        self,
        *,
        display_name: str,
        embedding_model: str,
        embedding_version: str,
        embedding_path: str,
    ) -> sqlite3.Row:
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM person_profiles WHERE profile_type = 'self' ORDER BY id LIMIT 1"
            ).fetchone()
            if existing is None:
                cursor = connection.execute(
                    """
                    INSERT INTO person_profiles (
                        display_name, profile_type, embedding_model,
                        embedding_version, embedding_path, created_at
                    ) VALUES (?, 'self', ?, ?, ?, ?)
                    """,
                    (display_name, embedding_model, embedding_version, embedding_path, self._now()),
                )
                profile_id = int(cursor.lastrowid)
            else:
                profile_id = int(existing["id"])
                connection.execute(
                    """
                    UPDATE person_profiles
                    SET display_name = ?, embedding_model = ?, embedding_version = ?, embedding_path = ?
                    WHERE id = ?
                    """,
                    (display_name, embedding_model, embedding_version, embedding_path, profile_id),
                )
            return connection.execute(
                "SELECT * FROM person_profiles WHERE id = ?", (profile_id,)
            ).fetchone()

    def get_self_profile(self) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM person_profiles WHERE profile_type = 'self' ORDER BY id LIMIT 1"
            ).fetchone()

    def upsert_known_person_profile(
        self,
        *,
        display_name: str,
        embedding_model: str,
        embedding_version: str,
        embedding_path: str,
    ) -> sqlite3.Row:
        with self.connect() as connection:
            existing = connection.execute(
                """
                SELECT * FROM person_profiles
                WHERE profile_type = 'known_person' AND display_name = ?
                ORDER BY id LIMIT 1
                """,
                (display_name,),
            ).fetchone()
            if existing is None:
                cursor = connection.execute(
                    """
                    INSERT INTO person_profiles (
                        display_name, profile_type, embedding_model,
                        embedding_version, embedding_path, created_at
                    ) VALUES (?, 'known_person', ?, ?, ?, ?)
                    """,
                    (
                        display_name,
                        embedding_model,
                        embedding_version,
                        embedding_path,
                        self._now(),
                    ),
                )
                profile_id = int(cursor.lastrowid)
            else:
                profile_id = int(existing["id"])
                connection.execute(
                    """
                    UPDATE person_profiles
                    SET embedding_model = ?, embedding_version = ?, embedding_path = ?
                    WHERE id = ?
                    """,
                    (embedding_model, embedding_version, embedding_path, profile_id),
                )
            return connection.execute(
                "SELECT * FROM person_profiles WHERE id = ?", (profile_id,)
            ).fetchone()

    def get_person_profile(self, person_id: int) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM person_profiles WHERE id = ?", (person_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"人物 {person_id} 不存在")
        return row

    def list_person_profiles(self) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(connection.execute("SELECT * FROM person_profiles ORDER BY id"))

    def clear_person_assignments(self, recording_id: int, person_id: int) -> int:
        """Remove a person's derived identity from one recording, preserving speaker labels."""
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE speech_segments
                SET person_id = NULL, speaker_match_score = NULL, updated_at = ?
                WHERE recording_id = ? AND person_id = ?
                """,
                (self._now(), recording_id, person_id),
            )
            return cursor.rowcount

    def person_assignment_count(self, person_id: int) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM speech_segments WHERE person_id = ?",
                (person_id,),
            ).fetchone()
            return int(row["count"])

    def delete_person_profile(self, person_id: int) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM person_profiles WHERE id = ?", (person_id,)
            )
            return cursor.rowcount == 1

    def assign_person_to_speaker(
        self,
        recording_id: int,
        speaker_session_id: str,
        person_id: int,
        score: float = 1.0,
    ) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE speech_segments
                SET person_id = ?, speaker_match_score = ?, updated_at = ?
                WHERE recording_id = ? AND speaker_session_id = ?
                """,
                (person_id, score, self._now(), recording_id, speaker_session_id),
            )
            return cursor.rowcount

    def assign_person_to_segments(
        self,
        recording_id: int,
        person_id: int,
        segment_scores: Sequence[tuple[int, float]],
    ) -> int:
        if not segment_scores:
            return 0
        with self.connect() as connection:
            before = connection.total_changes
            connection.executemany(
                """
                UPDATE speech_segments
                SET person_id = ?, speaker_match_score = ?, updated_at = ?
                WHERE recording_id = ? AND id = ?
                """,
                [
                    (person_id, score, self._now(), recording_id, segment_id)
                    for segment_id, score in segment_scores
                ],
            )
            return connection.total_changes - before

    def upsert_segment_annotations(
        self, recording_id: int, annotations: Sequence[dict[str, Any]]
    ) -> int:
        if not annotations:
            return 0
        segment_ids = [int(item["segment_id"]) for item in annotations]
        placeholders = ",".join("?" for _ in segment_ids)
        now = self._now()
        with self.connect() as connection:
            valid_rows = connection.execute(
                f"""
                SELECT id FROM speech_segments
                WHERE recording_id = ? AND id IN ({placeholders})
                """,
                (recording_id, *segment_ids),
            ).fetchall()
            if len(valid_rows) != len(set(segment_ids)):
                raise ValueError("标注中包含不属于当前录音的片段")
            connection.executemany(
                """
                INSERT INTO segment_identity_annotations (
                    recording_id, segment_id, identity_label, raw_label,
                    confidence, annotation_source, note, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(recording_id, segment_id) DO UPDATE SET
                    identity_label = excluded.identity_label,
                    raw_label = excluded.raw_label,
                    confidence = excluded.confidence,
                    annotation_source = excluded.annotation_source,
                    note = excluded.note,
                    updated_at = excluded.updated_at
                """,
                [
                    (
                        recording_id,
                        int(item["segment_id"]),
                        item["identity_label"],
                        item["raw_label"],
                        item["confidence"],
                        item.get("annotation_source", "human"),
                        item.get("note"),
                        now,
                        now,
                    )
                    for item in annotations
                ],
            )
        return len(annotations)

    def list_segment_annotations(self, recording_id: int) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM segment_identity_annotations
                    WHERE recording_id = ? ORDER BY segment_id
                    """,
                    (recording_id,),
                )
            )

    def upsert_voice_library_sample(self, values: dict[str, Any]) -> sqlite3.Row:
        now = self._now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO voice_library_samples (
                    sample_key, person_id, identity_label, sample_type, split,
                    source_path, stored_path, source_sha256, recording_id, segment_id,
                    session_key, duration_ms, speech_ms, embedding_blob, embedding_dim,
                    embedding_model, embedding_version, match_score, human_confirmed,
                    metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(sample_key) DO UPDATE SET
                    person_id = excluded.person_id,
                    identity_label = excluded.identity_label,
                    sample_type = excluded.sample_type,
                    split = excluded.split,
                    source_path = excluded.source_path,
                    stored_path = excluded.stored_path,
                    source_sha256 = excluded.source_sha256,
                    recording_id = excluded.recording_id,
                    segment_id = excluded.segment_id,
                    session_key = excluded.session_key,
                    duration_ms = excluded.duration_ms,
                    speech_ms = excluded.speech_ms,
                    embedding_blob = excluded.embedding_blob,
                    embedding_dim = excluded.embedding_dim,
                    embedding_model = excluded.embedding_model,
                    embedding_version = excluded.embedding_version,
                    match_score = excluded.match_score,
                    human_confirmed = excluded.human_confirmed,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    values["sample_key"],
                    values.get("person_id"),
                    values["identity_label"],
                    values["sample_type"],
                    values["split"],
                    values["source_path"],
                    values.get("stored_path"),
                    values.get("source_sha256"),
                    values.get("recording_id"),
                    values.get("segment_id"),
                    values["session_key"],
                    values["duration_ms"],
                    values["speech_ms"],
                    values.get("embedding_blob"),
                    values.get("embedding_dim"),
                    values.get("embedding_model"),
                    values.get("embedding_version"),
                    values.get("match_score"),
                    1 if values.get("human_confirmed", True) else 0,
                    json.dumps(values.get("metadata", {}), ensure_ascii=False),
                    now,
                    now,
                ),
            )
            return connection.execute(
                "SELECT * FROM voice_library_samples WHERE sample_key = ?",
                (values["sample_key"],),
            ).fetchone()

    def list_voice_library_samples(
        self, *, person_id: int | None = None, recording_id: int | None = None
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        params: list[Any] = []
        if person_id is not None:
            clauses.append("person_id = ?")
            params.append(person_id)
        if recording_id is not None:
            clauses.append("recording_id = ?")
            params.append(recording_id)
        sql = "SELECT * FROM voice_library_samples"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        with self.connect() as connection:
            return list(connection.execute(sql, params))
