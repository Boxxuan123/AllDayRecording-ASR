from __future__ import annotations
from typing import Any

from .desktop_repository_codec import (
    _device,
    _dict,
    _json_object,
)


class DesktopSystemQueryMixin:
    def list_devices(self) -> tuple[dict[str, Any], ...]:
        rows = self.connection.execute(
            """
            SELECT d.*,
              EXISTS(SELECT 1 FROM device_credentials c
                WHERE c.device_id = d.device_id AND c.revoked_at IS NULL) AS paired,
              (SELECT receiver_id FROM pairing_records p
                WHERE p.device_id = d.device_id ORDER BY paired_at DESC LIMIT 1) AS receiver_id
            FROM devices d ORDER BY d.kind, d.name, d.device_id
            """
        ).fetchall()
        return tuple(_device(row) for row in rows)

    def data_health(self) -> dict[str, Any]:
        row = self.connection.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM audio_assets) AS audio_assets,
              (SELECT COALESCE(SUM(size_bytes), 0) FROM audio_assets) AS audio_bytes,
              (SELECT COUNT(*) FROM audio_replicas WHERE state = 'available') AS available_replicas,
              (SELECT COUNT(*) FROM audio_replicas
                WHERE state IN ('failed_retryable', 'quarantined')) AS unhealthy_replicas,
              (SELECT COUNT(*) FROM backup_evidence WHERE status = 'verified') AS verified_backups,
              (SELECT COUNT(*) FROM artifacts) AS artifacts,
              (SELECT COALESCE(SUM(size_bytes), 0) FROM artifacts) AS artifact_bytes,
              (SELECT COUNT(*) FROM artifact_status_events WHERE status = 'stale') AS stale_artifacts
            """
        ).fetchone()
        return {key: int(row[key]) for key in row.keys()}

    def media(self, media_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            """
            SELECT a.media_id, a.size_bytes, a.format, r.storage_key
            FROM audio_assets a JOIN audio_replicas r ON r.asset_id = a.asset_id
            WHERE a.media_id = ? AND r.state = 'available'
            ORDER BY r.verified_at DESC, r.replica_id LIMIT 1
            """,
            (media_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"media does not exist: {media_id}")
        return _dict(row)

    def processing_events(
        self, after_sequence: int, limit: int
    ) -> tuple[dict[str, Any], ...]:
        rows = self.connection.execute(
            """
            SELECT sequence, resource_id, revision, operation, payload_json, created_at
            FROM change_events WHERE sequence > ? AND resource_type = 'processing_run'
            ORDER BY sequence LIMIT ?
            """,
            (after_sequence, limit),
        ).fetchall()
        return tuple(
            {
                **_dict(row),
                "payload": _json_object(row["payload_json"]),
            }
            for row in rows
        )
