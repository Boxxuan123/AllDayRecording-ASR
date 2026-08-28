from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS recordings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path TEXT NOT NULL,
    sha256 TEXT NOT NULL UNIQUE,
    device TEXT,
    recorded_at TEXT NOT NULL,
    timezone TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    codec TEXT,
    sample_rate INTEGER,
    channels INTEGER,
    bit_rate INTEGER,
    encoder TEXT,
    normalized_path TEXT,
    status TEXT NOT NULL DEFAULT 'imported',
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS processing_stages (
    recording_id INTEGER NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    model_id TEXT,
    model_version TEXT,
    started_at TEXT,
    completed_at TEXT,
    error TEXT,
    details_json TEXT,
    PRIMARY KEY (recording_id, stage),
    FOREIGN KEY (recording_id) REFERENCES recordings(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS person_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    display_name TEXT NOT NULL,
    profile_type TEXT NOT NULL CHECK(profile_type IN ('self', 'known_person')),
    embedding_model TEXT,
    embedding_version TEXT,
    embedding_path TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS speech_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recording_id INTEGER NOT NULL,
    segment_index INTEGER NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    speaker_session_id TEXT,
    person_id INTEGER,
    speaker_match_score REAL,
    language TEXT,
    text_raw TEXT,
    text_display TEXT,
    asr_model TEXT,
    audio_ref TEXT NOT NULL,
    asr_status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(recording_id, segment_index),
    FOREIGN KEY (recording_id) REFERENCES recordings(id) ON DELETE CASCADE,
    FOREIGN KEY (person_id) REFERENCES person_profiles(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_segments_recording_time
ON speech_segments(recording_id, start_ms);

CREATE INDEX IF NOT EXISTS idx_segments_asr_status
ON speech_segments(recording_id, asr_status);

CREATE TABLE IF NOT EXISTS segment_identity_annotations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recording_id INTEGER NOT NULL,
    segment_id INTEGER NOT NULL,
    identity_label TEXT NOT NULL,
    raw_label TEXT NOT NULL,
    confidence TEXT NOT NULL,
    annotation_source TEXT NOT NULL DEFAULT 'human',
    note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(recording_id, segment_id),
    FOREIGN KEY (recording_id) REFERENCES recordings(id) ON DELETE CASCADE,
    FOREIGN KEY (segment_id) REFERENCES speech_segments(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS voice_library_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_key TEXT NOT NULL UNIQUE,
    person_id INTEGER,
    identity_label TEXT NOT NULL,
    sample_type TEXT NOT NULL,
    split TEXT NOT NULL CHECK(split IN ('accepted', 'holdout', 'negative')),
    source_path TEXT NOT NULL,
    stored_path TEXT,
    source_sha256 TEXT,
    recording_id INTEGER,
    segment_id INTEGER,
    session_key TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    speech_ms INTEGER NOT NULL,
    embedding_blob BLOB,
    embedding_dim INTEGER,
    embedding_model TEXT,
    embedding_version TEXT,
    match_score REAL,
    human_confirmed INTEGER NOT NULL DEFAULT 1,
    metadata_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (person_id) REFERENCES person_profiles(id) ON DELETE SET NULL,
    FOREIGN KEY (recording_id) REFERENCES recordings(id) ON DELETE SET NULL,
    FOREIGN KEY (segment_id) REFERENCES speech_segments(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_voice_library_person_split
ON voice_library_samples(person_id, split);

CREATE INDEX IF NOT EXISTS idx_voice_library_identity_label
ON voice_library_samples(identity_label, split);

CREATE TABLE IF NOT EXISTS conversation_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recording_id INTEGER NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    title TEXT,
    summary TEXT,
    segment_ids_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (recording_id) REFERENCES recordings(id) ON DELETE CASCADE
);
"""

LATEST_SCHEMA_VERSION = 3

MIGRATIONS: dict[int, str] = {
    2: """
        CREATE TABLE IF NOT EXISTS processing_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recording_id INTEGER NOT NULL,
            run_kind TEXT NOT NULL,
            status TEXT NOT NULL,
            config_json TEXT NOT NULL,
            config_sha256 TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            error TEXT,
            summary_json TEXT,
            artifacts_json TEXT,
            FOREIGN KEY (recording_id) REFERENCES recordings(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_processing_runs_recording
        ON processing_runs(recording_id, id);

        CREATE TABLE IF NOT EXISTS evaluation_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recording_id INTEGER NOT NULL,
            truth_path TEXT NOT NULL,
            truth_sha256 TEXT NOT NULL,
            config_json TEXT NOT NULL,
            metrics_json TEXT NOT NULL,
            report_json_path TEXT NOT NULL,
            report_markdown_path TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (recording_id) REFERENCES recordings(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_evaluation_runs_recording
        ON evaluation_runs(recording_id, id);
    """,
    3: """
        CREATE TABLE IF NOT EXISTS action_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recording_id INTEGER NOT NULL,
            candidate_key TEXT NOT NULL UNIQUE,
            candidate_type TEXT NOT NULL CHECK(candidate_type IN ('schedule', 'todo')),
            status TEXT NOT NULL CHECK(status IN ('pending', 'confirmed', 'dismissed')),
            start_ms INTEGER NOT NULL,
            end_ms INTEGER NOT NULL,
            source_segment_ids_json TEXT NOT NULL,
            title TEXT NOT NULL,
            scheduled_at TEXT,
            time_text TEXT,
            location TEXT,
            participants_json TEXT NOT NULL,
            confidence REAL NOT NULL,
            evidence_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (recording_id) REFERENCES recordings(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_action_candidates_recording_status
        ON action_candidates(recording_id, status, start_ms);
    """,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            versions = {
                int(row["version"])
                for row in connection.execute("SELECT version FROM schema_migrations")
            }
            if not versions:
                connection.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (1, ?)",
                    (utc_now(),),
                )
                versions.add(1)
            if max(versions) > LATEST_SCHEMA_VERSION:
                raise RuntimeError(
                    f"数据库版本 {max(versions)} 高于程序支持的 {LATEST_SCHEMA_VERSION}"
                )
            for version in range(2, LATEST_SCHEMA_VERSION + 1):
                if version in versions:
                    continue
                connection.executescript(MIGRATIONS[version])
                connection.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (version, utc_now()),
                )

    def schema_version(self) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT MAX(version) AS version FROM schema_migrations"
            ).fetchone()
            return int(row["version"] or 0)

    def find_recording_by_hash(self, sha256: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM recordings WHERE sha256 = ?", (sha256,)
            ).fetchone()

    def create_recording(self, values: dict[str, Any]) -> sqlite3.Row:
        now = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO recordings (
                    source_path, sha256, device, recorded_at, timezone, duration_ms,
                    codec, sample_rate, channels, bit_rate, encoder, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    values["source_path"],
                    values["sha256"],
                    values.get("device"),
                    values["recorded_at"],
                    values["timezone"],
                    values["duration_ms"],
                    values.get("codec"),
                    values.get("sample_rate"),
                    values.get("channels"),
                    values.get("bit_rate"),
                    values.get("encoder"),
                    now,
                    now,
                ),
            )
            recording_id = int(cursor.lastrowid)
            return connection.execute(
                "SELECT * FROM recordings WHERE id = ?", (recording_id,)
            ).fetchone()

    def get_recording(self, recording_id: int) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM recordings WHERE id = ?", (recording_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"录音 {recording_id} 不存在")
        return row

    def list_recordings(self) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(connection.execute("SELECT * FROM recordings ORDER BY id DESC"))

    def update_recording(self, recording_id: int, **values: Any) -> None:
        if not values:
            return
        values["updated_at"] = utc_now()
        assignments = ", ".join(f"{column} = ?" for column in values)
        params = [*values.values(), recording_id]
        with self.connect() as connection:
            connection.execute(
                f"UPDATE recordings SET {assignments} WHERE id = ?", params
            )

    def set_stage(
        self,
        recording_id: int,
        stage: str,
        status: str,
        *,
        model_id: str | None = None,
        model_version: str | None = None,
        error: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        now = utc_now()
        started_at = now if status == "running" else None
        completed_at = now if status in {"completed", "partial", "failed"} else None
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT started_at FROM processing_stages WHERE recording_id = ? AND stage = ?",
                (recording_id, stage),
            ).fetchone()
            preserved_start = existing["started_at"] if existing else None
            connection.execute(
                """
                INSERT INTO processing_stages (
                    recording_id, stage, status, model_id, model_version,
                    started_at, completed_at, error, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(recording_id, stage) DO UPDATE SET
                    status = excluded.status,
                    model_id = COALESCE(excluded.model_id, processing_stages.model_id),
                    model_version = COALESCE(excluded.model_version, processing_stages.model_version),
                    started_at = COALESCE(excluded.started_at, processing_stages.started_at),
                    completed_at = excluded.completed_at,
                    error = excluded.error,
                    details_json = excluded.details_json
                """,
                (
                    recording_id,
                    stage,
                    status,
                    model_id,
                    model_version,
                    started_at or preserved_start,
                    completed_at,
                    error,
                    json.dumps(details, ensure_ascii=False) if details else None,
                ),
            )

    def get_stage(self, recording_id: int, stage: str) -> sqlite3.Row | None:
        """Return the persisted status for one processing stage, if present."""
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT * FROM processing_stages
                WHERE recording_id = ? AND stage = ?
                """,
                (recording_id, stage),
            ).fetchone()

    def list_stages(self, recording_id: int) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM processing_stages
                    WHERE recording_id = ? ORDER BY stage
                    """,
                    (recording_id,),
                )
            )

    def start_processing_run(
        self,
        recording_id: int,
        *,
        run_kind: str,
        config: dict[str, Any],
        config_sha256: str,
    ) -> int:
        self.get_recording(recording_id)
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO processing_runs (
                    recording_id, run_kind, status, config_json,
                    config_sha256, started_at
                ) VALUES (?, ?, 'running', ?, ?, ?)
                """,
                (
                    recording_id,
                    run_kind,
                    json.dumps(config, ensure_ascii=False, sort_keys=True),
                    config_sha256,
                    utc_now(),
                ),
            )
            return int(cursor.lastrowid)

    def finish_processing_run(
        self,
        run_id: int,
        *,
        status: str,
        summary: dict[str, Any] | None = None,
        artifacts: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE processing_runs
                SET status = ?, completed_at = ?, error = ?,
                    summary_json = ?, artifacts_json = ?
                WHERE id = ?
                """,
                (
                    status,
                    utc_now(),
                    error[:2000] if error else None,
                    json.dumps(summary, ensure_ascii=False) if summary is not None else None,
                    json.dumps(artifacts, ensure_ascii=False) if artifacts is not None else None,
                    run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"处理运行 {run_id} 不存在")

    def list_processing_runs(self, recording_id: int) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM processing_runs
                    WHERE recording_id = ? ORDER BY id
                    """,
                    (recording_id,),
                )
            )

    def segment_count(self, recording_id: int) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM speech_segments WHERE recording_id = ?",
                (recording_id,),
            ).fetchone()
            return int(row["count"])

    def replace_vad_segments(
        self,
        recording_id: int,
        segments: Sequence[tuple[int, int]],
        source_path: str,
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM speech_segments WHERE recording_id = ?", (recording_id,)
            )
            connection.executemany(
                """
                INSERT INTO speech_segments (
                    recording_id, segment_index, start_ms, end_ms, audio_ref,
                    asr_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                [
                    (
                        recording_id,
                        index,
                        start_ms,
                        end_ms,
                        f"{source_path}#t={start_ms / 1000:.3f},{end_ms / 1000:.3f}",
                        now,
                        now,
                    )
                    for index, (start_ms, end_ms) in enumerate(segments)
                ],
            )

    def reset_failed_segments(self, recording_id: int) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE speech_segments
                SET asr_status = 'pending', error = NULL, updated_at = ?
                WHERE recording_id = ? AND asr_status = 'failed'
                """,
                (utc_now(), recording_id),
            )
            return cursor.rowcount

    def reset_interrupted_segments(self, recording_id: int) -> int:
        """Recover segments left in running state by an interrupted process."""
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE speech_segments
                SET asr_status = 'pending', error = '上次处理被中断，已自动恢复', updated_at = ?
                WHERE recording_id = ? AND asr_status = 'running'
                """,
                (utc_now(), recording_id),
            )
            return cursor.rowcount

    def reset_all_asr_segments(self, recording_id: int) -> int:
        """Explicitly discard derived ASR text while preserving VAD boundaries."""
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE speech_segments
                SET asr_status = 'pending', language = NULL, text_raw = NULL,
                    text_display = NULL, asr_model = NULL, error = NULL, updated_at = ?
                WHERE recording_id = ?
                """,
                (utc_now(), recording_id),
            )
            return cursor.rowcount

    def pending_segments(
        self, recording_id: int, limit: int | None = None
    ) -> list[sqlite3.Row]:
        sql = """
            SELECT * FROM speech_segments
            WHERE recording_id = ? AND asr_status = 'pending'
            ORDER BY segment_index
        """
        params: list[Any] = [recording_id]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self.connect() as connection:
            return list(connection.execute(sql, params))

    def all_segments(self, recording_id: int, completed_only: bool = False) -> list[sqlite3.Row]:
        sql = """
            SELECT speech_segments.*, person_profiles.display_name AS person_name
            FROM speech_segments
            LEFT JOIN person_profiles ON person_profiles.id = speech_segments.person_id
            WHERE speech_segments.recording_id = ?
        """
        if completed_only:
            sql += " AND speech_segments.asr_status = 'completed'"
        sql += " ORDER BY speech_segments.segment_index"
        with self.connect() as connection:
            return list(connection.execute(sql, (recording_id,)))

    def mark_segment_running(self, segment_id: int) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE speech_segments
                SET asr_status = 'running', attempts = attempts + 1, error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (utc_now(), segment_id),
            )

    def mark_segment_completed(
        self,
        segment_id: int,
        *,
        language: str | None,
        text_raw: str,
        text_display: str,
        asr_model: str,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE speech_segments
                SET asr_status = 'completed', language = ?, text_raw = ?, text_display = ?,
                    asr_model = ?, error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (language, text_raw, text_display, asr_model, utc_now(), segment_id),
            )

    def mark_segment_failed(self, segment_id: int, error: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE speech_segments
                SET asr_status = 'failed', error = ?, updated_at = ?
                WHERE id = ?
                """,
                (error[:2000], utc_now(), segment_id),
            )

    def segment_status_counts(self, recording_id: int) -> dict[str, int]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT asr_status, COUNT(*) AS count
                FROM speech_segments WHERE recording_id = ? GROUP BY asr_status
                """,
                (recording_id,),
            )
            return {row["asr_status"]: int(row["count"]) for row in rows}

    def replace_speaker_labels(
        self, recording_id: int, labels: Sequence[tuple[int, str | None]]
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE speech_segments
                SET speaker_session_id = NULL, person_id = NULL,
                    speaker_match_score = NULL, updated_at = ?
                WHERE recording_id = ?
                """,
                (now, recording_id),
            )
            connection.executemany(
                """
                UPDATE speech_segments
                SET speaker_session_id = ?, updated_at = ?
                WHERE id = ? AND recording_id = ?
                """,
                [(label, now, segment_id, recording_id) for segment_id, label in labels],
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
                    (display_name, embedding_model, embedding_version, embedding_path, utc_now()),
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
                        utc_now(),
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
                (utc_now(), recording_id, person_id),
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
                (person_id, score, utc_now(), recording_id, speaker_session_id),
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
                    (person_id, score, utc_now(), recording_id, segment_id)
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
        now = utc_now()
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
        now = utc_now()
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

    def replace_conversation_events(
        self,
        recording_id: int,
        events: Sequence[dict[str, Any]],
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM conversation_events WHERE recording_id = ?", (recording_id,)
            )
            connection.executemany(
                """
                INSERT INTO conversation_events (
                    recording_id, start_ms, end_ms, title, summary,
                    segment_ids_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        recording_id,
                        event["start_ms"],
                        event["end_ms"],
                        event.get("title"),
                        event.get("summary"),
                        json.dumps(event["segment_ids"], ensure_ascii=False),
                        now,
                    )
                    for event in events
                ],
            )

    def list_conversation_events(self, recording_id: int) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM conversation_events
                    WHERE recording_id = ? ORDER BY start_ms
                    """,
                    (recording_id,),
                )
            )

    def record_evaluation_run(
        self,
        recording_id: int,
        *,
        truth_path: str,
        truth_sha256: str,
        config: dict[str, Any],
        metrics: dict[str, Any],
        report_json_path: str,
        report_markdown_path: str,
    ) -> int:
        self.get_recording(recording_id)
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO evaluation_runs (
                    recording_id, truth_path, truth_sha256, config_json,
                    metrics_json, report_json_path, report_markdown_path, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    recording_id,
                    truth_path,
                    truth_sha256,
                    json.dumps(config, ensure_ascii=False, sort_keys=True),
                    json.dumps(metrics, ensure_ascii=False, sort_keys=True),
                    report_json_path,
                    report_markdown_path,
                    utc_now(),
                ),
            )
            return int(cursor.lastrowid)

    def list_evaluation_runs(self, recording_id: int) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM evaluation_runs
                    WHERE recording_id = ? ORDER BY id
                    """,
                    (recording_id,),
                )
            )

    def upsert_action_candidate(self, values: dict[str, Any]) -> sqlite3.Row:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO action_candidates (
                    recording_id, candidate_key, candidate_type, status,
                    start_ms, end_ms, source_segment_ids_json, title,
                    scheduled_at, time_text, location, participants_json,
                    confidence, evidence_json, created_at, updated_at
                ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(candidate_key) DO UPDATE SET
                    start_ms = excluded.start_ms,
                    end_ms = excluded.end_ms,
                    source_segment_ids_json = excluded.source_segment_ids_json,
                    title = CASE
                        WHEN action_candidates.status = 'pending' THEN excluded.title
                        ELSE action_candidates.title
                    END,
                    scheduled_at = CASE
                        WHEN action_candidates.status = 'pending' THEN excluded.scheduled_at
                        ELSE action_candidates.scheduled_at
                    END,
                    time_text = excluded.time_text,
                    location = CASE
                        WHEN action_candidates.status = 'pending' THEN excluded.location
                        ELSE action_candidates.location
                    END,
                    participants_json = excluded.participants_json,
                    confidence = excluded.confidence,
                    evidence_json = excluded.evidence_json,
                    updated_at = excluded.updated_at
                """,
                (
                    values["recording_id"],
                    values["candidate_key"],
                    values["candidate_type"],
                    values["start_ms"],
                    values["end_ms"],
                    json.dumps(values["source_segment_ids"], ensure_ascii=False),
                    values["title"],
                    values.get("scheduled_at"),
                    values.get("time_text"),
                    values.get("location"),
                    json.dumps(values.get("participants", []), ensure_ascii=False),
                    values["confidence"],
                    json.dumps(values["evidence"], ensure_ascii=False),
                    now,
                    now,
                ),
            )
            return connection.execute(
                "SELECT * FROM action_candidates WHERE candidate_key = ?",
                (values["candidate_key"],),
            ).fetchone()

    def list_action_candidates(
        self, recording_id: int, *, status: str | None = None
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM action_candidates WHERE recording_id = ?"
        params: list[Any] = [recording_id]
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY start_ms, id"
        with self.connect() as connection:
            return list(connection.execute(sql, params))

    def get_action_candidate(self, candidate_id: int) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM action_candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"行动候选 {candidate_id} 不存在")
        return row

    def review_action_candidate(
        self,
        candidate_id: int,
        *,
        status: str,
        title: str | None = None,
        scheduled_at: str | None = None,
        location: str | None = None,
    ) -> sqlite3.Row:
        if status not in {"pending", "confirmed", "dismissed"}:
            raise ValueError("status 只能是 pending、confirmed 或 dismissed")
        if title is not None and not title.strip():
            raise ValueError("title 不能为空")
        if scheduled_at is not None:
            try:
                datetime.fromisoformat(scheduled_at.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("scheduled_at 必须是有效的 ISO 8601 时间") from exc
        updates = ["status = ?", "updated_at = ?"]
        params: list[Any] = [status, utc_now()]
        if title is not None:
            updates.append("title = ?")
            params.append(title.strip())
        if scheduled_at is not None:
            updates.append("scheduled_at = ?")
            params.append(scheduled_at)
        if location is not None:
            updates.append("location = ?")
            params.append(location)
        params.append(candidate_id)
        with self.connect() as connection:
            cursor = connection.execute(
                f"UPDATE action_candidates SET {', '.join(updates)} WHERE id = ?",
                params,
            )
            if cursor.rowcount != 1:
                raise KeyError(f"行动候选 {candidate_id} 不存在")
            return connection.execute(
                "SELECT * FROM action_candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
