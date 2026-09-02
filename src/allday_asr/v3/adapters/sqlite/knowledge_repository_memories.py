from __future__ import annotations

import sqlite3
from typing import Any

from allday_asr.v3.domain.knowledge import (
    MemoryRecord,
)
from .knowledge_repository_codec import (
    _datetime,
    _dict,
    _json,
    _object,
)


class KnowledgeMemoryRepositoryMixin:
    def next_memory_version(self, memory_id: str) -> int:
        row = self.connection.execute(
            """
            SELECT COALESCE(MAX(version), 0) + 1 AS next_version
            FROM memory_records WHERE memory_id = ?
            """,
            (memory_id,),
        ).fetchone()
        return int(row["next_version"])

    def add_memory(self, memory: MemoryRecord) -> bool:
        cursor = self.connection.execute(
            """
            INSERT INTO memory_records (
              memory_id, version, session_id, kind, subject_type, subject_id,
              content_json, generation_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                memory.memory_id,
                memory.version,
                memory.session_id,
                memory.kind.value,
                memory.subject_type,
                memory.subject_id,
                _json(memory.content),
                memory.generation_id,
                _datetime(memory.created_at),
            ),
        )
        return cursor.rowcount == 1

    def list_memories(
        self, session_id: str | None, subject_type: str | None, subject_id: str | None
    ) -> tuple[dict[str, Any], ...]:
        clauses: list[str] = []
        parameters: list[object] = []
        if session_id is not None:
            clauses.append("memory.session_id = ?")
            parameters.append(session_id)
        if subject_type is not None:
            clauses.append("memory.subject_type = ?")
            parameters.append(subject_type)
        if subject_id is not None:
            clauses.append("memory.subject_id = ?")
            parameters.append(subject_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.connection.execute(
            f"""
            SELECT memory.*, CASE WHEN EXISTS (
              SELECT 1 FROM invalidation_events invalidation
              WHERE invalidation.target_type = 'memory'
                AND invalidation.target_id = memory.memory_id
                AND invalidation.target_revision = memory.version
                AND invalidation.status IN ('stale', 'invalid')
            ) THEN 'stale' WHEN EXISTS (
              SELECT 1 FROM invalidation_events invalidation
              WHERE invalidation.target_type = 'memory'
                AND invalidation.target_id = memory.memory_id
                AND invalidation.target_revision = memory.version
                AND invalidation.status = 'superseded'
            ) THEN 'superseded' ELSE 'active' END AS derivation_status,
              g.producer, g.producer_version, g.model, g.prompt_version,
              g.extractor_version, g.input_scope_json, g.input_sha256,
              g.generation_number
            FROM memory_records memory
            JOIN generation_records g ON g.generation_id = memory.generation_id
            {where}
            ORDER BY memory.created_at DESC, memory.memory_id, memory.version DESC
            """,
            parameters,
        ).fetchall()
        return tuple(self._memory_dict(row) for row in rows)

    def add_evidence_link(
        self,
        link_id: str,
        subject_type: str,
        subject_id: str,
        subject_revision: int,
        evidence_type: str,
        evidence_id: str,
        evidence_revision: int,
        created_at: str,
    ) -> bool:
        cursor = self.connection.execute(
            """
            INSERT INTO evidence_links (
              link_id, subject_type, subject_id, subject_revision,
              evidence_type, evidence_id, evidence_revision, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                link_id,
                subject_type,
                subject_id,
                subject_revision,
                evidence_type,
                evidence_id,
                evidence_revision,
                created_at,
            ),
        )
        return cursor.rowcount == 1

    def _evidence_links(
        self, subject_type: str, subject_id: str, subject_revision: int
    ) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT evidence_type, evidence_id, evidence_revision
            FROM evidence_links WHERE subject_type = ? AND subject_id = ?
              AND subject_revision = ? ORDER BY evidence_type, evidence_id
            """,
            (subject_type, subject_id, subject_revision),
        ).fetchall()
        return [_dict(row) for row in rows]

    def _event_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        value = _dict(row)
        value["payload"] = _object(value.pop("payload_json"))
        links = self.connection.execute(
            """
            SELECT evidence_type, evidence_id, MAX(evidence_revision)
              AS evidence_revision
            FROM evidence_links WHERE subject_type = 'event' AND subject_id = ?
              AND subject_revision <= ?
            GROUP BY evidence_type, evidence_id
            ORDER BY evidence_type, evidence_id
            """,
            (str(row["event_id"]), int(row["revision"])),
        ).fetchall()
        value["evidence"] = [_dict(link) for link in links]
        return value

    def _operation_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        value = _dict(row)
        value["payload"] = _object(value.pop("payload_json"))
        value["evidence"] = self._evidence_links(
            "event", str(row["event_id"]), int(row["event_revision"])
        )
        return value

    def _memory_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        value = _dict(row)
        value["content"] = _object(value.pop("content_json"))
        value["input_scope"] = _object(value.pop("input_scope_json"))
        value["evidence"] = self._evidence_links(
            "memory", str(row["memory_id"]), int(row["version"])
        )
        return value
