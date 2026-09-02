from __future__ import annotations
import sqlite3
from allday_asr.v3.domain.models import (
    Artifact,
    ProcessingRun,
)
from allday_asr.v3.domain.processing import ArtifactInvalidation

from .core_repository_codec import (
    _processing_run,
    _artifact,
    _datetime,
    _optional_datetime,
    _json,
)


class SqliteProcessingRunRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def add(self, run: ProcessingRun) -> bool:
        cursor = self.connection.execute(
            """
            INSERT INTO processing_runs (
                run_id, session_id, pipeline_version, input_revision, status,
                config_digest, current_stage, progress, completed_at, error,
                legacy_ref, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                run.run_id,
                run.session_id,
                run.pipeline_version,
                run.input_revision,
                run.status.value,
                run.config_digest,
                run.current_stage,
                run.progress,
                _optional_datetime(run.completed_at),
                run.error,
                run.legacy_ref,
                _datetime(run.created_at),
                _datetime(run.updated_at),
            ),
        )
        return cursor.rowcount == 1

    def find_by_legacy_ref(self, legacy_ref: str) -> ProcessingRun | None:
        row = self.connection.execute(
            "SELECT * FROM processing_runs WHERE legacy_ref = ?", (legacy_ref,)
        ).fetchone()
        return _processing_run(row) if row is not None else None

    def get(self, run_id: str) -> ProcessingRun:
        row = self.connection.execute(
            "SELECT * FROM processing_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"processing run does not exist: {run_id}")
        return _processing_run(row)


class SqliteArtifactRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def add(self, artifact: Artifact) -> bool:
        cursor = self.connection.execute(
            """
            INSERT INTO artifacts (
                artifact_id, run_id, kind, producer, producer_version,
                config_digest, input_refs_json, storage_ref, sha256,
                size_bytes, status, metadata_json, legacy_ref, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                artifact.artifact_id,
                artifact.run_id,
                artifact.kind,
                artifact.producer,
                artifact.producer_version,
                artifact.config_digest,
                _json(list(artifact.input_refs)),
                artifact.storage_ref,
                artifact.sha256,
                artifact.size_bytes,
                artifact.status,
                _json(artifact.metadata),
                artifact.legacy_ref,
                _datetime(artifact.created_at),
            ),
        )
        return cursor.rowcount == 1

    def list_active_for_run(self, run_id: str) -> tuple[Artifact, ...]:
        rows = self.connection.execute(
            """
            SELECT a.* FROM artifacts a
            WHERE a.run_id = ? AND a.status = 'active'
              AND NOT EXISTS (
                SELECT 1 FROM artifact_status_events e
                WHERE e.artifact_id = a.artifact_id
              )
            ORDER BY a.created_at, a.artifact_id
            """,
            (run_id,),
        ).fetchall()
        return tuple(_artifact(row) for row in rows)

    def add_dependency(
        self, artifact_id: str, input_type: str, input_id: str, input_revision: int
    ) -> bool:
        cursor = self.connection.execute(
            """
            INSERT INTO artifact_dependencies (
                artifact_id, input_type, input_id, input_revision
            ) VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING
            """,
            (artifact_id, input_type, input_id, input_revision),
        )
        return cursor.rowcount == 1

    def dependent_ids(self, input_type: str, input_id: str) -> tuple[str, ...]:
        return tuple(
            str(row["artifact_id"])
            for row in self.connection.execute(
                """
                SELECT DISTINCT artifact_id FROM artifact_dependencies
                WHERE input_type = ? AND input_id = ? ORDER BY artifact_id
                """,
                (input_type, input_id),
            )
        )

    def invalidate(self, event: ArtifactInvalidation) -> bool:
        cursor = self.connection.execute(
            """
            INSERT INTO artifact_status_events (
                status_event_id, artifact_id, status, reason, source_type,
                source_id, source_revision, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING
            """,
            (
                event.status_event_id,
                event.artifact_id,
                event.status,
                event.reason,
                event.source_type,
                event.source_id,
                event.source_revision,
                _datetime(event.created_at),
            ),
        )
        return cursor.rowcount == 1
