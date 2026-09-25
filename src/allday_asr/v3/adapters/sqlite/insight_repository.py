from __future__ import annotations
from .sound_eligibility import usable_content, confirmed_interaction

import json
import sqlite3
from typing import Any


class SqliteInsightRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def event_sources(self) -> tuple[dict[str, Any], ...]:
        rows = self.connection.execute(
            """
            SELECT e.event_id, e.session_id, e.event_kind, e.status, e.revision,
              e.payload_json, e.created_at, e.updated_at,
              s.captured_start, s.captured_end, s.timezone
            FROM event_current_states e
            JOIN recording_sessions s ON s.session_id = e.session_id
            ORDER BY s.captured_start, e.event_id
            """
        ).fetchall()
        values: list[dict[str, Any]] = []
        for row in rows:
            value = _row(row)
            value["payload"] = _object(value.pop("payload_json"))
            value["evidence"] = list(
                self._utterance_evidence(
                    """
                    SELECT evidence_id FROM evidence_links
                    WHERE subject_type = 'event' AND subject_id = ?
                      AND subject_revision = ? AND evidence_type = 'utterance'
                    ORDER BY evidence_id
                    """,
                    (row["event_id"], row["revision"]),
                )
            )
            values.append(value)
        return tuple(values)

    def person_interactions(self, person_id: str) -> tuple[dict[str, Any], ...]:
        person = self.connection.execute(
            "SELECT person_id FROM persons WHERE person_id = ?", (person_id,)
        ).fetchone()
        if person is None:
            raise KeyError(f"person does not exist: {person_id}")
        rows = self.connection.execute(
            f"""
            SELECT DISTINCT u.utterance_id AS evidence_id
            FROM utterances u
            JOIN speaker_cluster_memberships membership
              ON membership.speaker_track_id = u.speaker_track_id
             AND membership.state = 'active'
            JOIN person_cluster_links link
              ON link.cluster_id = membership.cluster_id
             AND link.status = 'active'
            WHERE link.person_id = ? AND u.status = 'active'
              AND {confirmed_interaction("u")}
            ORDER BY u.start_at, u.utterance_id
            """,
            (person_id,),
        ).fetchall()
        return self._utterance_evidence_rows(rows)

    def person_profile(self, person_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            """
            SELECT p.person_id, p.display_name, p.kind, p.revision,
              profile.aliases_json, profile.relationship_labels_json, profile.notes
            FROM persons p
            LEFT JOIN person_profile_revisions profile
              ON profile.person_id = p.person_id
             AND profile.revision = (
               SELECT MAX(latest.revision) FROM person_profile_revisions latest
               WHERE latest.person_id = p.person_id
             )
            WHERE p.person_id = ?
            """,
            (person_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"person does not exist: {person_id}")
        value = _row(row)
        value["aliases"] = _array(value.pop("aliases_json") or "[]")
        value["relationship_labels"] = _array(
            value.pop("relationship_labels_json") or "[]"
        )
        return value

    def next_daily_revision(self, summary_id: str) -> int:
        row = self.connection.execute(
            "SELECT COALESCE(MAX(revision), 0) + 1 FROM daily_summary_revisions "
            "WHERE summary_id = ?",
            (summary_id,),
        ).fetchone()
        return int(row[0])

    def add_daily(self, **values: Any) -> None:
        self.connection.execute(
            """
            INSERT INTO daily_summary_revisions (
              summary_id, revision, summary_date, timezone, period_start,
              period_end, objective_json, narrative_json, input_sha256,
              generation_id, provenance_json, status, created_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                values["summary_id"],
                values["revision"],
                values["summary_date"],
                values["timezone"],
                values["period_start"],
                values["period_end"],
                _json(values["objective"]),
                _json(values["narrative"]),
                values["input_sha256"],
                values["generation_id"],
                _json(values["provenance"]),
                values["status"],
                values["created_by"],
                values["created_at"],
            ),
        )

    def daily(self, summary_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            """
            SELECT value.*,
              CASE WHEN EXISTS (
                SELECT 1 FROM invalidation_events invalidation
                WHERE invalidation.target_type = 'daily_summary'
                  AND invalidation.target_id = value.summary_id
                  AND invalidation.target_revision = value.revision
                  AND invalidation.status IN ('stale', 'invalid')
              ) THEN 'stale' ELSE value.status END AS derivation_status
            FROM daily_summary_revisions value
            WHERE value.summary_id = ? ORDER BY value.revision DESC LIMIT 1
            """,
            (summary_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"daily summary does not exist: {summary_id}")
        return self._daily_value(row)

    def list_daily(self, limit: int) -> tuple[dict[str, Any], ...]:
        rows = self.connection.execute(
            """
            SELECT value.*,
              CASE WHEN EXISTS (
                SELECT 1 FROM invalidation_events invalidation
                WHERE invalidation.target_type = 'daily_summary'
                  AND invalidation.target_id = value.summary_id
                  AND invalidation.target_revision = value.revision
                  AND invalidation.status IN ('stale', 'invalid')
              ) THEN 'stale' ELSE value.status END AS derivation_status
            FROM daily_summary_revisions value
            WHERE value.revision = (
              SELECT MAX(latest.revision) FROM daily_summary_revisions latest
              WHERE latest.summary_id = value.summary_id
            )
            ORDER BY value.summary_date DESC, value.timezone, value.summary_id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return tuple(self._daily_value(row) for row in rows)

    def next_relationship_revision(self, report_id: str) -> int:
        row = self.connection.execute(
            "SELECT COALESCE(MAX(revision), 0) + 1 "
            "FROM relationship_observation_revisions WHERE report_id = ?",
            (report_id,),
        ).fetchone()
        return int(row[0])

    def add_relationship(self, **values: Any) -> None:
        self.connection.execute(
            """
            INSERT INTO relationship_observation_revisions (
              report_id, revision, person_id, window_days, end_date, timezone,
              period_start, period_end, verified_facts_json, observations_json,
              input_sha256, generation_id, provenance_json, status,
              created_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                values["report_id"],
                values["revision"],
                values["person_id"],
                values["window_days"],
                values["end_date"],
                values["timezone"],
                values["period_start"],
                values["period_end"],
                _json(values["verified_facts"]),
                _json(values["observations"]),
                values["input_sha256"],
                values["generation_id"],
                _json(values["provenance"]),
                values["status"],
                values["created_by"],
                values["created_at"],
            ),
        )

    def relationship(self, report_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            """
            SELECT value.*,
              CASE WHEN EXISTS (
                SELECT 1 FROM invalidation_events invalidation
                WHERE invalidation.target_type = 'relationship_observation'
                  AND invalidation.target_id = value.report_id
                  AND invalidation.target_revision = value.revision
                  AND invalidation.status IN ('stale', 'invalid')
              ) THEN 'stale' ELSE value.status END AS derivation_status
            FROM relationship_observation_revisions value
            WHERE value.report_id = ? ORDER BY value.revision DESC LIMIT 1
            """,
            (report_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"relationship observation does not exist: {report_id}")
        return self._relationship_value(row)

    def list_relationships(
        self, person_id: str | None, limit: int
    ) -> tuple[dict[str, Any], ...]:
        where = "AND value.person_id = ?" if person_id else ""
        parameters: tuple[object, ...] = (
            (person_id, limit) if person_id is not None else (limit,)
        )
        rows = self.connection.execute(
            f"""
            SELECT value.*,
              CASE WHEN EXISTS (
                SELECT 1 FROM invalidation_events invalidation
                WHERE invalidation.target_type = 'relationship_observation'
                  AND invalidation.target_id = value.report_id
                  AND invalidation.target_revision = value.revision
                  AND invalidation.status IN ('stale', 'invalid')
              ) THEN 'stale' ELSE value.status END AS derivation_status
            FROM relationship_observation_revisions value
            WHERE value.revision = (
              SELECT MAX(latest.revision)
              FROM relationship_observation_revisions latest
              WHERE latest.report_id = value.report_id
            ) {where}
            ORDER BY value.end_date DESC, value.window_days, value.report_id
            LIMIT ?
            """,
            parameters,
        ).fetchall()
        return tuple(self._relationship_value(row) for row in rows)

    def add_evidence(
        self,
        link_id: str,
        insight_type: str,
        insight_id: str,
        insight_revision: int,
        event_id: str | None,
        event_revision: int | None,
        utterance_id: str | None,
        utterance_revision: int | None,
        created_at: str,
    ) -> bool:
        cursor = self.connection.execute(
            """
            INSERT INTO insight_evidence (
              link_id, insight_type, insight_id, insight_revision,
              event_id, event_revision, utterance_id, utterance_revision,
              created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                link_id,
                insight_type,
                insight_id,
                insight_revision,
                event_id,
                event_revision,
                utterance_id,
                utterance_revision,
                created_at,
            ),
        )
        return cursor.rowcount == 1

    def add_operation(
        self,
        operation_id: str,
        insight_type: str,
        insight_id: str,
        insight_revision: int,
        kind: str,
        actor: str,
        payload: dict[str, Any],
        created_at: str,
        reverts_operation_id: str | None = None,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO insight_operations (
              operation_id, insight_type, insight_id, insight_revision,
              kind, actor, payload_json, reverts_operation_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                operation_id,
                insight_type,
                insight_id,
                insight_revision,
                kind,
                actor,
                _json(payload),
                reverts_operation_id,
                created_at,
            ),
        )

    def latest_relationship_operation(self, report_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT * FROM insight_operations
            WHERE insight_type = 'relationship_observation' AND insight_id = ?
            ORDER BY created_at DESC, operation_id DESC LIMIT 1
            """,
            (report_id,),
        ).fetchone()
        if row is None:
            return None
        value = _row(row)
        value["payload"] = _object(value.pop("payload_json"))
        return value

    def evidence_revisions(
        self, event_ids: tuple[str, ...], utterance_ids: tuple[str, ...]
    ) -> tuple[dict[str, int], dict[str, int]]:
        events: dict[str, int] = {}
        utterances: dict[str, int] = {}
        if event_ids:
            placeholders = ",".join("?" for _ in event_ids)
            rows = self.connection.execute(
                f"SELECT event_id, revision FROM event_current_states "
                f"WHERE event_id IN ({placeholders})",
                event_ids,
            ).fetchall()
            events = {str(row["event_id"]): int(row["revision"]) for row in rows}
        if utterance_ids:
            placeholders = ",".join("?" for _ in utterance_ids)
            rows = self.connection.execute(
                f"SELECT utterance_id, revision FROM utterances "
                f"WHERE utterance_id IN ({placeholders}) AND status = 'active'",
                utterance_ids,
            ).fetchall()
            utterances = {
                str(row["utterance_id"]): int(row["revision"]) for row in rows
            }
        return events, utterances

    def _daily_value(self, row: sqlite3.Row) -> dict[str, Any]:
        value = _row(row)
        value["objective"] = _object(value.pop("objective_json"))
        value["narrative"] = _object(value.pop("narrative_json"))
        value["provenance"] = _object(value.pop("provenance_json"))
        value["evidence"] = list(
            self._insight_evidence(
                "daily_summary", value["summary_id"], value["revision"]
            )
        )
        return value

    def _relationship_value(self, row: sqlite3.Row) -> dict[str, Any]:
        value = _row(row)
        value["verified_facts"] = _object(value.pop("verified_facts_json"))
        value["observations"] = _array(value.pop("observations_json"))
        value["provenance"] = _object(value.pop("provenance_json"))
        value["evidence"] = list(
            self._insight_evidence(
                "relationship_observation", value["report_id"], value["revision"]
            )
        )
        return value

    def _insight_evidence(
        self, insight_type: str, insight_id: str, revision: int
    ) -> tuple[dict[str, Any], ...]:
        links = self.connection.execute(
            """
            SELECT event_id, event_revision, utterance_id, utterance_revision
            FROM insight_evidence
            WHERE insight_type = ? AND insight_id = ? AND insight_revision = ?
            ORDER BY event_id, utterance_id
            """,
            (insight_type, insight_id, revision),
        ).fetchall()
        utterance_ids = [
            str(row["utterance_id"]) for row in links if row["utterance_id"] is not None
        ]
        playable = {
            item["utterance_id"]: item
            for item in self._utterance_evidence_ids(tuple(utterance_ids))
        }
        return tuple(
            {
                "event_id": row["event_id"],
                "event_revision": row["event_revision"],
                "utterance_id": row["utterance_id"],
                "utterance_revision": row["utterance_revision"],
                **(
                    {
                        key: value
                        for key, value in playable.get(
                            str(row["utterance_id"]), {}
                        ).items()
                        if key not in {"utterance_id", "revision"}
                    }
                    if row["utterance_id"] is not None
                    else {}
                ),
            }
            for row in links
        )

    def _utterance_evidence(
        self, query: str, parameters: tuple[object, ...]
    ) -> tuple[dict[str, Any], ...]:
        rows = self.connection.execute(query, parameters).fetchall()
        return self._utterance_evidence_rows(rows)

    def _utterance_evidence_rows(
        self, rows: list[sqlite3.Row]
    ) -> tuple[dict[str, Any], ...]:
        return self._utterance_evidence_ids(
            tuple(str(row["evidence_id"]) for row in rows)
        )

    def _utterance_evidence_ids(
        self, utterance_ids: tuple[str, ...]
    ) -> tuple[dict[str, Any], ...]:
        if not utterance_ids:
            return ()
        placeholders = ",".join("?" for _ in utterance_ids)
        rows = self.connection.execute(
            f"""
            SELECT u.utterance_id, u.revision, u.session_id, u.start_at, u.end_at,
              u.text, u.start_ms AS session_start_ms, u.end_ms AS session_end_ms,
              asset.media_id,
              segment.source_start_ms + MAX(u.start_ms, segment.session_start_ms)
                - segment.session_start_ms AS start_ms,
              segment.source_start_ms + MIN(u.end_ms, segment.session_end_ms)
                - segment.session_start_ms AS end_ms
            FROM utterances u
            LEFT JOIN capture_segments segment
              ON segment.session_id = u.session_id
             AND segment.session_start_ms < u.end_ms
             AND segment.session_end_ms > u.start_ms
            LEFT JOIN audio_assets asset ON asset.asset_id = segment.asset_id
            WHERE u.utterance_id IN ({placeholders}) AND u.status = 'active'
              AND {usable_content("u")}
            GROUP BY u.utterance_id
            ORDER BY u.start_at, u.utterance_id
            """,
            utterance_ids,
        ).fetchall()
        return tuple(_row(row) for row in rows)


def _row(row: sqlite3.Row) -> dict[str, Any]:
    return {str(key): row[key] for key in row.keys()}


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _object(value: object) -> dict[str, Any]:
    parsed = json.loads(str(value))
    if not isinstance(parsed, dict):
        raise ValueError("stored insight JSON is not an object")
    return parsed


def _array(value: object) -> list[Any]:
    parsed = json.loads(str(value))
    if not isinstance(parsed, list):
        raise ValueError("stored insight JSON is not an array")
    return parsed


__all__ = ["SqliteInsightRepository"]
