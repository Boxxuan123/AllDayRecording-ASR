from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from allday_asr.v3.domain.knowledge import (
    DerivationDependency,
    EventCurrentState,
    EventKind,
    EventOperation,
    EventStatus,
    EvidenceSpan,
    GenerationRecord,
    GenerationStatus,
    InvalidationEvent,
    KnowledgeLayer,
    MemoryRecord,
    ProposalKind,
    ProposalStatus,
    RecomputeRequest,
    StructuredProposal,
)


class SqliteKnowledgeRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def unmaterialized_evidence(
        self, session_id: str
    ) -> tuple[dict[str, Any], ...]:
        rows = self.connection.execute(
            """
            SELECT u.utterance_id, u.session_id, u.source_artifact_id,
              u.revision AS utterance_revision, seg.asset_id,
              MAX(u.start_ms, seg.session_start_ms) AS session_start_ms,
              MIN(u.end_ms, seg.session_end_ms) AS session_end_ms,
              seg.source_start_ms + MAX(u.start_ms, seg.session_start_ms)
                - seg.session_start_ms AS asset_start_ms,
              seg.source_start_ms + MIN(u.end_ms, seg.session_end_ms)
                - seg.session_start_ms AS asset_end_ms
            FROM utterances u
            JOIN capture_segments seg ON seg.session_id = u.session_id
              AND u.end_ms > seg.session_start_ms
              AND u.start_ms < seg.session_end_ms
            WHERE u.session_id = ? AND u.status = 'active'
              AND NOT EXISTS (
                SELECT 1 FROM evidence_spans span
                WHERE span.utterance_id = u.utterance_id
                  AND span.asset_id = seg.asset_id
                  AND span.session_start_ms = MAX(u.start_ms, seg.session_start_ms)
                  AND span.session_end_ms = MIN(u.end_ms, seg.session_end_ms)
              )
            ORDER BY u.start_ms, seg.sequence, u.utterance_id
            """,
            (session_id,),
        ).fetchall()
        return tuple(_dict(row) for row in rows)

    def add_evidence_span(self, span: EvidenceSpan) -> bool:
        cursor = self.connection.execute(
            """
            INSERT INTO evidence_spans (
              evidence_span_id, session_id, asset_id, artifact_id, utterance_id,
              session_start_ms, session_end_ms, asset_start_ms, asset_end_ms,
              created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                span.evidence_span_id,
                span.session_id,
                span.asset_id,
                span.artifact_id,
                span.utterance_id,
                span.session_start_ms,
                span.session_end_ms,
                span.asset_start_ms,
                span.asset_end_ms,
                _datetime(span.created_at),
            ),
        )
        return cursor.rowcount == 1

    def list_evidence(self, session_id: str) -> tuple[dict[str, Any], ...]:
        rows = self.connection.execute(
            """
            SELECT span.*, a.media_id, a.sha256 AS asset_sha256,
              u.text, u.speaker_track_id, u.identity,
              u.revision AS utterance_revision
            FROM evidence_spans span
            JOIN audio_assets a ON a.asset_id = span.asset_id
            LEFT JOIN utterances u ON u.utterance_id = span.utterance_id
            WHERE span.session_id = ?
            ORDER BY span.session_start_ms, span.session_end_ms, span.evidence_span_id
            """,
            (session_id,),
        ).fetchall()
        return tuple(_dict(row) for row in rows)

    def next_generation_number(
        self,
        layer: str,
        producer: str,
        producer_version: str,
        model: str,
        prompt_version: str,
        extractor_version: str,
        input_sha256: str,
    ) -> int:
        row = self.connection.execute(
            """
            SELECT COALESCE(MAX(generation_number), 0) + 1 AS next_number
            FROM generation_records WHERE layer = ? AND producer = ?
              AND producer_version = ? AND model = ? AND prompt_version = ?
              AND extractor_version = ? AND input_sha256 = ?
            """,
            (
                layer,
                producer,
                producer_version,
                model,
                prompt_version,
                extractor_version,
                input_sha256,
            ),
        ).fetchone()
        return int(row["next_number"])

    def add_generation(self, generation: GenerationRecord) -> bool:
        cursor = self.connection.execute(
            """
            INSERT INTO generation_records (
              generation_id, layer, producer, producer_version, model,
              prompt_version, extractor_version, input_scope_json, input_sha256,
              generation_number, status, error, created_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                generation.generation_id,
                generation.layer.value,
                generation.producer,
                generation.producer_version,
                generation.model,
                generation.prompt_version,
                generation.extractor_version,
                _json(generation.input_scope),
                generation.input_sha256,
                generation.generation_number,
                generation.status.value,
                generation.error,
                _datetime(generation.created_at),
                _optional_datetime(generation.completed_at),
            ),
        )
        return cursor.rowcount == 1

    def get_generation(self, generation_id: str) -> GenerationRecord:
        row = self.connection.execute(
            "SELECT * FROM generation_records WHERE generation_id = ?",
            (generation_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"generation does not exist: {generation_id}")
        return _generation(row)

    def complete_generation(
        self, generation_id: str, status: str, completed_at: str, error: str | None
    ) -> None:
        cursor = self.connection.execute(
            """
            UPDATE generation_records SET status = ?, completed_at = ?, error = ?
            WHERE generation_id = ? AND status = 'collecting'
            """,
            (status, completed_at, error, generation_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("generation is not collecting")

    def add_proposal(self, proposal: StructuredProposal) -> bool:
        cursor = self.connection.execute(
            """
            INSERT INTO structured_change_proposals (
              proposal_id, generation_id, kind, payload_json,
              evidence_utterance_ids_json, status, created_at, resolved_at,
              resolved_by, resolution_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                proposal.proposal_id,
                proposal.generation_id,
                proposal.kind.value,
                _json(proposal.payload),
                _json(list(proposal.evidence_utterance_ids)),
                proposal.status.value,
                _datetime(proposal.created_at),
                _optional_datetime(proposal.resolved_at),
                proposal.resolved_by,
                proposal.resolution_reason,
            ),
        )
        return cursor.rowcount == 1

    def get_proposal(self, proposal_id: str) -> StructuredProposal:
        row = self.connection.execute(
            "SELECT * FROM structured_change_proposals WHERE proposal_id = ?",
            (proposal_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"proposal does not exist: {proposal_id}")
        return _proposal(row)

    def list_proposals(
        self, status: str | None, limit: int
    ) -> tuple[dict[str, Any], ...]:
        where = "WHERE p.status = ?" if status else ""
        parameters: tuple[object, ...] = (status, limit) if status else (limit,)
        rows = self.connection.execute(
            f"""
            SELECT p.*, g.layer, g.producer, g.producer_version, g.model,
              g.prompt_version, g.extractor_version, g.input_scope_json,
              g.input_sha256, g.generation_number
            FROM structured_change_proposals p
            JOIN generation_records g ON g.generation_id = p.generation_id
            {where}
            ORDER BY p.created_at DESC, p.proposal_id LIMIT ?
            """,
            parameters,
        ).fetchall()
        return tuple(_proposal_dict(row) for row in rows)

    def resolve_proposal(
        self,
        proposal_id: str,
        status: str,
        resolved_at: str,
        resolved_by: str,
        reason: str | None,
    ) -> None:
        cursor = self.connection.execute(
            """
            UPDATE structured_change_proposals
            SET status = ?, resolved_at = ?, resolved_by = ?, resolution_reason = ?
            WHERE proposal_id = ? AND status = 'pending'
            """,
            (status, resolved_at, resolved_by, reason, proposal_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("proposal is no longer pending")

    def utterance_revisions(
        self, utterance_ids: tuple[str, ...]
    ) -> dict[str, tuple[str, int]]:
        if not utterance_ids:
            return {}
        placeholders = ",".join("?" for _ in utterance_ids)
        rows = self.connection.execute(
            f"""
            SELECT utterance_id, session_id, revision FROM utterances
            WHERE utterance_id IN ({placeholders}) AND status = 'active'
            """,
            utterance_ids,
        ).fetchall()
        return {
            str(row["utterance_id"]): (str(row["session_id"]), int(row["revision"]))
            for row in rows
        }

    def utterance_facts(
        self, utterance_ids: tuple[str, ...]
    ) -> dict[str, dict[str, Any]]:
        if not utterance_ids:
            return {}
        placeholders = ",".join("?" for _ in utterance_ids)
        rows = self.connection.execute(
            f"""
            SELECT utterance_id, session_id, revision, identity, status
            FROM utterances WHERE utterance_id IN ({placeholders})
            """,
            utterance_ids,
        ).fetchall()
        return {str(row["utterance_id"]): _dict(row) for row in rows}

    def get_event(self, event_id: str) -> EventCurrentState | None:
        row = self.connection.execute(
            """
            SELECT state.*, CASE WHEN EXISTS (
              SELECT 1 FROM invalidation_events invalidation
              WHERE invalidation.target_type = 'event'
                AND invalidation.target_id = state.event_id
                AND invalidation.target_revision = state.revision
                AND invalidation.status IN ('stale', 'invalid')
            ) THEN 'stale' ELSE 'active' END AS derivation_status
            FROM event_current_states state WHERE state.event_id = ?
            """,
            (event_id,),
        ).fetchone()
        return _event_state(row) if row is not None else None

    def add_event_operation(self, operation: EventOperation) -> bool:
        cursor = self.connection.execute(
            """
            INSERT INTO event_operations (
              operation_id, event_id, session_id, event_kind, operation_kind,
              event_revision, payload_json, actor, generation_id, proposal_id,
              created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                operation.operation_id,
                operation.event_id,
                operation.session_id,
                operation.event_kind.value,
                operation.operation.value,
                operation.event_revision,
                _json(operation.payload),
                operation.actor,
                operation.generation_id,
                operation.proposal_id,
                _datetime(operation.created_at),
            ),
        )
        return cursor.rowcount == 1

    def put_event_state(
        self, state: EventCurrentState, expected_revision: int
    ) -> None:
        values = (
            state.session_id,
            state.event_kind.value,
            state.status.value,
            state.revision,
            _json(state.payload),
            state.latest_operation_id,
            _datetime(state.updated_at),
        )
        if expected_revision == 0:
            cursor = self.connection.execute(
                """
                INSERT INTO event_current_states (
                  event_id, session_id, event_kind, status, revision,
                  payload_json, latest_operation_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                (
                    state.event_id,
                    state.session_id,
                    state.event_kind.value,
                    state.status.value,
                    state.revision,
                    _json(state.payload),
                    state.latest_operation_id,
                    _datetime(state.created_at),
                    _datetime(state.updated_at),
                ),
            )
        else:
            cursor = self.connection.execute(
                """
                UPDATE event_current_states SET session_id = ?, event_kind = ?,
                  status = ?, revision = ?, payload_json = ?,
                  latest_operation_id = ?, updated_at = ?
                WHERE event_id = ? AND revision = ?
                """,
                (*values, state.event_id, expected_revision),
            )
        if cursor.rowcount != 1:
            raise ValueError("event revision conflict")

    def list_events(self, session_id: str) -> tuple[dict[str, Any], ...]:
        rows = self.connection.execute(
            """
            SELECT state.*, CASE WHEN EXISTS (
              SELECT 1 FROM invalidation_events invalidation
              WHERE invalidation.target_type = 'event'
                AND invalidation.target_id = state.event_id
                AND invalidation.target_revision = state.revision
                AND invalidation.status IN ('stale', 'invalid')
            ) THEN 'stale' ELSE 'active' END AS derivation_status
            FROM event_current_states state WHERE state.session_id = ?
            ORDER BY state.updated_at DESC, state.event_id
            """,
            (session_id,),
        ).fetchall()
        return tuple(self._event_dict(row) for row in rows)

    def event_history(self, event_id: str) -> tuple[dict[str, Any], ...]:
        rows = self.connection.execute(
            """
            SELECT * FROM event_operations WHERE event_id = ?
            ORDER BY event_revision, operation_id
            """,
            (event_id,),
        ).fetchall()
        if not rows:
            raise KeyError(f"event does not exist: {event_id}")
        return tuple(self._operation_dict(row) for row in rows)

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


class SqliteDerivationRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def add_dependency(self, dependency: DerivationDependency) -> bool:
        cursor = self.connection.execute(
            """
            INSERT INTO derivation_dependencies (
              dependent_type, dependent_id, dependent_revision,
              input_type, input_id, input_revision, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                dependency.dependent_type,
                dependency.dependent_id,
                dependency.dependent_revision,
                dependency.input_type,
                dependency.input_id,
                dependency.input_revision,
                _datetime(dependency.created_at),
            ),
        )
        return cursor.rowcount == 1

    def dependent_closure(
        self, input_type: str, input_id: str
    ) -> tuple[tuple[str, str, int], ...]:
        rows = self.connection.execute(
            """
            WITH RECURSIVE affected(target_type, target_id, target_revision) AS (
              SELECT dependent_type, dependent_id, dependent_revision
              FROM derivation_dependencies
              WHERE input_type = ? AND input_id = ?
              UNION
              SELECT dependency.dependent_type, dependency.dependent_id,
                dependency.dependent_revision
              FROM derivation_dependencies dependency
              JOIN affected parent
                ON dependency.input_type = parent.target_type
                AND dependency.input_id = parent.target_id
                AND dependency.input_revision = parent.target_revision
            )
            SELECT target_type, target_id, target_revision FROM affected
            ORDER BY target_type, target_id, target_revision
            """,
            (input_type, input_id),
        ).fetchall()
        return tuple(
            (
                str(row["target_type"]),
                str(row["target_id"]),
                int(row["target_revision"]),
            )
            for row in rows
        )

    def add_invalidation(self, event: InvalidationEvent) -> bool:
        cursor = self.connection.execute(
            """
            INSERT INTO invalidation_events (
              invalidation_id, target_type, target_id, target_revision, status,
              reason, source_type, source_id, source_revision, cascade_root_id,
              created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                event.invalidation_id,
                event.target_type,
                event.target_id,
                event.target_revision,
                event.status,
                event.reason,
                event.source_type,
                event.source_id,
                event.source_revision,
                event.cascade_root_id,
                _datetime(event.created_at),
            ),
        )
        return cursor.rowcount == 1

    def add_recompute_request(self, request: RecomputeRequest) -> bool:
        cursor = self.connection.execute(
            """
            INSERT INTO recompute_requests (
              request_id, target_type, target_id, target_revision,
              invalidation_id, status, reason, generation_id, error,
              created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                request.request_id,
                request.target_type,
                request.target_id,
                request.target_revision,
                request.invalidation_id,
                request.status,
                request.reason,
                request.generation_id,
                request.error,
                _datetime(request.created_at),
                _datetime(request.updated_at),
            ),
        )
        return cursor.rowcount == 1

    def complete_recompute_for_target(
        self,
        target_type: str,
        target_id: str,
        target_revision: int,
        generation_id: str,
        updated_at: str,
    ) -> int:
        cursor = self.connection.execute(
            """
            UPDATE recompute_requests
            SET status = 'succeeded', generation_id = ?, updated_at = ?, error = NULL
            WHERE target_type = ? AND target_id = ? AND target_revision = ?
              AND status IN ('queued', 'running')
            """,
            (
                generation_id,
                updated_at,
                target_type,
                target_id,
                target_revision,
            ),
        )
        return cursor.rowcount

    def list_invalidations(
        self, target_type: str | None, target_id: str | None, limit: int
    ) -> tuple[dict[str, Any], ...]:
        clauses: list[str] = []
        parameters: list[object] = []
        if target_type is not None:
            clauses.append("target_type = ?")
            parameters.append(target_type)
        if target_id is not None:
            clauses.append("target_id = ?")
            parameters.append(target_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(limit)
        rows = self.connection.execute(
            f"""
            SELECT * FROM invalidation_events {where}
            ORDER BY created_at DESC, invalidation_id LIMIT ?
            """,
            parameters,
        ).fetchall()
        return tuple(_dict(row) for row in rows)

    def list_recompute_requests(
        self, status: str | None, limit: int
    ) -> tuple[dict[str, Any], ...]:
        where = "WHERE status = ?" if status else ""
        parameters: tuple[object, ...] = (status, limit) if status else (limit,)
        rows = self.connection.execute(
            f"""
            SELECT * FROM recompute_requests {where}
            ORDER BY created_at, request_id LIMIT ?
            """,
            parameters,
        ).fetchall()
        return tuple(_dict(row) for row in rows)


def _generation(row: sqlite3.Row) -> GenerationRecord:
    return GenerationRecord(
        generation_id=str(row["generation_id"]),
        layer=KnowledgeLayer(str(row["layer"])),
        producer=str(row["producer"]),
        producer_version=str(row["producer_version"]),
        model=str(row["model"]),
        prompt_version=str(row["prompt_version"]),
        extractor_version=str(row["extractor_version"]),
        input_scope=_object(row["input_scope_json"]),
        input_sha256=str(row["input_sha256"]),
        generation_number=int(row["generation_number"]),
        status=GenerationStatus(str(row["status"])),
        error=str(row["error"]) if row["error"] is not None else None,
        created_at=_parse_datetime(row["created_at"]),
        completed_at=_optional_parse_datetime(row["completed_at"]),
    )


def _proposal(row: sqlite3.Row) -> StructuredProposal:
    evidence = json.loads(str(row["evidence_utterance_ids_json"]))
    if not isinstance(evidence, list) or not all(isinstance(item, str) for item in evidence):
        raise ValueError("stored proposal evidence is invalid")
    return StructuredProposal(
        proposal_id=str(row["proposal_id"]),
        generation_id=str(row["generation_id"]),
        kind=ProposalKind(str(row["kind"])),
        payload=_object(row["payload_json"]),
        evidence_utterance_ids=tuple(evidence),
        status=ProposalStatus(str(row["status"])),
        created_at=_parse_datetime(row["created_at"]),
        resolved_at=_optional_parse_datetime(row["resolved_at"]),
        resolved_by=str(row["resolved_by"]) if row["resolved_by"] else None,
        resolution_reason=(
            str(row["resolution_reason"]) if row["resolution_reason"] else None
        ),
    )


def _proposal_dict(row: sqlite3.Row) -> dict[str, Any]:
    value = _dict(row)
    value["payload"] = _object(value.pop("payload_json"))
    value["evidence_utterance_ids"] = json.loads(
        str(value.pop("evidence_utterance_ids_json"))
    )
    value["input_scope"] = _object(value.pop("input_scope_json"))
    return value


def _event_state(row: sqlite3.Row) -> EventCurrentState:
    return EventCurrentState(
        event_id=str(row["event_id"]),
        session_id=str(row["session_id"]),
        event_kind=EventKind(str(row["event_kind"])),
        status=EventStatus(str(row["status"])),
        revision=int(row["revision"]),
        payload=_object(row["payload_json"]),
        latest_operation_id=str(row["latest_operation_id"]),
        created_at=_parse_datetime(row["created_at"]),
        updated_at=_parse_datetime(row["updated_at"]),
        derivation_status=str(row["derivation_status"]),
    )


def _dict(row: sqlite3.Row) -> dict[str, Any]:
    return {str(key): row[key] for key in row.keys()}


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _object(value: object) -> dict[str, Any]:
    parsed = json.loads(str(value))
    if not isinstance(parsed, dict):
        raise ValueError("stored JSON value is not an object")
    return parsed


def _datetime(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("database timestamps must be timezone-aware")
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _optional_datetime(value: datetime | None) -> str | None:
    return _datetime(value) if value is not None else None


def _parse_datetime(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("stored timestamp is not timezone-aware")
    return parsed.astimezone(timezone.utc)


def _optional_parse_datetime(value: object) -> datetime | None:
    return _parse_datetime(value) if value is not None else None


__all__ = ["SqliteDerivationRepository", "SqliteKnowledgeRepository"]
