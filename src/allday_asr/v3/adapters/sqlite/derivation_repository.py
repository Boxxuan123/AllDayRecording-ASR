from __future__ import annotations

from typing import Any

from allday_asr.v3.domain.knowledge import (
    DerivationDependency,
    InvalidationEvent,
    RecomputeRequest,
)
from .knowledge_repository_codec import (
    _datetime,
    _dict,
)


class DerivationRepositoryMixin:
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
