from __future__ import annotations

from typing import Any

from allday_asr.v3.domain.knowledge import (
    EvidenceSpan,
    GenerationRecord,
    StructuredProposal,
)
from .knowledge_repository_codec import (
    _datetime,
    _dict,
    _generation,
    _json,
    _optional_datetime,
    _proposal,
    _proposal_dict,
)


class KnowledgeGenerationRepositoryMixin:
    def unmaterialized_evidence(self, session_id: str) -> tuple[dict[str, Any], ...]:
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
